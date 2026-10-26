"""Read-only intake of ARL's fixed planning actor, never enrollment or execution.

The seal covers the supplied SDK/product subset. CPython stdlib/site/native
libraries and the complete running Console are explicitly outside that seal.
Caller-provided paths or hashes are not an authorization policy.
"""

import ast
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .arl import ARLError

MANIFEST_FIELDS = {
    "schema_version",
    "files",
    "source_set_sha256",
    "product",
    "owner",
    "product_version",
    "identity",
    "python_executable",
    "python_sha256",
    "entry_sha256",
    "host_sha256",
    "production_enabled",
    "trusted_product_subset",
}
ENTRIES = {"planning_host.py", "planning_consumer.py"}
PRODUCT = {"product/homeplane/__init__.py", "product/homeplane/arl.py"}
SHA = re.compile(r"[a-f0-9]{64}\Z")
V3_FIELDS = MANIFEST_FIELDS | {
    "asset_files",
    "runtime_files",
    "provider_calls",
    "review_binding",
    "runtime_trust",
    "sealed_payload_sha256",
    "source_deployment",
}


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _checked_path(path, *, directory=False, owner=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in [path, *path.parents]):
        raise ARLError("actor_unsafe_path")
    s = path.lstat()
    if (
        not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode))
        or s.st_mode & 0o022
        or owner
        and s.st_uid != os.geteuid()
    ):
        raise ARLError("actor_unsafe_path")
    return s


def _read(path, maximum=1048576):
    before = _checked_path(path)
    if before.st_size > maximum:
        raise ARLError("actor_source_budget")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    current = _checked_path(path)
    signatures = [
        (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        for s in (before, opened, after, current)
    ]
    if len(set(signatures)) != 1 or len(raw) != before.st_size:
        raise ARLError("actor_source_changed")
    return raw


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ARLError("actor_invalid_manifest")
            value[key] = item
        return value

    try:
        return json.loads(
            raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (TypeError, ValueError, UnicodeError):
        raise ARLError("actor_invalid_manifest") from None


@dataclass(frozen=True)
class ActorSeal:
    candidate: Path
    manifest_sha256: str
    source_set_sha256: str
    fullset_sha256: str
    file_count: int
    interpreter: Path
    interpreter_sha256: str
    product_matches: bool
    site_import_disabled: bool
    manifest_version: int = 1
    runtime_file_count: int = 0
    payload_sha256: str | None = None
    payload_bytes: int = 0

    def report(self):
        return {
            "status": "source_and_copied_runtime_verified"
            if self.manifest_version == 2
            else "source_subset_verified",
            "manifest_sha256": self.manifest_sha256,
            "source_set_sha256": self.source_set_sha256,
            "fullset_sha256": self.fullset_sha256,
            "file_count": self.file_count,
            "interpreter": str(self.interpreter),
            "interpreter_sha256": self.interpreter_sha256,
            "current_product_subset_matches": self.product_matches,
            "site_import_disabled": self.site_import_disabled,
            "manifest_version": self.manifest_version,
            "runtime_file_count": self.runtime_file_count,
            "copied_runtime_verified": self.manifest_version == 2 and self.runtime_file_count > 0,
            "payload_sha256": self.payload_sha256,
            "payload_bytes": self.payload_bytes,
            "os_trusted_base": "macOS system dylibs/frameworks" if self.manifest_version == 2 else None,
            "complete_executable_environment_verified": False,
            "console_identity_verified": False,
            "actor_registered": False,
            "execution_grants": False,
            "model_inference_attempted": False,
            "device_write_attempted": False,
        }


def inspect_candidate(candidate, expected_manifest_sha256, *, product_root):
    """Explicit local source review only. Does not open reviewed payload or state."""
    candidate, product_root = Path(candidate), Path(product_root)
    _checked_path(candidate, directory=True)
    if type(expected_manifest_sha256) is not str or not SHA.fullmatch(expected_manifest_sha256):
        raise ARLError("actor_invalid_manifest")
    raw = _read(candidate / "source-manifest.json", 1048576)
    if _digest(raw) != expected_manifest_sha256:
        raise ARLError("actor_manifest_changed")
    m = _json(raw)
    if (
        type(m) is not dict
        or set(m) != (V3_FIELDS if m.get("schema_version") == 2 else MANIFEST_FIELDS)
        or type(m["schema_version"]) is not int
        or m["schema_version"] not in {1, 2}
        or m["owner"] != "friday"
        or m["product"] != "Friday Planning"
        or m["production_enabled"] is not False
        or type(m["files"]) is not dict
        or not 6 <= len(m["files"]) <= 64
        or not PRODUCT <= m["files"].keys()
        or m["identity"] != "host_owned_pid_uid_birth_plus_exact_source_set"
    ):
        raise ARLError("actor_invalid_manifest")
    v3 = m["schema_version"] == 2
    runtime = m.get("runtime_files", {})
    assets = m.get("asset_files", {})
    if v3 and (
        type(runtime) is not dict
        or not 1 <= len(runtime) <= 4096
        or type(assets) is not dict
        or set(assets) != {"load_sdk.py", "registration-proposal.json"}
        or m["source_deployment"] is not False
        or type(m["provider_calls"]) is not int
        or m["provider_calls"] != 0
        or m["python_executable"] != "runtime/bin/python3.12"
        or runtime.get("runtime/bin/python3.12") != m["python_sha256"]
    ):
        raise ARLError("actor_invalid_manifest")
    required = {
        "sdk/arl/__init__.py",
        "sdk/arl/client.py",
        "sdk/arl/friday_planning.py",
        "sdk/arl/friday_planning_client.py",
        "sdk/arl/friday_profile.py",
        "sdk/arl/supervisor.py",
        "sdk/arl/identity.py",
    }
    if not required <= m["files"].keys():
        raise ARLError("actor_incomplete_source_subset")
    files, budget = {}, 0
    for name, digest in m["files"].items():
        if (
            name not in PRODUCT
            and not re.fullmatch(r"sdk/arl/[A-Za-z_][A-Za-z0-9_]*\.py", name)
            or type(digest) is not str
            or not SHA.fullmatch(digest)
        ):
            raise ARLError("actor_invalid_manifest")
        data = _read(candidate / name)
        budget += len(data)
        if budget > 8 * 1048576:
            raise ARLError("actor_source_budget")
        if _digest(data) != digest:
            raise ARLError("actor_source_changed")
        files[name] = digest
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    if _digest(canonical) != m["source_set_sha256"]:
        raise ARLError("actor_invalid_manifest")
    for name, field in (("planning_host.py", "host_sha256"), ("planning_consumer.py", "entry_sha256")):
        data = _read(candidate / name)
        if _digest(data) != m[field]:
            raise ARLError("actor_source_changed")
        files[name] = _digest(data)
        budget += len(data)
    for name, digest in {**runtime, **assets}.items():
        parts = Path(name).parts
        if (
            type(digest) is not str
            or not SHA.fullmatch(digest)
            or Path(name).is_absolute()
            or ".." in parts
            or name not in assets
            and (
                not (name == "runtime/bin/python3.12" or name.startswith("runtime/lib/"))
                or "site-packages" in parts
                or "__pycache__" in parts
            )
        ):
            raise ARLError("actor_invalid_manifest")
        data = _read(candidate / name, 64 * 1048576)
        budget += len(data)
        if budget > 128 * 1048576:
            raise ARLError("actor_source_budget")
        if _digest(data) != digest:
            raise ARLError("actor_source_changed")
        files[name] = digest
    payload_sha = _digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
    if v3 and payload_sha != m["sealed_payload_sha256"]:
        raise ARLError("actor_invalid_manifest")
    actual, visited = set(), 0
    for folder, directories, names in os.walk(candidate, followlinks=False):
        visited += 1 + len(directories) + len(names)
        if visited > (16384 if v3 else 256):
            raise ARLError("actor_source_budget")
        for d in list(directories):
            path = Path(folder) / d
            _checked_path(path, directory=True)
            if path == candidate / "state":
                directories.remove(d)  # Do not read runtime journals or prompts.
        for name in names:
            path = Path(folder) / name
            _checked_path(path)
            relative = str(path.relative_to(candidate))
            if relative == "reviewed-request.json":
                continue  # Payload has an independent short-lived content review.
            actual.add(relative)
            if len(actual) > (4165 if v3 else 67):
                raise ARLError("actor_source_budget")
    if actual != set(files) | {"source-manifest.json"}:
        raise ARLError("actor_source_set_changed")
    # Re-read the captured bytes to detect a change during traversal.
    for name, digest in files.items():
        if _digest(_read(candidate / name, 64 * 1048576 if name in runtime else 1048576)) != digest:
            raise ARLError("actor_source_changed")
    if _read(candidate / "source-manifest.json", 1048576) != raw:
        raise ARLError("actor_manifest_changed")
    files["source-manifest.json"] = expected_manifest_sha256
    subset = {name.removeprefix("product/"): m["files"][name] for name in PRODUCT}
    if m["trusted_product_subset"] != subset:
        raise ARLError("actor_invalid_manifest")
    matches = all(_digest(_read(product_root / name)) == digest for name, digest in subset.items())
    interpreter = candidate / m["python_executable"] if v3 else Path(m["python_executable"])
    _checked_path(interpreter, owner=False)
    before = interpreter.stat()
    if before.st_size > 256 * 1048576:
        raise ARLError("actor_source_budget")
    fd = os.open(interpreter, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        exe_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if interpreter.stat() != before or exe_hash != m["python_sha256"]:
        raise ARLError("actor_interpreter_changed")
    # This exact upstream profile's launch argv is pinned source, not caller data.
    profile = ast.parse(_read(candidate / "sdk/arl/friday_profile.py"))
    launches = [
        n
        for n in ast.walk(profile)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "LaunchSpec"
    ]
    if len(launches) != 1 or len(launches[0].args) < 3 or not isinstance(launches[0].args[2], ast.Tuple):
        raise ARLError("actor_unsupported_launch_profile")
    flags = [
        n.value for n in launches[0].args[2].elts if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    if v3:
        declarations = [
            n
            for n in ast.walk(profile)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "flags" for t in n.targets)
        ]
        expected = ast.parse("m.get('schema_version') == 2", mode="eval").body
        if (
            len(declarations) != 1
            or not isinstance(declarations[0].value, ast.IfExp)
            or ast.dump(declarations[0].value.test) != ast.dump(expected)
            or not any(
                isinstance(n, ast.Starred) and isinstance(n.value, ast.Name) and n.value.id == "flags"
                for n in launches[0].args[2].elts
            )
        ):
            raise ARLError("actor_unsupported_launch_profile")
        flags = ast.literal_eval(declarations[0].value.body)
        if flags != ("-I", "-S", "-B"):
            raise ARLError("actor_unsupported_launch_profile")
    no_site = "-S" in flags
    return ActorSeal(
        candidate,
        expected_manifest_sha256,
        m["source_set_sha256"],
        _digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()),
        len(files),
        interpreter,
        exe_hash,
        matches,
        no_site,
        m["schema_version"],
        len(runtime),
        payload_sha if v3 else None,
        budget,
    )
