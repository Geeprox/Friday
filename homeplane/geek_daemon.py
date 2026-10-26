"""User-owned background read-only connector, independent of Console/Core.

Private Unix socket, typed operations, no device/Graph/model writes.
Only the exact saved endpoint and local credential version can be restored.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import stat
import tempfile
import time
from pathlib import Path

from .geek_background_contracts import BackgroundRequest, ConnectionIntent
from .geek_session import GeekSession
from .geek_setup import GeekSetup
from .settings import SettingsConfig
from .runtime_log import RuntimeLog

SOCKET_NAME = "geek-background.sock"
MAX_RESPONSE = 128 * 1024


def private_file(path, limit=2048):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("private_path_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError("private_file_required")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("private_file_budget")
        return data
    finally:
        os.close(fd)


def private_directory(directory):
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("private_directory_required")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("private_directory_required")
    os.chmod(directory, 0o700)


def atomic_private(path, body):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("private_path_required")
    fd, temporary = tempfile.mkstemp(prefix=".geek-background-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GeekDaemon:
    def __init__(self, directory, *, launcher=None, clock=time.monotonic):
        self.directory = Path(directory)
        self.setup = GeekSetup(self.directory)
        self.session = GeekSession(self.setup, launcher=launcher, clock=clock)
        self.intent_path = self.directory / "geek-connection-intent.json"
        self.socket_path = self.directory / SOCKET_NAME
        self.intent = ConnectionIntent()
        self.clock = clock
        self.gate = asyncio.Lock()
        self.clients = 0
        self.server = self.monitor = None
        self.lock_fd = None
        self.lock_acquired = self.socket_owned = self.stopping = False
        self.handlers = set()
        self.next_retry = 0
        self.gap = False
        self.runtime_log = RuntimeLog(self.directory / "geek-runtime")
        self.recovery = None

    def credential_stamp(self):
        if not self.setup.available():
            return None
        info = self.setup.secret.stat()
        # Metadata only, no PIN hash (low-entropy hashes are not safe exports).
        return f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"

    def saved_url(self):
        # Reload actual private settings; a changed/corrupt endpoint never keeps
        # using the previously saved credential destination in the background.
        stored = json.loads(private_file(self.directory / "settings.json", 16384))
        if not isinstance(stored, dict) or not isinstance(stored.get("config"), dict):
            raise ValueError("private_settings_required")
        return SettingsConfig.model_validate(stored["config"]).geek_url

    def persist(self):
        atomic_private(self.intent_path, self.intent.model_dump_json() + "\n")

    def status(self):
        report = self.session.status()
        return {**report, "connection_host": "background_service",
                "background_service_live": True, "keep_connected": self.intent.keep_connected,
                "gap": self.gap or report.get("gap", False)}

    @staticmethod
    def transport_failure(report):
        # A typed EOF/crash is recoverable; malformed worker output remains latched.
        if report.get("status") == "worker_stopped" and not report.get("failure_code"):
            return True
        if report.get("failure_phase") == "authenticate":
            # A generic close may be a policy rejection; only explicit transport errors recover.
            return report.get("failure_code") in {"connection_refused", "network_unreachable", "connection_timeout"}
        return report.get("failure_phase") in {"websocket_connect", "inventory"} and report.get("failure_code") in {
            "connection_refused", "network_unreachable", "connection_timeout", "gateway_closed_connection",
            "session_not_connected", "readonly_request_timeout",
        }

    def latch_failure(self):
        report = self.session.status()
        if not self.intent.keep_connected or self.intent.authentication_blocked or self.intent.automatic_retry_blocked or report["status"] in {"starting", "online", "reconnecting", "stale"}:
            return
        if self.transport_failure(report):
            return
        if report.get("failure_phase") == "authenticate":
            self.intent.authentication_blocked = True
        else:
            self.intent.automatic_retry_blocked = True
        self.intent.failure_status = report["status"]
        self.intent.failure_phase = report.get("failure_phase")
        self.intent.failure_code = report.get("failure_code")
        self.persist()

    def end_recovery(self, result, code="none"):
        if self.recovery is not None:
            correlation, started = self.recovery
            self.runtime_log.record("infrastructure.reconcile", "result", result=result, code=code,
                                    correlation=correlation, elapsed_ms=(self.clock() - started) * 1000)
            self.recovery = None

    async def connect(self, url):
        if not url or url != self.saved_url() or not self.setup.valid_url(url):
            raise ValueError("saved_endpoint_required")
        candidate = ConnectionIntent(keep_connected=True, url=url, credential_stamp=self.credential_stamp())
        if candidate.credential_stamp is None:
            raise ValueError("credential_required")
        # Explicit requests clear the authentication latch. Startup alone never
        # retries a credential version already rejected by authentication.
        old = self.intent
        self.intent = candidate
        try:
            self.persist()
        except (OSError, ValueError):
            self.intent = old
            raise
        await self.session.connect(url)
        self.next_retry = self.clock() + 300
        return self.status()

    async def disconnect(self):
        self.end_recovery("cancelled", "disconnected")
        self.intent = ConnectionIntent()
        try:
            self.persist()
        finally:
            await self.session.close()
            self.gap = False
        return self.status()

    async def tick(self):
        async with self.gate:
            if not self.intent.keep_connected:
                return
            try:
                matches = self.intent.url == self.saved_url() and self.intent.credential_stamp == self.credential_stamp()
            except (OSError, ValueError):
                matches = False
            if not matches:
                # Changing endpoint/PIN out of band must close the old owner,
                # not silently move the saved credential to the new endpoint.
                await self.disconnect()
                return
            report = self.session.status()
            if report["status"] == "online":
                self.end_recovery("ok")
                self.next_retry = 0
            elif not self.session.live() and report["status"] not in {"starting", "reconnecting", "stale"}:
                code = "transport_error" if self.transport_failure(report) else "authentication_rejected" if report.get("failure_phase") == "authenticate" else "internal_error"
                self.end_recovery("unavailable", code)
            self.latch_failure()
            if self.intent.authentication_blocked or self.intent.automatic_retry_blocked or self.session.live():
                return
            if self.transport_failure(report):
                if not self.next_retry:
                    self.next_retry = self.clock() + 300
                if self.clock() < self.next_retry:
                    return
                # Five-minute backoff after transport failure; a subsequent
                # rejected handshake is latched instead of repeatedly using PIN.
                self.next_retry = self.clock() + 300
                self.gap = True
                self.recovery = (self.runtime_log.correlate(), self.clock())
                self.runtime_log.record("infrastructure.reconcile", "start", correlation=self.recovery[0])
                try:
                    await self.session.connect(self.intent.url)
                except asyncio.CancelledError:
                    self.end_recovery("cancelled", "disconnected")
                    raise

    async def _monitor(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self.tick()
                self.setup.prune(active=self.session.inventory_root)
            except (OSError, ValueError):
                # Persistence/configuration failure cannot send new requests.
                await self.session.close()

    async def start(self):
        private_directory(self.directory)
        path = self.directory / ".geek-background.lock"
        self.lock_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(self.lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private_lock_required")
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock_acquired = True
        if self.socket_path.exists() or self.socket_path.is_symlink():
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("owned_socket_required")
            self.socket_path.unlink()  # Only after acquiring the exclusive lock.
        if self.intent_path.exists():
            self.intent = ConnectionIntent.model_validate_json(private_file(self.intent_path))
        if self.intent.authentication_blocked or self.intent.automatic_retry_blocked:
            self.session.report = {"status": self.intent.failure_status or "blocked"}
            for key in ("failure_phase", "failure_code"):
                if getattr(self.intent, key) is not None:
                    self.session.report[key] = getattr(self.intent, key)
        self.server = await asyncio.start_unix_server(self.handle, path=str(self.socket_path), limit=2048)
        self.socket_owned = True
        self.socket_path.chmod(0o600)
        if self.intent.keep_connected and not self.intent.authentication_blocked and not self.intent.automatic_retry_blocked:
            try:
                if self.intent.url == self.saved_url() and self.intent.credential_stamp == self.credential_stamp():
                    self.gap = True
                    await self.session.connect(self.intent.url)
                    self.next_retry = self.clock() + 300
                else:
                    await self.disconnect()
            except (OSError, ValueError):
                await self.disconnect()
        self.monitor = asyncio.create_task(self._monitor())

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        admitted = self.clients < 8 and not self.stopping
        if admitted:
            self.clients += 1
        try:
            if not admitted:
                return
            line = await asyncio.wait_for(reader.readline(), 2)
            if len(line) > 1024 or not line.endswith(b"\n"):
                raise ValueError("request_budget")
            request = BackgroundRequest.model_validate_json(line)
            if request.operation != "connect" and request.url is not None:
                raise ValueError("unexpected_url")
            if request.operation in {"connect", "disconnect"}:
                async with asyncio.timeout(12):
                    async with self.gate:
                        result = await self.connect(request.url) if request.operation == "connect" else await self.disconnect()
            elif request.operation == "diagnostics":
                result = self.runtime_log.export()
            elif request.operation == "inventory":
                result = (await self.session.inventory()).model_dump(mode="json")
                status = self.status()
                if result["status"] == "ready" and (status["status"] != "online" or status.get("fingerprint") != result.get("fingerprint")):
                    from .geek_inventory import GeekInventoryView
                    result = GeekInventoryView(session=status, status="stale").model_dump(mode="json")
                else:
                    result["session"] = status
            else:
                result = self.status()
            body = json.dumps(result, ensure_ascii=False).encode() + b"\n"
            if len(body) > MAX_RESPONSE:
                raise ValueError("response_budget")
            writer.write(body)
            await asyncio.wait_for(writer.drain(), 2)
        except (OSError, ValueError, TimeoutError):
            # Fixed error, never an arbitrary exception containing a PIN/raw RPC.
            try:
                writer.write(b'{"error":"background_request_failed"}\n')
                await asyncio.wait_for(writer.drain(), 2)
            except (OSError, TimeoutError):
                pass
        finally:
            if admitted:
                self.clients -= 1
            self.handlers.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def close(self):
        self.end_recovery("cancelled", "disconnected")
        self.stopping = True
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        active = list(self.handlers)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        # Service shutdown ends socket ownership, but retains ONLY readonly
        # intent. No action window, device operation or inference is persisted.
        try:
            try:
                self.latch_failure()
            finally:
                await self.session.close()
        finally:
            if self.lock_fd is not None:
                if self.socket_owned:
                    self.socket_path.unlink(missing_ok=True)
                os.close(self.lock_fd)
                self.lock_fd = None


async def run(directory):
    daemon = GeekDaemon(directory)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await daemon.start()
        await stopped.wait()
    finally:
        await daemon.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.directory))


if __name__ == "__main__":
    main()
