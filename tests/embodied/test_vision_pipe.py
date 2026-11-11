"""No model subprocesses: backpressure, framing and total request deadlines."""

import asyncio
import json
import base64
import secrets
from collections import deque
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeplane.embodied.vision_pipe import PIPE_LIMIT, request_vision
from homeplane.embodied import camera_recognition, previews, ptz_control
from homeplane.embodied.camera_audio import AudioIngress
from homeplane.embodied.frame_sampling import CAPTURE_RECEIVE_TIMEOUT, offer_frame
from homeplane.embodied.pet_tracks import PetTracks
from homeplane.people.live import close_identity
from tests.embodied.test_camera_audio import actual
from tests.embodied.test_capture_stream_recovery import closed_scope


def pipe(reply=b'{"boxes":[],"poses":[],"hands":[],"faces":[]}\n'):
    return SimpleNamespace(stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
                           stdout=SimpleNamespace(readline=AsyncMock(return_value=reply)))


@pytest.mark.asyncio
async def test_healthy_request_and_its_independent_peer_continue_while_a_write_is_stalled():
    stalled, healthy = pipe(), pipe()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    stalled.stdin.drain.side_effect = blocked
    task = asyncio.create_task(request_vision(stalled, {'jpeg': 'SYNTHETIC'}, timeout=.03))
    await entered.wait()
    assert await request_vision(healthy, {'jpeg': 'OTHER-SYNTHETIC'}, timeout=.03) == dict(
        boxes=[], poses=[], hands=[], faces=[])
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, .2)
    assert cancelled.is_set()
    stalled.stdout.readline.assert_not_awaited()
    assert json.loads(healthy.stdin.write.call_args.args[0]) == {'jpeg': 'OTHER-SYNTHETIC'}


@pytest.mark.asyncio
async def test_read_does_not_get_a_second_full_budget_after_backpressure(monkeypatch):
    transport = pipe()
    writing, written, reading = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    now = 100.0

    async def write():
        writing.set()
        await written.wait()

    async def read():
        reading.set()
        await asyncio.Event().wait()

    async def flush_callbacks():
        for _ in range(6):
            await asyncio.sleep(0)

    transport.stdin.drain.side_effect = write
    transport.stdout.readline.side_effect = read
    with monkeypatch.context() as patch:
        patch.setattr(loop, 'time', lambda: now)
        task = asyncio.create_task(request_vision(transport, {'jpeg': 'SYNTHETIC'}, timeout=3))
        try:
            await flush_callbacks()
            assert writing.is_set() and not reading.is_set()
            now = 102.0
            written.set()
            await flush_callbacks()
            assert reading.is_set() and not task.done()
            now = 102.9
            await flush_callbacks()
            assert not task.done()
            now = 103.01
            await flush_callbacks()
            assert task.done(), '读取必须沿用写入前开始的总期限'
            with pytest.raises(TimeoutError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['write', 'read'])
async def test_request_cancellation_stays_cancellation(phase):
    transport = pipe()
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    if phase == 'write':
        transport.stdin.drain.side_effect = blocked
    else:
        transport.stdout.readline.side_effect = blocked
    task = asyncio.create_task(request_vision(transport, {'jpeg': 'SYNTHETIC'}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize('reply,reason', [
    (b'', 'vision_stream_closed'), (b'[]\n', 'invalid_vision_reply'),
    (b'{private-invalid-json', 'invalid_vision_reply'), (b'{}', 'invalid_vision_reply'),
    (b'{"boxes":{},"poses":[]}', 'invalid_vision_reply'),
    (b'{"boxes":[],"poses":[],"hands":null}', 'invalid_vision_reply'),
    (b'{"boxes":[],"poses":[],"inference_ms":NaN}', 'invalid_vision_reply'),
    (b'{"error":"private-native-error","domain":"private"}', 'vision_request_failed'),
])
async def test_failed_or_malformed_output_cannot_become_an_empty_room(reply, reason):
    with pytest.raises((ValueError, RuntimeError), match='^'+reason+'$'):
        await request_vision(pipe(reply), {'jpeg': 'SYNTHETIC'})


@pytest.mark.asyncio
async def test_oversized_input_is_refused_before_writing_a_child_pipe():
    transport = pipe()
    with pytest.raises(ValueError, match='^vision_request_too_large$'):
        await request_vision(transport, {'jpeg': 'x'*PIPE_LIMIT})
    transport.stdin.write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['write_stall', 'read_stall', 'bad_reply'])
async def test_original_primary_owner_retires_and_reaps_a_failed_exchange(tmp_path, monkeypatch, fault):
    """Original run_source, receiver, retirement and reaper; no worker import."""
    import time
    owner, _, _, pending, _ = closed_scope()
    pending.cancel();owner['motor_waiters'] = {}
    capture, vision = pipe(), pipe(b'[]\n' if fault == 'bad_reply' else b'')
    for process in (capture, vision):
        process.returncode = None
        def terminate(p=process):p.returncode = 0
        process.terminate = process.kill = terminate
        process.wait = AsyncMock(return_value=0)

    async def blocked():await asyncio.Event().wait()
    if fault == 'write_stall':vision.stdin.drain.side_effect = blocked
    if fault == 'read_stall':vision.stdout.readline.side_effect = blocked
    sent = False
    async def captured():
        nonlocal sent
        if sent:await asyncio.Event().wait()
        sent = True
        request = json.loads(capture.stdin.write.call_args_list[0].args[0])
        return json.dumps(dict(type='frame', capture_session=request['capture_session'],
            received_at=time.time(), jpeg=base64.b64encode(b'SYNTHETIC-NOT-CAMERA-VIDEO').decode(),
            width=640, height=360)).encode()
    capture.stdout.readline.side_effect = captured
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=[capture, vision]))
    owner.update(asyncio=asyncio, time=time, secrets=secrets, base64=base64, json=json,
        SESSION='synthetic', camera_lease=None, camera_planner=None, Tracks=lambda: object(), PetTracks=PetTracks,
        processed_times=deque(), captured_times=deque(), PYTHON='SYNTHETIC-NOT-EXECUTED',
        VISION='SYNTHETIC-NOT-EXECUTED', MODELS=tmp_path, offer_frame=offer_frame,
        CAPTURE_RECEIVE_TIMEOUT=CAPTURE_RECEIVE_TIMEOUT,
        AudioIngress=AudioIngress, close_identity=close_identity, camera_is_open=AsyncMock(return_value=True),
        capture_environment=lambda: {},
        room=None,close_rooms=AsyncMock(),
        SceneProcess=lambda _: SimpleNamespace(analyze=AsyncMock(return_value=None),
            close=AsyncMock(return_value=True), status=lambda: dict(state='idle', reason=None)),
        request_vision=partial(request_vision, timeout=.03))
    owner['home'].gesture = SimpleNamespace(reset=Mock())
    owner['identities'].close = Mock()
    owner['games'].reset('ski')
    owner['reap'] = actual('homeplane/embodied/worker.py', 'reap', owner)
    for name in ('primary_frame_current', 'scene_frame_current'):
        actual('homeplane/embodied/worker.py', name, owner)
    run = actual('homeplane/embodied/worker.py', 'run_source', owner)
    await asyncio.wait_for(run(1), .5)
    assert owner['state']['status'] == 'unavailable'
    assert owner['state']['error_stage'] == 'vision_request'
    assert owner['state']['error_class'] == ('ValueError' if fault == 'bad_reply' else 'TimeoutError')
    assert not owner['state']['people'] and owner['state']['frame'] is None
    assert owner['games'].mode == 'ski' and owner['games'].source_resume_required
    assert capture.returncode == vision.returncode == 0
    capture.wait.assert_awaited_once();vision.wait.assert_awaited_once()
    owner['identities'].close.assert_called_once()
    assert owner['capture'] is owner['vision'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['write_stall', 'read_stall', 'bad_reply'])
async def test_actual_secondary_owner_and_recognizer_reap_failed_native_request_on_reconnect(tmp_path, monkeypatch, fault):
    """Real PreviewPool -> CameraRecognition -> bounded native pipe -> cleanup."""
    import time
    from tests.embodied.test_secondary_stream_recovery import CAMERA
    capture, vision = pipe(), pipe(b'[]\n' if fault == 'bad_reply' else b'')
    for process in (capture, vision):
        process.returncode = None
        def terminate(p=process):p.returncode = 0
        process.terminate = process.kill = terminate
        process.wait = AsyncMock(return_value=0)
    async def blocked():await asyncio.Event().wait()
    if fault == 'write_stall':vision.stdin.drain.side_effect = blocked
    if fault == 'read_stall':vision.stdout.readline.side_effect = blocked
    sent = False
    async def captured():
        nonlocal sent
        if sent:await asyncio.Event().wait()
        sent = True
        return json.dumps(dict(type='frame', received_at=time.time(),
            jpeg=base64.b64encode(b'SYNTHETIC-NOT-CAMERA-VIDEO').decode(), width=640, height=360)).encode()
    capture.stdout.readline.side_effect = captured
    lease = SimpleNamespace(snapshot={'before': {'camera_power': True}},
        acquire=AsyncMock(), release=AsyncMock(return_value=True))
    io = SimpleNamespace(close=AsyncMock())
    store = SimpleNamespace(clear_camera=Mock(), path=tmp_path/'unused-identity.sqlite')
    monkeypatch.setattr(previews, 'DATA', tmp_path)
    monkeypatch.setattr(previews, 'camera_storage', lambda _: tmp_path)
    monkeypatch.setattr(previews, 'capture_environment', lambda: {})
    monkeypatch.setattr(previews, 'camera_is_open', AsyncMock(return_value=True))
    monkeypatch.setattr(ptz_control, 'Transport', lambda _: io)
    monkeypatch.setattr(ptz_control, 'CameraLease', lambda *_, **__: lease)
    monkeypatch.setattr(camera_recognition, 'request_vision', partial(request_vision, timeout=.03))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=[vision, capture]))
    observation, clear = AsyncMock(), Mock()
    pool = previews.PreviewPool(identity_store=store, continuous=False, recognize=True,
        on_observation=observation, on_clear=clear)
    # A real monitor retries with the previous unavailable state. New ownership
    # must admit its first frame before publication changes the state to live.
    item = dict(state='unavailable', frames=0, frame=None, received_at=0,
                expires=time.monotonic()+60, shutter_changed=False)
    pool.sessions[CAMERA['id']] = item
    await asyncio.wait_for(pool._run(CAMERA, item), .5)
    vision.stdin.write.assert_called_once()
    assert item['state'] == 'unavailable' and not item['source_active']
    assert item['error_stage'] == 'recognition'
    assert item['error_class'] == ('ValueError' if fault == 'bad_reply' else 'TimeoutError')
    assert item['frame'] is None and item['frames'] == 0 and item['recognizer'] is None
    observation.assert_not_awaited();clear.assert_called_once_with(CAMERA['id'])
    store.clear_camera.assert_not_called()  # No face input reached the new identity owner.
    assert capture.returncode == vision.returncode == 0
    capture.wait.assert_awaited_once();vision.wait.assert_awaited_once()
    lease.release.assert_awaited_once();io.close.assert_awaited_once()
