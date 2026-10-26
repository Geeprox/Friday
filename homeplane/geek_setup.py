"""Supervised read-only Oh My Sage session; never run a device writer."""
import asyncio
import fcntl
import ipaddress
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .settings import run_bounded

ROOT = Path(__file__).resolve().parents[1]


class GeekSetup:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.secret = self.directory / "geek-passcode"
        self.evidence = self.directory / "geek-evidence"
        self.gate = asyncio.Lock()

    def available(self):
        try:
            if self.secret.is_symlink() or any(p.is_symlink() for p in self.secret.parents):
                return False
            info = self.secret.stat()
            return self.secret.is_file() and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0 and info.st_size <= 1024
        except OSError:
            return False

    def save(self, passcode):
        if not passcode or len(passcode) > 128 or any(ord(c) < 32 for c in passcode):
            raise ValueError("invalid_passcode")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.secret.is_symlink() or any(p.is_symlink() for p in self.directory.parents) or self.directory.is_symlink():
            raise ValueError("secret_path_symlink")
        fd, temporary = tempfile.mkstemp(prefix=".geek-", dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(passcode)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.secret)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"credential_available": True, "secret_values_returned": False}

    def prune(self, active=None):
        if not self.evidence.exists():
            return
        fd = self._lease_file(self.evidence / ".cleanup.lock")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._prune(active)
        finally:
            os.close(fd)

    def _prune(self, active=None):
        # Raw Graphs remain private, exact and opaque for seven days only.
        if self.evidence.is_symlink() or any(p.is_symlink() for p in self.evidence.parents):
            raise ValueError("evidence_symlink")
        if self.evidence.exists():
            candidates = []
            for item in self.evidence.iterdir():
                if re.fullmatch(r"session-[a-f0-9-]{36}", item.name) and item.is_dir() and not item.is_symlink():
                    if active is not None and item == active:
                        continue
                    lease = item.with_name(item.name + ".lock")
                    # Legacy/unmanaged roots are never assumed inactive from
                    # mtime. Another live Console may still be writing them.
                    if not lease.exists():
                        continue
                    fd = self._lease_file(lease)
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        if time.time() - item.stat().st_mtime > 7 * 86400:
                            shutil.rmtree(item)
                            lease.unlink()
                        else:
                            candidates.append(item)
                    finally:
                        os.close(fd)
            # Retain <=1 finished owned root plus each live owner's <=128 MiB.
            # Legacy roots have no trustworthy live-owner record and are not
            # eligible for automatic deletion during this migration.
            for item in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[1:]:
                lease = item.with_name(item.name + ".lock")
                fd = self._lease_file(lease)
                try:
                    # Recheck ownership at deletion; a concurrent owner never
                    # loses its current exact snapshots to another process.
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    shutil.rmtree(item)
                    lease.unlink()
                except BlockingIOError:
                    pass
                finally:
                    os.close(fd)
            for lease in self.evidence.glob("session-*.lock"):
                if not re.fullmatch(r"session-[a-f0-9-]{36}\.lock", lease.name):
                    continue
                root = lease.with_suffix("")
                if root.exists() or time.time() - lease.stat().st_mtime <= 7 * 86400:
                    continue
                fd = self._lease_file(lease)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lease.unlink()
                except BlockingIOError:
                    pass
                finally:
                    os.close(fd)

    @staticmethod
    def _lease_file(path):
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("evidence_lease_symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ValueError("private_evidence_lease_required")
        return fd

    def lease(self, output):
        fd = self._lease_file(output.with_name(output.name + ".lock"))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def valid_url(url):
        parts = urlsplit(url)
        try:
            address = ipaddress.ip_address(parts.hostname)
            if address.version != 4 or not address.is_private or address.is_loopback:
                raise ValueError()
            if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
                raise ValueError()
            # Match the worker's exact RFC1918 scope (not IPv4 link-local/multicast).
            if not any(address in ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                raise ValueError()
            parts.port
            return True
        except (ValueError, TypeError):
            return False

    async def probe(self, url):
        if self.gate.locked():
            return {"status": "probe_busy", "writes_attempted": False}
        if not url or not self.available():
            return {"status": "pairing_required", "credential_available": self.available(), "writes_attempted": False}
        if not self.valid_url(url):
            return {"status": "unsupported_gateway_address", "writes_attempted": False}
        async with self.gate:
            try:
                self.prune()
                code, output = await run_bounded([
                    "node", "--max-old-space-size=192", str(ROOT / "workers/geek/read-only.mjs"),
                    "--url", url, "--passcode-file", str(self.secret.absolute()),
                    "--output", str(self.evidence.absolute())], timeout=125)
                report = json.loads(output)
                if code not in {0, 2} or report.get("writes_attempted") is not False:
                    raise ValueError()
                keys = {"status", "provider", "revision", "simulated", "writes_attempted", "firmware",
                        "graph_semantics", "device_count", "graph_count", "scope_count", "retention_days",
                        "sensitivity", "limitations", "started_at", "finished_at", "failure_phase", "failure_code"}
                return {k: v for k, v in report.items() if k in keys}
            except (ValueError, OSError, TimeoutError, TypeError):
                return {"status": "probe_unavailable", "writes_attempted": False}
