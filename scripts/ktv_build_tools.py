"""Small offline helpers for KTV's owned build areas and read-only inputs."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path


def toolchain_identity(java: Path, *, ndk: Path | None = None, compiler: str | None = None) -> str:
    release = java / "release"
    versions = re.findall(r'^JAVA_VERSION="([A-Za-z0-9_.+-]{1,40})"$', release.read_text(), re.MULTILINE) if release.is_file() else []
    # A standalone compatible JDK can lack release metadata; identify its actual
    # javac bytes rather than imposing a new JDK installation requirement.
    java_id = versions[0].replace("+", "_") if len(versions) == 1 else hashlib.sha256((java / "bin/javac").read_bytes()).hexdigest()[:12]
    identity = "jdk" + java_id
    if ndk is not None:
        properties = ndk / "source.properties"
        if not properties.is_file() or not re.search(r'^Pkg.Revision\s*=\s*27\.3\.13750724\s*$', properties.read_text(), re.MULTILINE):
            raise ValueError("pinned_ndk_r27d_required")
        identity += "-ndk27.3.13750724"
    if compiler is not None:
        version = subprocess.run([compiler, "--version"], check=True, capture_output=True, text=True, timeout=10).stdout
        identity += "-cxx-" + hashlib.sha256(version.encode()).hexdigest()[:12]
    return identity


def build_environment(environment: dict[str, str], area: Path) -> dict[str, str]:
    temporary = area / "tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    return {**environment, "TMPDIR": str(temporary), "FRIDAY_KTV_BUILD_TMPDIR": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1", "CLANG_MODULE_CACHE_PATH": str(area / "module-cache")}


def workspace_receipt(record: dict) -> dict:
    # The private cache manifest retains reproduction arguments. Public tool
    # stdout must not echo an optional household --host or private profile path.
    fields = ("toolchain", "purpose", "configuration", "actual_output_path", "status", "retain",
              "payload_removed", "exclusive_lock_released", "logical_bytes_unlinked",
              "allocated_bytes_inventoried", "actual_free_space_delta_measured", "oboe_objects")
    return {name: record[name] for name in fields if name in record}


def isolated_command(environment: dict[str, str], command) -> list[str]:
    values = [str(value) for value in command]
    temporary = environment.get("FRIDAY_KTV_BUILD_TMPDIR")
    if temporary:
        name = Path(values[0]).name
        # Android's shell launchers expect -JDfoo, whereas JDK launchers
        # expect -J-Dfoo. Use arguments, never JAVA_TOOL_OPTIONS (which echoes
        # inherited options to stderr and can expose private caller settings).
        prefix = "-D" if name == "java" else "-JD" if name in {"d8", "apksigner"} else "-J-D" if name in {"javac", "keytool"} else None
        if prefix:
            values.insert(1, prefix + "java.io.tmpdir=" + temporary)
    return values


def reuse_oboe_object(cached: Path, output: Path) -> bool:
    """Copy one hash-checked object into this run; never modify shared inputs."""
    proof = cached.with_suffix(".sha256")
    try:
        if any(p.is_symlink() or p.resolve() != p.absolute() or not p.is_file() for p in (cached, proof)):
            return False
        if cached.stat().st_size > 32 * 1024 * 1024 or proof.stat().st_size > 128:
            return False
        expected = proof.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(cached.read_bytes()).hexdigest() != expected:
            return False
        shutil.copyfile(cached, output)
        if hashlib.sha256(output.read_bytes()).hexdigest() == expected:
            return True
    except OSError:
        pass
    # Only this run's disposable destination can be removed after a raced or
    # interrupted copy. A missing/corrupt shared input is rebuilt locally.
    if output.exists():
        output.unlink()
    return False
