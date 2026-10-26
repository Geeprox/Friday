"""One explicit bounded camera verification; Core never imports native codecs."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .geek_daemon import private_file
from .media_contracts import MediaConnection, MediaProbeResult, NativeMediaConnection
from .recognition_consumer import NativeMetadataOwnerContext, SyntheticOwnerContext
from .recognition_contracts import FaceFrameProvenance, NativeFaceRequest, NativeFaceResult, RecognitionStatus

MAX_RSS = 256 * 1024 * 1024


async def stop_process(process, *, grace=1):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), grace)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.stdin:
        process.stdin.close()


def native_worker_command():
    project = Path(__file__).resolve().parents[1]
    return [str(project / "data/miloco-venv/bin/python"), "-B", "-I", str(project / "homeplane/native_media_worker.py")]


async def launch_media_worker(*, native):
    command = native_worker_command() if native else [sys.executable, "-m", "homeplane.media_worker"]
    return await asyncio.create_subprocess_exec(*command,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        env={**{k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ},
             "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}, limit=8192)


async def read_worker_rss(process):
    check = await asyncio.create_subprocess_exec("/bin/ps", "-p", str(process.pid), "-o", "rss=",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(check.communicate(), 1)
        if len(output) < 32 and output.strip().isdigit():
            return int(output.strip()) * 1024
        if process.returncode is not None:
            return 0
        raise ValueError("rss_unavailable")
    finally:
        if check.returncode is None:
            check.kill()
            await check.wait()


class MediaProbe:
    def __init__(self, settings, *, launcher=None, rss_reader=None, on_result=None, clock=time.monotonic, simulated=False):
        self.settings = settings
        self.launcher = launcher or self._launch
        self.rss_reader = rss_reader or self._rss
        self.on_result = on_result
        self.clock = clock
        self.simulated = simulated
        self.gate = asyncio.Lock()
        self.task = self.process = None
        self.generation = 0
        self.expires = None
        self.binding = None
        self.report = MediaProbeResult(simulated=self.simulated, provider=self.settings.config.camera_media_source)

    async def _launch(self):
        return await launch_media_worker(native=self.report.provider == "miot_sdk")

    async def _rss(self, process):
        return await read_worker_rss(process)

    def source_binding(self):
        paths = [self.settings.token_path]
        if self.settings.config.camera_media_source == "miot_sdk":
            db = Path(__file__).resolve().parents[1] / "data/miloco/miloco.db"
            paths.extend([db, Path(str(db) + "-wal")])
        stamps = []
        for path in paths:
            try:
                info = path.stat()
                stamps.append((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size))
            except OSError:
                stamps.append(None)
        return (self.settings.config.camera_media_source, self.settings.config.camera_service_url,
                self.settings.config.camera_id, tuple(stamps))

    def status(self):
        if self.report.status not in {"not_requested", "not_configured", "stopped", "discarded_configuration_changed"} and self.binding != self.source_binding():
            return MediaProbeResult(status="discarded_configuration_changed", simulated=self.simulated,
                provider=self.settings.config.camera_media_source)
        if self.expires is not None and self.clock() >= self.expires:
            return self.report.model_copy(update={"status": "expired"})
        return self.report.model_copy(deep=True)

    async def start(self, request):
        if self.gate.locked():
            raise ValueError("media_probe_busy")
        async with self.gate:
            return self._start(request)

    def _start(self, request):
        if self.task and not self.task.done():
            raise ValueError("media_probe_busy")
        self.binding = self.source_binding()
        self.expires = None
        provider = self.settings.config.camera_media_source
        if not self.settings.config.camera_id or (provider == "miloco_service" and not self.settings.config.camera_service_url):
            self.report = MediaProbeResult(status="not_configured", simulated=self.simulated, provider=provider)
            return self.status()
        try:
            if provider == "miot_sdk":
                connection = NativeMediaConnection(camera_id=self.settings.config.camera_id, seconds=request.seconds)
            else:
                token = json.loads(private_file(self.settings.token_path.absolute(), 65536)).get("server", {}).get("token")
                connection = MediaConnection(origin=self.settings.config.camera_service_url,
                    camera_id=self.settings.config.camera_id, token=token, seconds=request.seconds)
        except (OSError, ValueError, TypeError, AttributeError):
            self.report = MediaProbeResult(status="failed", failure="service_login_required", simulated=self.simulated, provider=provider)
            return self.status()
        self.generation += 1
        probe_id = str(uuid4())
        self.report = MediaProbeResult(status="running", probe_id=probe_id, simulated=self.simulated, provider=provider)
        self.task = asyncio.create_task(self._run(connection, self.generation, probe_id, self.binding))
        return self.status()

    async def _run(self, config, generation, probe_id, binding):
        process = monitor = None
        failure = None
        result = None
        native = isinstance(config, NativeMediaConnection)
        provider = "miot_sdk" if native else "miloco_service"
        async def memory_guard():
            while process.returncode is None:
                rss = await self.rss_reader(process)
                if rss > MAX_RSS:
                    raise MemoryError("owned_worker_rss_limit")
                await asyncio.sleep(.2)
        try:
            launch = asyncio.create_task(self.launcher())
            try:
                process = await asyncio.wait_for(asyncio.shield(launch), 3)
            except (asyncio.CancelledError, TimeoutError):
                def clean_late(task):
                    if not task.cancelled() and task.exception() is None:
                        asyncio.create_task(stop_process(task.result(), grace=3.5 if native else 1))
                launch.add_done_callback(clean_late)
                raise
            self.process = process
            payload = config.model_dump(mode="json", exclude={"token"})
            if not native:
                payload["token"] = config.token.get_secret_value()
            encoded = json.dumps(payload).encode()
            if len(encoded) > 16384:
                raise ValueError("request_budget")
            monitor = asyncio.create_task(memory_guard())
            async def exchange():
                process.stdin.write(encoded)
                await process.stdin.drain()
                process.stdin.close()
                output = await process.stdout.read(8193)
                if len(output) > 8192:
                    raise ValueError("response_budget")
                await process.wait()
                if process.returncode:
                    raise ValueError("worker_failed")
                parsed = MediaProbeResult.model_validate_json(output)
                if (parsed.provider != provider or parsed.status not in {"decoded_sync_unknown", "failed"} or
                        parsed.probe_id is not None or parsed.checked_at is not None):
                    raise ValueError("worker_result_required")
                return parsed
            exchange_task = asyncio.create_task(exchange())
            try:
                async with asyncio.timeout(config.seconds + (16 if native else 6)):
                    done, _ = await asyncio.wait({exchange_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
                    if monitor in done:
                        monitor.result()
                    result = await exchange_task
            finally:
                exchange_task.cancel()
                await asyncio.gather(exchange_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            failure = "worker_timeout"
        except MemoryError:
            failure = "worker_memory_limit"
        except (OSError, ValueError):
            failure = "worker_failed"
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if process:
                await stop_process(process, grace=3.5 if native else 1)
            if self.process is process:
                self.process = None
        if generation != self.generation:
            return
        if binding != self.source_binding():
            self.report = MediaProbeResult(status="discarded_configuration_changed", simulated=self.simulated, provider=provider)
            return
        result = result or MediaProbeResult(status="failed", failure=failure or "worker_failed", provider=provider)
        now = datetime.now(UTC)
        self.report = result.model_copy(update={"probe_id": probe_id, "checked_at": now.isoformat(),
            "valid_until": (now + timedelta(seconds=60)).isoformat(), "simulated": self.simulated})
        self.expires = self.clock() + 60
        if self.on_result:
            self.on_result(self.report.model_copy(deep=True))

    async def stop(self, *, changed=False):
        async with self.gate:
            self.generation += 1
            task, self.task = self.task, None
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.report = MediaProbeResult(status="discarded_configuration_changed" if changed else "stopped", simulated=self.simulated,
                provider=self.settings.config.camera_media_source)
            self.expires = None


class RecognitionProbe:
    """Separate finite IPC consumer. No production activation or public start API."""
    def __init__(self, settings, *, launcher=None, rss_reader=None, clock=time.monotonic, people_catalog=None):
        self.settings = settings
        self.people_catalog = people_catalog
        self._people_owner = self.feature_message = None
        self.launcher = launcher
        self.rss_reader = rss_reader or read_worker_rss
        self.clock = clock
        self.generation = 0
        self.task = self.process = None
        self.report = RecognitionStatus()
        self.result = None
        self.session = None
        self.expected_provenance = None
        self.revision = settings.revision

    def _private_deadline(self):
        deadline = self.session.deadline_monotonic
        if isinstance(self.result, NativeFaceResult) and self.result.window_started_monotonic is not None:
            deadline = min(deadline, self.result.window_started_monotonic+self.session.seconds)
        return deadline

    def status(self):
        if self.session is not None and self.revision != self.settings.revision:
            return RecognitionStatus(state="unavailable", reason="configuration_changed", simulated=self.session.simulated)
        if self.session is not None and self.clock() >= self._private_deadline():
            return RecognitionStatus(state="expired", reason="expired", simulated=self.session.simulated)
        return self.report

    def private_result(self, context):
        if (not isinstance(context, (SyntheticOwnerContext, NativeMetadataOwnerContext)) or self.session is None
                or not context.admits(self.session) or self.revision != self.settings.revision
                or self.clock() >= self._private_deadline()):
            return None
        return self.result

    async def start_owner(self, context):
        # Private source-only synthetic seam. Never construct context from JSON.
        if not isinstance(context, (SyntheticOwnerContext, NativeMetadataOwnerContext)) or not context.admits(context.session):
            self.report = RecognitionStatus(state="unavailable", reason="owner_admission_unavailable")
            return self.status()
        if self.task and not self.task.done():
            raise ValueError("recognition_busy")
        session = context.session
        if (session.generation != self.generation + 1 or session.camera_id != self.settings.config.camera_id
                or self.settings.config.camera_media_source != "miot_sdk"):
            self.report = RecognitionStatus(state="unavailable", reason="owner_binding_mismatch", simulated=session.simulated)
            return self.status()
        remaining = session.deadline_monotonic - self.clock()
        if not 0 < remaining <= session.seconds + 16:
            self.report = RecognitionStatus(state="unavailable", reason="owner_binding_mismatch", simulated=session.simulated)
            return self.status()
        people_owner = context._people_owner
        if people_owner is not None:
            from .people_feature_bridge import SyntheticPeopleBatchOwner
            if (not isinstance(people_owner, SyntheticPeopleBatchOwner) or self.people_catalog is None
                    or not people_owner.admits(session, context.provenance)
                    or self.people_catalog.page().revision != people_owner.base_people_revision):
                self.report = RecognitionStatus(state="unavailable", reason="owner_binding_mismatch", simulated=session.simulated)
                return self.status()
        self.generation = session.generation
        self.session, self.revision = session, self.settings.revision
        self.expected_provenance = context.provenance
        self._people_owner = people_owner
        self.result = self.feature_message = None
        self.report = RecognitionStatus(state="running", reason="running", simulated=session.simulated)
        self.task = asyncio.create_task(self._run(session, self.revision))
        return self.status()

    async def _run(self, session, revision):
        process = monitor = exchange_task = None
        result = None
        reason = "worker_failed"
        async def memory_guard():
            while process.returncode is None:
                if self.generation != session.generation or revision != self.settings.revision:
                    if self._people_owner is not None:
                        self._people_owner.revoke()
                    raise ValueError("feature_scope_revoked")
                if await self.rss_reader(process) > MAX_RSS:
                    raise MemoryError("owned_worker_rss_limit")
                await asyncio.sleep(.2)
        try:
            launch = asyncio.create_task(self.launcher() if self.launcher else launch_media_worker(native=True))
            try:
                process = await asyncio.wait_for(asyncio.shield(launch), min(3, session.deadline_monotonic - self.clock()))
            except (asyncio.CancelledError, TimeoutError):
                def clean_late(task):
                    if not task.cancelled() and task.exception() is None:
                        asyncio.create_task(stop_process(task.result(), grace=3.5))
                launch.add_done_callback(clean_late)
                raise
            self.process = process
            monitor = asyncio.create_task(memory_guard())
            async def exchange():
                request = (NativeFaceRequest(schema_version=3, session=session, provenance=self.expected_provenance)
                    if isinstance(self.expected_provenance, FaceFrameProvenance) else session)
                process.stdin.write(request.model_dump_json().encode())
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while True:
                    chunk = await process.stdout.read(8193 - len(output))
                    output.extend(chunk)
                    if len(output) > 8192:
                        raise ValueError("recognition_worker_protocol")
                    if not chunk:
                        break
                await process.wait()
                if process.returncode != 0 or len(output) > 8192:
                    raise ValueError("recognition_worker_protocol")
                from .recognition_feature_protocol import parse_worker_output
                return parse_worker_output(bytes(output), session, self.expected_provenance,
                    allow_features=self._people_owner is not None)
            exchange_task = asyncio.create_task(exchange())
            async with asyncio.timeout(max(0, session.deadline_monotonic - self.clock())):
                done, _ = await asyncio.wait({exchange_task, monitor}, return_when=asyncio.FIRST_COMPLETED)
                if monitor in done:
                    await monitor
                result = await exchange_task
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            reason = "worker_timeout"
        except MemoryError:
            reason = "worker_memory_limit"
        except (OSError, ValueError):
            pass
        finally:
            for task in (monitor, exchange_task):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (monitor, exchange_task) if t), return_exceptions=True)
            if process:
                await stop_process(process, grace=3.5)
                if self.process is process:
                    self.process = None
        # Stop/edit invalidates before cancellation; late results never publish.
        if self.generation != session.generation or revision != self.settings.revision:
            if self._people_owner is not None:
                self._people_owner.revoke()
            return
        if (result is not None and isinstance(result.result, NativeFaceResult)
                and result.result.window_started_monotonic is not None
                and self.clock() >= min(session.deadline_monotonic,
                    result.result.window_started_monotonic+session.seconds)):
            self.result = self.feature_message = None
            self.report = RecognitionStatus(state="expired", reason="expired", simulated=session.simulated)
            return
        if result is not None and result.features is not None:
            try:
                self._people_owner.apply(self.people_catalog, result.features)
                self.feature_message = result.features
            except ValueError:
                result = None
        self.result = result.result if result is not None else None
        self.report = self.result.status if self.result else RecognitionStatus(state="unavailable", reason=reason,
            simulated=session.simulated)

    async def stop(self):
        self.generation += 1
        if self._people_owner is not None:
            self._people_owner.revoke()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = self.process = self.session = self.expected_provenance = self.result = None
        self.expected_provenance = None
        self._people_owner = self.feature_message = None
        self.revision = self.settings.revision
        self.report = RecognitionStatus()
