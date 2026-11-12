"""Prepare private NAS/Worker/TV migration inputs offline; never installs or starts anything."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r"[A-Za-z0-9_-]{43,100}")
RFC1918 = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ipv4(value: str, *, loopback: bool = False) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("private_ipv4_required") from None
    if address.version != 4 or not (any(address in network for network in RFC1918)
                                    or loopback and address.is_loopback):
        raise ValueError("private_ipv4_required")
    return str(address)


def validate_origin(value: str, *, loopback: bool = True) -> str:
    """Match Android KtvEndpoint without DNS, network requests or TLS overrides."""
    if not isinstance(value, str) or len(value) > 512 or not re.match(r"https?://", value):
        raise ValueError("private_http_or_https_required")
    try:
        parsed = urlsplit(value)
        host, port, path = parsed.hostname, parsed.port, parsed.path
        if (not host or len(host) > 253 or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or len(path) > 128 or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/?", path)):
            raise ValueError()
        if parsed.scheme == "http":
            if private_ipv4(host, loopback=loopback) != host or port is None or not 1024 <= port <= 65535:
                raise ValueError()
        elif parsed.scheme != "https" or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        return parsed.scheme + "://" + host.lower() + (":" + str(port) if port is not None else "") + path.rstrip("/")
    except (ValueError, UnicodeError):
        raise ValueError("invalid_connection_origin") from None


def profile_fingerprint(role: str, origin: str, token: str) -> str:
    return hashlib.sha256(("friday-ktv-profile-v1\n" + role + "\n" + origin + "\n" + token + "\n").encode()).hexdigest()


def private_path(path: Path, root: Path = ROOT) -> Path:
    value, root = path.absolute(), root.absolute()
    if value.resolve() != value or not any(value.is_relative_to(root / name) for name in ("data", ".artifacts")):
        raise ValueError("ignored_private_workspace_path_required")
    return value


def read_regular(path: Path, *, private: bool = True, limit: int = 65536) -> bytes:
    path = path.absolute()
    if path.resolve() != path or not path.is_file() or path.is_symlink():
        raise ValueError("canonical_regular_input_required")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit or private and metadata.st_mode & 0o077:
        raise ValueError("private_bounded_input_required")
    return path.read_bytes()


def read_nas_connection(path: Path, root: Path = ROOT) -> dict:
    raw = read_regular(private_path(path, root))
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or not {"origin", "token"} <= set(value)
                or set(value) - {"schema", "role", "origin", "token", "profile_fingerprint"}
                or type(value.get("schema", 1)) is not int or value.get("schema", 1) != 1
                or value.get("role", "nas") != "nas" or not isinstance(value["token"], str)
                or not KEY.fullmatch(value["token"])):
            raise ValueError()
        origin = validate_origin(value["origin"])
        fingerprint = profile_fingerprint("nas", origin, value["token"])
        if value.get("profile_fingerprint", fingerprint) != fingerprint:
            raise ValueError()
        return {"role": "nas", "origin": origin, "token": value["token"], "fingerprint": fingerprint,
                "input_sha256": hashlib.sha256(raw).hexdigest()}
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid_private_nas_connection") from None


def launchd_worker(raw: bytes) -> tuple[dict, str, int, int]:
    try:
        value = plistlib.loads(raw)
        args = value["ProgramArguments"]
        if (value.get("Label") != "local.friday.ktv" or not isinstance(args, list)
                or not 5 <= len(args) <= 40 or any(not isinstance(arg, str) or len(arg) > 4096 for arg in args)
                or not Path(args[0]).is_absolute() or not re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", Path(args[0]).name)
                or not Path(args[1]).is_absolute() or not args[1].endswith("/scripts/ktv_server.py") or args[2] != "serve"):
            raise ValueError()
        indexes = {}
        for flag in ("--host", "--port", "--public-config"):
            if args.count(flag) != 1:
                raise ValueError()
            index = args.index(flag)
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                raise ValueError()
            indexes[flag] = index + 1
        host = private_ipv4(args[indexes["--host"]])
        port = int(args[indexes["--port"]])
        if not 1024 <= port <= 65535:
            raise ValueError()
        sockets = value.get("Sockets", {})
        if "--launchd-sockets" in args and not sockets:
            raise ValueError()
        if sockets:
            if set(sockets) != {"KTVLAN", "KTVLocal"}:
                raise ValueError()
            for name, address in (("KTVLAN", host), ("KTVLocal", "127.0.0.1")):
                listener = sockets.get(name, {})
                if (listener.get("SockNodeName") != address or str(listener.get("SockServiceName")) != str(port)
                        or listener.get("SockFamily") != "IPv4" or listener.get("SockType") != "stream"):
                    raise ValueError()
        return value, host, port, indexes["--public-config"]
    except (ValueError, KeyError, TypeError, IndexError, plistlib.InvalidFileException):
        raise ValueError("configured_worker_plist_required") from None


def write_new(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def prepare(public_config: Path, launchd_plist: Path, nas_origin: str, output: Path,
            *, existing_tv_key: Path | None = None, root: Path = ROOT) -> dict:
    destination = private_path(output, root)
    if destination.exists():
        raise ValueError("new_output_directory_required")
    config_path = private_path(public_config, root)
    config_bytes = read_regular(config_path)
    plist_bytes = read_regular(launchd_plist, private=False)
    try:
        old = json.loads(config_bytes)
        if (not isinstance(old, dict) or not isinstance(old.get("agent_key"), str)
                or not KEY.fullmatch(old["agent_key"]) or not isinstance(old.get("endpoint"), str)):
            raise ValueError()
        # Keep the existing, owner-configured guest/agent endpoint. A TV LAN
        # endpoint is separate and never replaces the public phone endpoint.
        public = urlsplit(old["endpoint"])
        if (public.scheme != "https" or not public.hostname or public.username or public.password
                or public.port is not None and not 1 <= public.port <= 65535
                or public.query or public.fragment or not re.fullmatch(r"/[a-z0-9_-]+", public.path)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("private_public_configuration_required") from None
    definition, host, port, public_index = launchd_worker(plist_bytes)
    configured_public = Path(definition["ProgramArguments"][public_index]).absolute()
    if configured_public != config_path:
        raise ValueError("worker_public_configuration_mismatch")
    origin = validate_origin(nas_origin, loopback=False)
    token = read_regular(existing_tv_key, limit=256).decode().strip() if existing_tv_key else secrets.token_urlsafe(32)
    if not KEY.fullmatch(token) or secrets.compare_digest(token, old["agent_key"]):
        raise ValueError("separate_private_tv_key_required")
    fingerprint = profile_fingerprint("nas", origin, token)
    worker = {**old, "coordination": "nas", "media_origin": "http://" + host + ":" + str(port)}
    tv = {"schema": 1, "role": "nas", "origin": origin, "token": token, "profile_fingerprint": fingerprint}
    definition["ProgramArguments"][public_index] = str(destination / "mac-nas.json")
    deployment = """# Private KTV coordinator migration candidate

Prepared offline. Existing data, configuration, services, TV and audio were not changed.

1. Before deployment, inspect the existing NAS private runtime `tv-key` file locally. If it already exists, preserve it and rerun this helper into a NEW directory with `--existing-tv-key-file` pointing to the private local copy. Do not overwrite a working key with this candidate merely because it is newer. Never paste any key into chat or a commit.
2. If no NAS TV key exists, install `nas-tv-key` as the NAS runtime `tv-key` with mode 0600 before starting the new NAS server, using a no-clobber operation. The server may otherwise generate its own different key. Keep the existing database and agent key; take a consistent SQLite backup on the NAS before migration. Do not copy a live SQLite database through SMB.
3. Review `mac-nas.json` and the draft `mac-launchd.plist`. The draft preserves the configured LAN host, port, launchd sockets and recovery policy; it only selects the new private Worker configuration. Updating/reloading the actual launch agent is a separate deployment action. This helper never runs launchctl, executes plist commands, wakes a Mac or edits the original configuration.
4. Build the private TV candidate with `--nas-connection-config <this-directory>/tv-connection.json`, using the existing APK signing identity. The new NAS profile applies once when the app is opened; portable builds and an already-seen profile preserve subsequent manual TV settings. Keep private APKs out of public release artifacts.
5. The NAS origin must be reachable directly from the TV on the LAN. Use the configured NAS LAN listener or valid HTTPS endpoint and prefix. This plan changes no DHCP, DNS, routing, firewall or TLS validation. Confirm the actual listener and certificate before installation; a prepared file is not a connectivity check.
6. Publish NAS code/mobile assets and update the TV APK as separate deployments. Validate complete readiness, current-session mute/command protection, Mac direct transfer with NAS caching pending, cache fallback, persistent room/queue and acoustic singing separately. Tonight's offline checks are not physical acceptance.
"""
    files = {
        "mac-nas.json": json.dumps(worker, indent=2).encode() + b"\n",
        "tv-connection.json": json.dumps(tv, indent=2).encode() + b"\n",
        "nas-tv-key": token.encode() + b"\n",
        "mac-launchd.plist": plistlib.dumps(definition),
        "DEPLOYMENT.md": deployment.encode(),
    }
    receipt = {"schema": 1, "prepared": True, "role": "nas", "profile_fingerprint": fingerprint,
               "existing_tv_key_reused": existing_tv_key is not None, "nas_existing_key_checked": False,
               "source_sha256": {"public_config": hashlib.sha256(config_bytes).hexdigest(),
                                 "launchd_plist": hashlib.sha256(plist_bytes).hexdigest()},
               "files_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
               "installed": False, "services_started": False, "device_contacted": False, "secrets_printed": False}
    files["prepare-result.json"] = json.dumps(receipt, indent=2).encode() + b"\n"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Claim the destination before writing any secret. Existing targets, even
    # empty directories, are never replaced and each file uses O_EXCL.
    destination.mkdir(mode=0o700, exist_ok=False)
    for name, raw in files.items():
        write_new(destination / name, raw)
    return {"prepared": True, "files": len(files), "existing_tv_key_reused": existing_tv_key is not None,
            "installed": False, "services_started": False, "device_contacted": False, "secrets_printed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-config", type=Path, required=True)
    parser.add_argument("--launchd-plist", type=Path, required=True)
    parser.add_argument("--nas-origin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing-tv-key-file", type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.public_config, args.launchd_plist, args.nas_origin, args.output,
                         existing_tv_key=args.existing_tv_key_file, root=ROOT)
    except (ValueError, OSError, UnicodeError):
        print(json.dumps({"prepared": False, "code": "private_migration_inputs_invalid", "secrets_printed": False}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
