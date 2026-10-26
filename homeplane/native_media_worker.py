"""Bounded use of the already installed MIoT SDK; never import Miloco producers.

No package is installed or copied here. This conditional local adapter is not a
license grant. It reads an existing login without refreshing or deleting it.
The entire SDK and its native decoders live in this disposable worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.abc
import importlib.metadata
import json
import logging
import os
import platform
import resource
import signal
import socket
import sqlite3
import stat
import sys
import time
from pathlib import Path

# Executed as a fixed absolute script under -I, never via PYTHONPATH.
PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from homeplane.media_contracts import MediaProbeResult, NativeMediaConnection  # noqa: E402
from homeplane.recognition_consumer import (  # noqa: E402
    _OWNED_RECOGNITION_CALLS,
    NativeMetadataOwnerContext,
    RecognitionConsumer,
    SyntheticOwnerContext,
)
from homeplane.recognition_contracts import (  # noqa: E402
    NativeFaceRequest,
    NativeFaceResult,
    RecognitionResult,
    RecognitionSession,
    RecognitionStatus,
)

SDK_HOME = PROJECT / "data/miloco-venv"
DB = PROJECT / "data/miloco/miloco.db"
BLOCKED_IMPORTS = frozenset({"miloco", "miot.client", "miot.mips_cloud", "torch", "transformers", "mlx", "onnxruntime", "llama_cpp",
    "openai", "anthropic", "friday", "homeplane.arl", "homeplane.decision", "homeplane.decision_providers"})
# Keep the full SDK graph outside Task/coroutine frames. asyncio shutdown can
# cancel a Task without stopping its executor call; GC must not run SDK __del__
# on that graph. Unreleased sessions require the worker's no-finalization exit.
_NATIVE_SESSIONS = {}


class NativeSessionOwnership:
    """Process-owned lifetime, released only after proven orderly cleanup."""
    def __init__(self, client):
        self.client = client
        self.instance = None
        _NATIVE_SESSIONS[id(self)] = self

    def release(self):
        _NATIVE_SESSIONS.pop(id(self), None)
        self.client = self.instance = None


class NativeCleanupUncertain(Exception):
    """An asyncio task's cancellation is not native completion evidence."""


class ProducerImportFence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in BLOCKED_IMPORTS):
            raise ImportError("media_worker_producer_import_denied")
        return None


def qualify_sdk(home=SDK_HOME):
    """Pin installed APIs/assets before importing the conditional dependency."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    root = home / "lib/python3.12/site-packages/miot"
    try:
        manifest = json.loads((PROJECT / "homeplane/native_media_sdk.json").read_text())
        # Inspect metadata without loading any SDK/module/native library.
        versions = [d.version for d in importlib.metadata.distributions(path=[str(root.parent)])
                    if d.metadata.get("Name", "").lower() == "miloco"]
        if versions != [manifest["version"]] or root.is_symlink():
            return False
        actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and
                  (p.suffix in {".py", ".yaml", ".json"} or "libs/darwin/arm64" in str(p))}
        if actual != set(manifest["files"]):
            return False
        for relative, expected in manifest["files"].items():
            path = root / relative
            if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_size > 64 * 1024 * 1024:
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def read_login(path=DB, *, now=time.time):
    """Fixed-key, bounded, query-only SQLite. No migrations/init/unbind/refresh."""
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or
            any(p.is_symlink() for p in (path, *path.parents)) or info.st_size > 32 * 1024 * 1024):
        raise ValueError("account_login_required")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)
    try:
        connection.execute("pragma query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() - began > .5), 100)
        began = time.monotonic()
        rows = connection.execute("select key,value from kv where key in (?,?) and length(value)<=65536",
            ("MIOT_TOKEN_INFO_KEY", "DEVICE_UUID_KEY")).fetchall()
        if len(rows) != 2:
            raise ValueError("account_login_required")
        values = dict(rows)
        login = json.loads(values["MIOT_TOKEN_INFO_KEY"])
        if not isinstance(login, dict) or not isinstance(login.get("access_token"), str) or not login["access_token"]:
            raise ValueError("account_login_required")
        if type(login.get("expires_ts")) is not int or login["expires_ts"] <= now() + 30:
            raise ValueError("account_token_expired")
        return values["DEVICE_UUID_KEY"], login
    finally:
        connection.close()


class NativeCounter:
    """Diagnostic counters, with a separate optional admitted callback consumer."""
    def __init__(self, camera_id, *, clock=time.monotonic, _recognition=None):
        self.camera_id = camera_id
        self._recognition = _recognition
        self.clock = clock
        self.started = clock()
        self.active = True
        self.failure = None
        self.phase = "inventory"
        self.failed = asyncio.Event()
        self.first_packet = asyncio.Event()
        self.last_timestamp = {}
        self.report = MediaProbeResult(provider="miot_sdk", alignment_reason="sdk_source_timestamps_clock_domain_unverified")

    def reject(self, code, *, phase=None):
        self.failure = self.failure or code
        self.report.failure_phase = self.report.failure_phase or phase or self.phase
        self.active = False
        self.failed.set()
        self.first_packet.set()  # Wake startup too; caller still checks failure.

    def accepts(self, did, channel):
        return self.active and did == self.camera_id and channel == 0

    async def status(self, did, status):
        if self.accepts(did, 0) and getattr(status, "name", None) == "ERROR":
            self.reject("stream_error")
            return
        if self.accepts(did, 0) and self.report.video.packets + self.report.audio.packets:
            if getattr(status, "name", None) == "DISCONNECTED":
                self.reject("stream_closed")

    def raw(self, kind, did, data, ts, seq, channel):
        if not self.accepts(did, channel):
            return False
        result = getattr(self.report, kind)
        if (not isinstance(data, bytes) or not data or len(data) > 1024 * 1024 or result.packets >= 4096 or
                result.encoded_bytes + len(data) > 12 * 1024 * 1024 or
                type(ts) is not int or not 0 <= ts < 2**64 or type(seq) is not int):
            self.reject("input_budget")
            return False
        result.packets += 1
        result.encoded_bytes += len(data)
        self.first_packet.set()
        if ts:
            result.source_timestamps_seen += 1
            if kind in self.last_timestamp and ts < self.last_timestamp[kind]:
                result.source_clock_discontinuities += 1
            self.last_timestamp[kind] = ts
        return True

    async def raw_video(self, did, data, ts, seq, channel):
        self.raw("video", did, data, ts, seq, channel)

    async def raw_audio(self, did, data, ts, seq, channel, codec):
        if not self.raw("audio", did, data, ts, seq, channel):
            return
        # Use actual installed enum names. Unknown/new codecs fail closed.
        name = {"AUDIO_OPUS": "opus", "AUDIO_G711A": "g711a", "AUDIO_G711U": "g711u"}.get(getattr(codec, "name", None))
        audio = self.report.audio
        if name is None:
            self.reject("unsupported_codec")
        elif audio.codec is not None and audio.codec != name:
            self.reject("codec_changed")
        else:
            audio.codec = name

    def decoded(self, kind):
        result = getattr(self.report, kind)
        if result.decoded_frames >= 4096:
            self.reject("input_budget")
            return False
        elapsed = (self.clock() - self.started) * 1000
        if not 0 <= elapsed <= 20000:
            self.reject("input_budget")
            return False
        result.decoded_frames += 1
        result.status = "decoded"
        if result.first_decode_ms is None:
            result.first_decode_ms = elapsed
        result.last_decode_ms = elapsed
        return True

    async def video(self, did, frame, ts, channel, recv_ms, decode_ms):
        received_monotonic = self.clock()  # Local decoded callback entry, never camera capture.
        if not self.accepts(did, channel):
            return
        shape = getattr(frame, "shape", ())
        # Installed SDK emits BGR uint8 ndarray, despite its VideoFrame annotation.
        if (len(shape) != 3 or shape[2] != 3 or not 0 < shape[0] <= 2160 or
                not 0 < shape[1] <= 3840 or str(getattr(frame, "dtype", "")) != "uint8"):
            self.reject("decode_failed")
            return
        if self.decoded("video"):
            self.report.video.height, self.report.video.width = int(shape[0]), int(shape[1])
            if self._recognition is not None:
                await self._recognition.video(did, frame, channel,
                    source_timestamp=ts if type(ts) is int and 0 <= ts < 2**64 else None,
                    received_monotonic=received_monotonic)
        # Raw video callback does not expose codec ID; do not guess H264/H265.

    async def audio(self, did, frame, ts, channel, recv_ms, decode_ms):
        if not self.accepts(did, channel):
            return
        shape = getattr(frame, "shape", ())
        # Pinned SDK resamples to mono s16/16k in its decoder thread.
        if (len(shape) != 1 or not 0 < shape[0] <= 320000 or str(getattr(frame, "dtype", "")) != "int16" or
                self.report.audio.pcm_16k_samples + shape[0] > 320000):
            self.reject("decode_failed")
            return
        if self.decoded("audio"):
            self.report.audio.pcm_16k_samples += int(shape[0])
            self.report.audio.pcm_format = "mono_s16_16000"
            # Source rate/channels are hidden by this resampled SDK callback.
            if self._recognition is not None:
                await self._recognition.audio(did, frame, channel, source_timestamp=ts if type(ts) is int and 0 <= ts < 2**64 else None)

    def finish(self):
        self.active = False
        report = self.report
        report.failure = self.failure
        if not report.failure and not (report.video.decoded_frames and report.audio.pcm_16k_samples and
                                       report.video.packets and report.audio.packets):
            report.failure = "no_decoded_frames"
        elif not report.failure and any((self.clock() - self.started) * 1000 - stream.last_decode_ms > 1000
                                       for stream in (report.video, report.audio)):
            report.failure = "stream_idle"
        if report.failure and report.failure_phase is None:
            report.failure_phase = self.phase
        report.status = "failed" if report.failure else "decoded_sync_unknown"
        if report.status == "decoded_sync_unknown":
            report.reception_overlap_ms = max(0.0, min(report.video.last_decode_ms, report.audio.last_decode_ms) -
                max(report.video.first_decode_ms, report.audio.first_decode_ms))
        return MediaProbeResult.model_validate(report.model_dump())


class CameraStartObserver:
    """Observe only the owned pinned SDK's deferred start task, without GC.

    The public start_async schedules __try_start_async and returns immediately.
    Source qualification pins this private coroutine shape. No SDK method or C
    function is replaced, and the previous loop task factory remains in use.
    """
    def __init__(self, loop, counter):
        self.loop, self.counter = loop, counter
        self.previous_factory = loop.get_task_factory()
        self.instance = None
        self.tasks = []
        self.closed = False

    def task_factory(self, loop, coro, context=None):
        code, frame = getattr(coro, "cr_code", None), getattr(coro, "cr_frame", None)
        owned = (self.instance is not None and frame is not None and
                 getattr(code, "co_qualname", None) == "MIoTCameraInstance.__try_start_async" and
                 frame.f_locals.get("self") is self.instance)
        if self.previous_factory:
            task = (self.previous_factory(loop, coro) if context is None else
                    self.previous_factory(loop, coro, context=context))
        else:
            task = asyncio.Task(coro, loop=loop, context=context)
        if owned:
            self.counter.report.native_start_status = "pending"
            self.tasks.append(task)
            task.add_done_callback(self.finished)
        return task

    def finished(self, task):
        # Retrieve the exception even after close; never emit SDK exception text.
        error = None if task.cancelled() else task.exception()
        if self.closed:
            return
        report = self.counter.report
        report.native_start_status = "pending" if task.cancelled() else "failed" if error else "succeeded"
        if error:
            message = getattr(error, "message", None)
            prefix = f"camera start failed, {self.counter.camera_id}, "
            if (type(error).__module__ == "miot.error" and type(error).__name__ == "MIoTCameraError" and
                    type(message) is str and message.startswith(prefix)):
                suffix = message[len(prefix):]
                digits = suffix[1:] if suffix.startswith("-") else suffix
                if 0 < len(digits) <= 10 and digits.isascii() and digits.isdigit():
                    value = int(suffix)
                    if -(2**31) <= value < 2**31 and value != 0:
                        report.native_start_error_code = value
        if (task.cancelled() or error) and self.counter.active:
            self.counter.reject("stream_error", phase="connect")

    def stop_accepting(self):
        # A completed Task's done callback may still be queued behind the sample
        # timer. Resolve it while failure reporting is active, before cleanup.
        for task in self.tasks:
            if task.done():
                self.finished(task)
        if any(not task.done() for task in self.tasks) and not self.counter.failure:
            self.counter.reject("worker_timeout", phase="connect")

    async def drain(self):
        if self.tasks:
            # Cancelling an executor Future does not stop its native C call.
            # Do not free the owned instance while start may still be using it.
            await asyncio.shield(asyncio.gather(*self.tasks, return_exceptions=True))
            if any(task.cancelled() for task in self.tasks):
                raise NativeCleanupUncertain()

    def restore(self):
        self.closed = True
        self.loop.set_task_factory(self.previous_factory)


async def read_camera(config, client, *, codec_quality, sleep=asyncio.sleep, _recognition=None):
    """Narrow read lifecycle port backed by installed public SDK primitives."""
    counter = NativeCounter(config.camera_id, _recognition=_recognition)
    loop = asyncio.get_running_loop()
    observer = CameraStartObserver(loop, counter)
    ownership = NativeSessionOwnership(client)
    previous_loop_handler, previous_unraisable = loop.get_exception_handler(), sys.unraisablehook

    def reject_thread_fault(_event):
        # ctypes invokes unraisablehook on its native callback thread. Move only
        # a fixed code onto the owner loop; do not retain the event or its payload.
        try:
            loop.call_soon_threadsafe(lambda: counter.reject("protocol_error") if counter.active else None)
        except RuntimeError:  # An already closed worker cannot accept callbacks.
            pass

    loop.set_task_factory(observer.task_factory)
    loop.set_exception_handler(lambda _loop, _context: counter.reject("stream_error") if counter.active else None)
    sys.unraisablehook = reject_thread_fault

    async def sample():
        async with asyncio.timeout(8):
            await client.init_async()
            cameras = await client.get_cameras_async()
            camera = cameras.get(config.camera_id)
            if camera is None:
                counter.reject("camera_not_found")
                return
            if camera.is_set_pincode:
                counter.reject("camera_pin_required")
                return
            if not camera.online:
                counter.reject("camera_offline")
                return
            if camera.channel_count != 1:
                # Native start opens every lens; admit only the audited single lens scope.
                counter.reject("sdk_not_qualified")
                return
            counter.phase = "connect"
            instance = await client.create_camera_instance_async(camera, frame_interval=1000, enable_hw_accel=False)
            observer.instance = instance
            ownership.instance = instance
            await instance.register_raw_video_async(counter.raw_video, channel=0, multi_reg=True)
            await instance.register_raw_audio_async(counter.raw_audio, channel=0, multi_reg=True)
            await instance.register_status_changed_async(counter.status, multi_reg=True)
            await instance.register_decode_video_frame_async(counter.video, channel=0, multi_reg=True)
            await instance.register_decode_audio_frame_async(counter.audio, channel=0, multi_reg=True)
            counter.started = counter.clock()
            await instance.start_async(qualities=codec_quality, enable_audio=True, enable_reconnect=False, enable_record=False)
            # start_async schedules native connection and returns before media is ready.
            # Bound discovery + connection together; sampling starts on actual data.
            await counter.first_packet.wait()
            if counter.failure:
                return
        counter.phase = "sampling"
        timer = asyncio.create_task(sleep(config.seconds))
        failed = asyncio.create_task(counter.failed.wait())
        try:
            await asyncio.wait({timer, failed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            failed.cancel()
            await asyncio.gather(timer, failed, return_exceptions=True)
    try:
        await sample()
        if _recognition is not None:
            await _recognition.finish()
    except TimeoutError:
        counter.reject("worker_timeout")
    except asyncio.CancelledError:
        raise
    except Exception:
        counter.reject("stream_error")
    finally:
        observer.stop_accepting()
        counter.active = False  # Late native callbacks cannot qualify an ended run.
        try:
            async with asyncio.timeout(3):
                # Close only the owned camera/HTTP primitives. No proxy deinit.
                await observer.drain()
                if _recognition is not None:
                    await _recognition.close()
                await client.deinit_async()
                ownership.release()
        except Exception:
            counter.phase = "cleanup"
            counter.reject("worker_timeout")
        finally:
            observer.restore()
            loop.set_exception_handler(previous_loop_handler)
            sys.unraisablehook = previous_unraisable
    diagnostic = counter.finish()
    if _recognition is not None:
        status = _recognition.status()
        if diagnostic.failure:
            status = RecognitionStatus(state="unavailable", reason="native_cleanup_uncertain" if _NATIVE_SESSIONS
                else "worker_failed", simulated=_recognition.session.simulated)
        result = _recognition.private_worker_result(status=status)
        if _recognition._feature_sink is not None:
            from homeplane.recognition_feature_protocol import RecognitionWorkerResult
            return RecognitionWorkerResult(result, _recognition._feature_message if not diagnostic.failure else None)
        return result
    return diagnostic


async def read_camera_recognition(session, client, *, codec_quality, _owner_context=None, sleep=asyncio.sleep):
    """Internal synthetic owner seam; no native production admission is issued here."""
    if isinstance(_owner_context, NativeMetadataOwnerContext) and _owner_context.session != session:
        raise ValueError("native_owner_binding_mismatch")
    if isinstance(_owner_context, NativeMetadataOwnerContext) and not _owner_context.admits(session):
        return RecognitionConsumer(_owner_context=_owner_context).private_worker_result()
    if (not isinstance(_owner_context, (SyntheticOwnerContext, NativeMetadataOwnerContext))
            or not _owner_context.admits(session)):
        return RecognitionResult(session=session, status=RecognitionStatus(state="unavailable",
            reason="owner_admission_unavailable", simulated=session.simulated))
    consumer = RecognitionConsumer(_owner_context=_owner_context)
    if not consumer.active:
        return consumer.private_worker_result()
    return await read_camera(NativeMediaConnection(camera_id=session.camera_id, seconds=session.seconds),
        client, codec_quality=codec_quality, sleep=sleep, _recognition=consumer)


def denied_native_wire_request(request):
    """A typed v3 request is metadata only; return before SDK/login/model setup."""
    request = NativeFaceRequest.model_validate_json(request.model_dump_json())
    return NativeFaceResult(session=request.session, provenance=request.provenance,
        status=RecognitionStatus(state="unavailable", reason="owner_admission_unavailable",
            simulated=request.session.simulated))


class CameraReadSDK:
    """Public camera + scoped HTTP primitives only; no general MIoTClient/MQTT.

    This owns one fresh native manager in a disposable process and reads only the
    selected DID. Camera type/channel metadata comes from the installed SDK YAML.
    """
    def __init__(self, camera_id, http, camera, extra_reader, classifier, info_type, disconnected):
        self.camera_id, self.http, self.camera = camera_id, http, camera
        self.extra_reader, self.classifier = extra_reader, classifier
        self.info_type, self.disconnected = info_type, disconnected

    async def init_async(self):
        await self.camera.init_async(frame_interval=1000, enable_hw_accel=False)

    async def get_cameras_async(self):
        devices = await self.http.get_devices_with_dids_async([self.camera_id])
        device = (devices or {}).get(self.camera_id)
        if device is None:
            return {}
        extra = await self.extra_reader()
        if not await self.classifier(device.model, extra):
            return {}
        channels = extra.extra_info[device.model].channel_count if device.model in extra.extra_info else 1
        return {self.camera_id: self.info_type(**device.model_dump(), channel_count=channels,
            camera_status=self.disconnected)}

    async def create_camera_instance_async(self, info, **options):
        return await self.camera.create_camera_async(camera_info=info, **options)

    async def deinit_async(self):
        try:
            await self.camera.deinit_async()
        finally:
            await self.http.deinit_async()


async def probe(config):
    if not qualify_sdk():
        return MediaProbeResult(provider="miot_sdk", status="failed", failure="sdk_not_qualified", failure_phase="sdk_preflight")
    # Do not compete with a user-owned native service/session.
    try:
        connection = socket.create_connection(("127.0.0.1", 1810), timeout=.2)
    except OSError:
        pass
    else:
        connection.close()
        return MediaProbeResult(provider="miot_sdk", status="failed", failure="service_conflict")
    try:
        _, login = read_login(DB)
    except ValueError as error:
        return MediaProbeResult(provider="miot_sdk", status="failed", failure=str(error), failure_phase="login")
    except (OSError, sqlite3.Error):
        return MediaProbeResult(provider="miot_sdk", status="failed", failure="account_login_required", failure_phase="login")
    # SQLite query-only WAL readers may initialize a shared-memory index on a
    # cold open. Complete that metadata-only read before forbidding media writes.
    # No application row is written, and no SQLite handle remains in the SDK phase.
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    sys.meta_path.insert(0, ProducerImportFence())
    from miot.camera import MIoTCamera, get_camera_extra_info, is_camera_model
    from miot.cloud import MIoTHttpClient
    from miot.types import MIoTCameraInfo, MIoTCameraStatus, MIoTCameraVideoQuality
    # No cache, no producer callbacks, no credentials refresh or auth DB writes.
    client = CameraReadSDK(config.camera_id,
        MIoTHttpClient(cloud_server="cn", access_token=login["access_token"]),
        MIoTCamera(cloud_server="cn", access_token=login["access_token"]),
        get_camera_extra_info, is_camera_model, MIoTCameraInfo, MIoTCameraStatus.DISCONNECTED)
    return await read_camera(config, client, codec_quality=MIoTCameraVideoQuality.LOW)


async def cancellable_probe(config):
    """Parent stop asks the SDK to close first; parent still owns a kill deadline."""
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await probe(config)
    except asyncio.CancelledError:
        return MediaProbeResult(provider="miot_sdk", status="failed", failure="stream_closed")
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def publish_worker_result(result, output):
    if isinstance(result, MediaProbeResult):
        result.alignment_reason = "sdk_source_timestamps_clock_domain_unverified"
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result.worker_peak_rss_bytes = usage if sys.platform == "darwin" else usage * 1024
    with os.fdopen(output, "w") as stream:
        from homeplane.recognition_feature_protocol import encode_worker_output
        stream.write(encode_worker_output(result))


async def worker_probe(config, output):
    """Exit an uncertain owned worker before asyncio/interpreter finalization.

    The result pipe is explicitly flushed first. os._exit skips SDK destructors,
    atexit handlers and default-executor shutdown; it does not free an instance
    underneath a live C call. The existing parent still owns kill/reap deadlines.
    """
    result = MediaProbeResult(provider="miot_sdk", status="failed", failure="worker_failed")
    try:
        result = await cancellable_probe(config)
    except ImportError:
        result.failure = "worker_unavailable"
    except Exception:
        pass
    finally:
        if _NATIVE_SESSIONS or _OWNED_RECOGNITION_CALLS:
            # Also take this path on repeated cancellation or a partial deinit.
            # Returning to asyncio.run would cancel Tasks and then await native
            # executor threads; Python finalization could collect the SDK graph.
            exit_code = 0
            try:
                publish_worker_result(result, output)
            except BaseException:
                exit_code = 1
            finally:
                os._exit(exit_code)
    return result


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.disable(logging.CRITICAL)
    result = MediaProbeResult(provider="miot_sdk", status="failed", failure="worker_failed")
    # Native libraries can print to stdout; reserve the protocol fd and silence them.
    output = os.dup(sys.stdout.fileno())
    with open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), sys.stdout.fileno())
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("request_budget")
        request = json.loads(raw)
        if isinstance(request, dict) and request.get("message_kind") == "face_frame_request":
            result = denied_native_wire_request(NativeFaceRequest.model_validate_json(raw))
        elif isinstance(request, dict) and request.get("operation") == "recognition":
            session = RecognitionSession.model_validate_json(raw)
            # Wire data cannot supply admission, providers, quality or calibration.
            # A future trusted native owner seam is a distinct acceptance gate.
            result = RecognitionResult(session=session, status=RecognitionStatus(state="unavailable",
                reason="owner_admission_unavailable", simulated=session.simulated))
        else:
            config = NativeMediaConnection.model_validate_json(raw)
            result = asyncio.run(worker_probe(config, output))
    except ImportError:
        result = MediaProbeResult(provider="miot_sdk", status="failed", failure="worker_unavailable")
    except Exception:
        pass
    publish_worker_result(result, output)


if __name__ == "__main__":
    main()
