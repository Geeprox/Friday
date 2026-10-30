"""Owned, short-lived protected previews; source media is never copied or served."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import secrets
import stat
import sys
from itertools import islice
from pathlib import Path

from .runtime_log import stage_span

MAX_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024


def model_configuration(path):
    """Read an explicit owner-only local setting; never open model weights here."""
    from .archive_privacy import MODEL_SHA256

    if path is None:
        return None, "unconfigured"
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not 0 < info.st_size <= 8192
        ):
            raise ValueError("archive_privacy_unavailable")
        with os.fdopen(os.dup(fd), "rb") as reader:
            raw = reader.read(8193)
        if len(raw) > 8192:
            raise ValueError("archive_privacy_unavailable")
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("archive_privacy_unavailable")
        if value.get("mode") == "full_frame" and set(value) == {"version", "mode"}:
            return None, "full_frame"
        if (
            set(value) != {"version", "mode", "model_path", "model_sha256"}
            or value["mode"] != "local_model"
            or value["model_sha256"] != MODEL_SHA256
            or not isinstance(value["model_path"], str)
            or len(value["model_path"].encode()) > 4096
            or any(ord(c) < 32 for c in value["model_path"])
        ):
            raise ValueError("archive_privacy_unavailable")
        model = Path(value["model_path"])
        if not model.is_absolute() or ".." in model.parts:
            raise ValueError("archive_privacy_unavailable")
        return model, "local_model"
    except FileNotFoundError:
        return None, "unconfigured"
    except (OSError, ValueError, TypeError, UnicodeError):
        return None, "invalid_configuration"
    finally:
        if fd is not None:
            os.close(fd)


def command(module, arguments, *, python=None):
    if module not in {"homeplane.archive_privacy_worker", "homeplane.archive_privacy_range"}:
        raise ValueError("archive_privacy_unavailable")
    python = python or sys.executable
    source = Path(__file__).resolve().parent.parent
    paths = [str(source)]
    vendor = source.parent / "vendor"
    if vendor.is_dir():
        paths.insert(0, str(vendor))
    bootstrap = (
        "import sys,runpy;"
        f"sys.path[:0]={json.dumps(paths)};"
        f"sys.argv={json.dumps([module, *arguments])};"
        f"runpy.run_module({json.dumps(module)},run_name='__main__')"
    )
    return [str(python), "-I", "-B", "-c", bootstrap]


def private_folder(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("archive_privacy_unavailable")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    from .archive_worker import root_handle

    fd = root_handle(str(path.absolute()))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("archive_privacy_unavailable")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


class PrivacyPreviews:
    def __init__(self, directory, *, model=None, config=None, launcher=None, python=None):
        self.parent = Path(directory)
        # A detector is a trusted host input. Never discover or download weights
        # implicitly: unconfigured installations produce whole-frame protection.
        configured, self.configuration = model_configuration(config)
        self.model = Path(model) if model is not None else configured
        self.launcher = launcher or asyncio.create_subprocess_exec
        self.python = python
        self.directory = None
        self.lock_fd = None
        self.files = set()
        self.process = self.launch_task = None
        self.pending_output = None
        self.gate = asyncio.Lock()
        self.closed = False

    def configuration_report(self):
        return {
            "mode": "local_model" if self.model is not None else "full_frame",
            "detector_configured": self.model is not None,
            "configuration": self.configuration,
            "detector_validation": "not_established_by_configuration",
            "configuration_applies": "current_service_lifetime",
        }

    def prune_abandoned(self):
        for path in islice(self.parent.iterdir(), 64):
            if not re.fullmatch(r"preview-[a-f0-9]{32}", path.name) or path.is_symlink() or not path.is_dir():
                continue
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                continue
            fd = None
            try:
                fd = os.open(path / ".owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    continue
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                items = list(islice(path.iterdir(), 4))
                if len(items) > 3 or any(
                    p.is_symlink()
                    or not p.is_file()
                    or p.stat().st_uid != os.getuid()
                    or (p.name != ".owner.lock" and not re.fullmatch(r"[a-f0-9]{32}\.mp4", p.name))
                    for p in items
                ):
                    continue
                for item in items:
                    item.unlink()
                path.rmdir()
            except (OSError, ValueError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)

    async def terminate(self, process):
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def stop(self):
        launch = self.launch_task
        output = self.pending_output
        if launch is not None:
            try:
                process = await asyncio.wait_for(asyncio.shield(launch), 12)
                await self.terminate(process)
            except (TimeoutError, OSError):

                def late(task):
                    if not task.cancelled() and task.exception() is None:

                        async def discard():
                            await self.terminate(task.result())
                            self._unlink(output)

                        asyncio.create_task(discard())

                launch.add_done_callback(late)
        await self.terminate(self.process)
        self.process = self.launch_task = None

    def _unlink(self, name):
        if name is None or self.directory is None or not re.fullmatch(r"[a-f0-9]{32}\.mp4", name):
            return
        from .archive_worker import root_handle

        try:
            fd = root_handle(str(self.directory.absolute()))
            try:
                os.unlink(name, dir_fd=fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def release(self, name):
        if name not in self.files:
            return
        self.files.remove(name)
        self._unlink(name)

    def owns(self, binding):
        """Metadata-only admission for an already completed owned derivative."""
        return bool(
            not self.closed
            and self.directory is not None
            and binding.get("path") in self.files
            and binding.get("root") == str(self.directory.absolute())
            and binding.get("privacy") in {"local_person_mosaic", "local_full_frame_mosaic"}
        )

    async def prepare(self, binding):
        with stage_span("archive.privacy"):
            if self.closed:
                raise ValueError("archive_privacy_unavailable")
            if self.gate.locked() or len(self.files) >= 2:
                raise ValueError("archive_privacy_busy")
            async with self.gate:
                return await self._prepare(binding)

    async def _prepare(self, binding):
        if not 0 < binding["size_bytes"] <= MAX_SOURCE_BYTES or not 0 < binding["info"]["duration_seconds"] <= 600:
            raise ValueError("archive_privacy_budget")
        from .archive_privacy_worker import preview_window
        window = preview_window(binding.get("preview_window"))
        if window is not None and window["start_seconds"] + window["duration_seconds"] > binding["info"]["duration_seconds"] + 0.001:
            raise ValueError("archive_privacy_budget")
        if self.directory is None:
            private_folder(self.parent)
            self.prune_abandoned()
            self.directory = self.parent / ("preview-" + secrets.token_hex(16))
            self.directory.mkdir(mode=0o700)
            self.lock_fd = os.open(
                self.directory / ".owner.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = secrets.token_hex(16) + ".mp4"
        self.files.add(name)
        self.pending_output = name
        try:
            args = command(
                "homeplane.archive_privacy_worker",
                ["--root", binding["root"], "--cache", str(self.directory.absolute())],
                python=self.python,
            )
            environment = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
            self.launch_task = asyncio.create_task(
                self.launcher(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                    limit=8192,
                )
            )
            self.process = await asyncio.wait_for(asyncio.shield(self.launch_task), 10)
            process = self.process
            request = {
                "path": binding["path"],
                "signature": binding["signature"],
                "output": name,
                "model": str(self.model.absolute()) if self.model is not None else None,
            }
            if window is not None:
                request["window"] = window
            async with asyncio.timeout(180):
                process.stdin.write(json.dumps(request).encode())
                await process.stdin.drain()
                process.stdin.close()
                raw = await process.stdout.read(4097)
                if len(raw) > 4096:
                    raise ValueError("archive_privacy_unavailable")
                await process.wait()
            result = json.loads(raw)
            if (
                self.closed
                or process.returncode
                or result.get("status") != "protected"
                or result.get("privacy") not in {"local_person_mosaic", "local_full_frame_mosaic"}
                or result.get("privacy_degraded_reason")
                not in {"none", "detector_unconfigured", "detector_unavailable", "analysis_uncertain"}
                or not isinstance(result.get("signature"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", result["signature"])
                or type(result.get("size_bytes")) is not int
                or not 0 < result["size_bytes"] <= MAX_BYTES
                or result.get("original_modified") is not False
                or result.get("original_copied") is not False
            ):
                raise ValueError("archive_privacy_unavailable")
            degraded = result["privacy_degraded_reason"]
            if self.configuration == "invalid_configuration" and degraded == "detector_unconfigured":
                degraded = "detector_unavailable"
            return {
                "root": str(self.directory.absolute()),
                "path": name,
                "signature": result["signature"],
                "size_bytes": result["size_bytes"],
                "privacy": result["privacy"],
                "privacy_degraded_reason": degraded,
                "source_root": binding["root"],
                "source_path": binding["path"],
                "source_signature": binding["signature"],
            }
        except BaseException:
            # Cancellation of the request must still reap the owned renderer
            # before any partial file can outlive this preparation operation.
            cleanup = asyncio.create_task(self.stop())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            finally:
                self.release(name)
            raise
        finally:
            # A returned preview is complete; no rendering process outlives it.
            if self.process is not None and self.process.returncode is not None:
                self.process = self.launch_task = None
            self.pending_output = None

    async def close(self):
        self.closed = True
        await self.stop()
        for name in list(self.files):
            self.release(name)
        if self.directory is not None:
            try:
                (self.directory / ".owner.lock").unlink(missing_ok=True)
                self.directory.rmdir()
            except OSError:
                pass
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
