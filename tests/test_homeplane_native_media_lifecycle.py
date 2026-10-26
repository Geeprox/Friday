"""SIMULATED deferred native starts and callback faults; no SDK is loaded."""
import asyncio
import contextvars
import sys
import threading
from types import SimpleNamespace

import pytest
from test_homeplane_native_media import SimulatedCamera, SimulatedSDK, immediate

from homeplane.media_contracts import MediaProbeResult, NativeMediaConnection
from homeplane.native_media_worker import read_camera


class MIoTCameraError(Exception):
    __module__ = "miot.error"  # Simulate only the pinned exception shape.

    def __init__(self, message):
        self.message = message
        super().__init__(message)


class MIoTCameraInstance(SimulatedCamera):
    """Match the pinned task's code name; every native operation is synthetic."""
    def __init__(self, *, code=-14, video=False, release=None):
        super().__init__()
        self.code, self.emit_video, self.release = code, video, release
        self.native_tasks = []  # Retain tasks so GC cannot report their exceptions.
        self.start_finished = False

    async def start_async(self, **options):
        self.starts.append(options)
        asyncio.get_running_loop().call_later(0, self.schedule_start)

    def schedule_start(self):
        self.native_tasks.append(asyncio.create_task(self.__try_start_async()))

    async def __try_start_async(self):
        did = "SIMULATED-camera"
        await self.callbacks["raw_audio"](did, b"SIMULATED", 1, 1, 0, SimpleNamespace(name="AUDIO_OPUS"))
        await self.callbacks["audio"](did, SimpleNamespace(shape=(320,), dtype="int16"), 1, 0, 1, 1)
        if self.emit_video:
            await self.callbacks["raw_video"](did, b"SIMULATED", 1, 1, 0)
            await self.callbacks["video"](did, SimpleNamespace(shape=(120, 160, 3), dtype="uint8"), 1, 0, 1, 1)
        if self.release:
            await self.release.wait()
        self.start_finished = True
        if self.code:
            raise MIoTCameraError(f"camera start failed, {did}, {self.code}")


@pytest.mark.asyncio
async def test_retained_deferred_start_error_is_prompt_and_preserves_opaque_code():
    client = SimulatedSDK()
    client.instance = MIoTCameraInstance()
    sample_finished = False

    async def sampling(_):
        nonlocal sample_finished
        await asyncio.sleep(.05)
        sample_finished = True

    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera", seconds=1.0),
        client, codec_quality="SIMULATED-LOW", sleep=sampling)
    # Reading the exception here prevents noisy GC output on the pre-fix run.
    assert isinstance(client.instance.native_tasks[0].exception(), MIoTCameraError)
    assert report.failure == "stream_error" and report.failure_phase == "connect"
    assert report.native_start_error_code == -14 and not sample_finished
    assert report.video.decoded_frames == 0 and report.audio.decoded_frames == 1
    assert report.native_start_status == "failed"
    assert "SIMULATED-camera" not in report.model_dump_json()
    assert client.calls[-1] == "deinit" and not report.model_invoked


@pytest.mark.asyncio
async def test_pending_native_start_prevents_qualification_and_finishes_before_free():
    client = SimulatedSDK()
    release = asyncio.Event()
    client.instance = MIoTCameraInstance(code=0, video=True, release=release)
    closed = asyncio.Event()
    old_close = client.deinit_async

    async def close():
        assert client.instance.start_finished
        closed.set()
        await old_close()

    client.deinit_async = close

    async def sampling(_):
        # Complete after the sample ends, while cleanup waits for native start.
        asyncio.get_running_loop().call_later(.03, release.set)

    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
        codec_quality="SIMULATED-LOW", sleep=sampling)
    assert closed.is_set()
    assert report.status == "failed" and report.failure == "worker_timeout"
    assert report.failure_phase == "connect" and report.native_start_error_code is None
    assert report.native_start_status == "succeeded"  # Completed only during cleanup.


@pytest.mark.asyncio
async def test_native_thread_fault_wakes_owner_loop_with_debug_checks(monkeypatch):
    client = SimulatedSDK()
    loop = asyncio.get_running_loop()
    prior_debug, prior_hook = loop.get_debug(), sys.unraisablehook
    loop.set_debug(True)
    thread_errors = []
    started = asyncio.Event()
    original_start = client.instance.start_async
    import homeplane.native_media_worker as worker
    original_reject = worker.NativeCounter.reject
    fault_threads = []

    def reject(self, code, **options):
        fault_threads.append(threading.get_ident())
        return original_reject(self, code, **options)

    monkeypatch.setattr(worker.NativeCounter, "reject", reject)

    async def start(**options):
        await original_start(**options)
        started.set()
        def fault():
            try:
                sys.unraisablehook(SimpleNamespace(
                    exc_value=ValueError("SIMULATED-private-payload")))
            except Exception as error:
                thread_errors.append(error)
        await asyncio.to_thread(fault)

    client.instance.start_async = start

    try:
        report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
            codec_quality="SIMULATED-LOW", sleep=immediate)
    finally:
        loop.set_debug(prior_debug)
    assert started.is_set() and not thread_errors
    assert fault_threads == [threading.get_ident()]
    assert report.failure == "protocol_error" and "private-payload" not in report.model_dump_json()
    assert sys.unraisablehook is prior_hook


@pytest.mark.asyncio
async def test_previous_task_factory_context_and_handlers_are_restored():
    loop = asyncio.get_running_loop()
    prior_factory, prior_handler = loop.get_task_factory(), loop.get_exception_handler()
    calls = []
    marker = contextvars.ContextVar("SIMULATED-marker", default=False)
    ctx = contextvars.copy_context()
    ctx.run(marker.set, True)

    def factory(loop, coro, context=None):
        calls.append(context)
        return asyncio.Task(coro, loop=loop, context=context)

    loop.set_task_factory(factory)
    try:
        async def marked():
            return marker.get()

        async def sampling(_):
            assert await asyncio.create_task(marked(), context=ctx)

        report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), SimulatedSDK(),
            codec_quality="SIMULATED-LOW", sleep=sampling)
        assert report.status == "decoded_sync_unknown" and loop.get_task_factory() is factory
        assert loop.get_exception_handler() is prior_handler
        assert ctx in calls
    finally:
        loop.set_task_factory(prior_factory)


@pytest.mark.parametrize("code,expected", [(-14, -14), (2**31 - 1, 2**31 - 1),
    (-(2**31), -(2**31)), (2**31, None), (-(2**31) - 1, None),
    ("-14, SIMULATED-private", None), ("-１４", None), ("+14", None)])
@pytest.mark.asyncio
async def test_only_bounded_exact_numeric_sdk_error_survives_redaction(code, expected):
    client = SimulatedSDK()
    client.instance = MIoTCameraInstance(code=code)
    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
        codec_quality="SIMULATED-LOW", sleep=immediate)
    assert report.native_start_status == "failed" and report.failure == "stream_error"
    assert report.native_start_error_code == expected
    assert "SIMULATED-private" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_owned_successful_start_can_qualify_but_keeps_alignment_unknown():
    client = SimulatedSDK()
    client.instance = MIoTCameraInstance(code=0, video=True)
    report = await read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"), client,
        codec_quality="SIMULATED-LOW", sleep=immediate)
    assert report.native_start_status == "succeeded" and report.native_start_error_code is None
    assert report.status == "decoded_sync_unknown" and report.alignment_error_bound_ms is None


@pytest.mark.asyncio
async def test_hung_native_start_is_bounded_and_never_frees_the_active_instance():
    client = SimulatedSDK()
    release = asyncio.Event()
    client.instance = MIoTCameraInstance(code=0, video=True, release=release)
    try:
        report = await asyncio.wait_for(read_camera(NativeMediaConnection(camera_id="SIMULATED-camera"),
            client, codec_quality="SIMULATED-LOW", sleep=immediate), 4)
        assert report.status == "failed" and report.failure == "worker_timeout"
        assert report.native_start_status == "pending" and "deinit" not in client.calls
        assert report.failure_phase == "connect"
    finally:
        release.set()
        await asyncio.gather(*client.instance.native_tasks, return_exceptions=True)


@pytest.mark.parametrize("fields", [
    {"provider": "miot_sdk", "native_start_status": "pending", "native_start_error_code": -14},
    {"provider": "miot_sdk", "native_start_status": "failed", "native_start_error_code": 0},
    {"native_start_status": "failed", "native_start_error_code": -14},
])
def test_contract_rejects_forged_native_error_states(fields):
    with pytest.raises(ValueError):
        MediaProbeResult.model_validate(fields)


@pytest.mark.asyncio
async def test_same_named_foreign_task_is_not_observed():
    import homeplane.native_media_worker as worker
    loop = asyncio.get_running_loop()
    counter = worker.NativeCounter("SIMULATED-camera")
    observer = worker.CameraStartObserver(loop, counter)
    owned, foreign = MIoTCameraInstance(code=0), MIoTCameraInstance()

    async def ignore(*args):
        pass

    foreign.callbacks = {"raw_audio": ignore, "audio": ignore}
    observer.instance = owned
    loop.set_task_factory(observer.task_factory)
    try:
        task = asyncio.create_task(foreign._MIoTCameraInstance__try_start_async())
        with pytest.raises(MIoTCameraError):
            await task
        assert observer.tasks == [] and counter.report.native_start_status == "unobserved"
        assert counter.report.native_start_error_code is None
    finally:
        observer.restore()


@pytest.mark.asyncio
async def test_done_failure_is_resolved_even_before_queued_done_callback():
    import homeplane.native_media_worker as worker
    counter = worker.NativeCounter("SIMULATED-camera")
    observer = worker.CameraStartObserver(asyncio.get_running_loop(), counter)

    async def fail():
        raise MIoTCameraError("camera start failed, SIMULATED-camera, -14")

    task = asyncio.create_task(fail())
    with pytest.raises(MIoTCameraError):
        await task
    observer.tasks = [task]
    counter.phase = "sampling"
    observer.stop_accepting()
    assert counter.failure == "stream_error" and counter.report.failure_phase == "connect"
    assert counter.report.native_start_error_code == -14
