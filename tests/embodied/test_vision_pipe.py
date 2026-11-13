"""No model subprocesses: backpressure, framing and total request deadlines."""

import asyncio
import json
import base64
import secrets
import time
from collections import deque
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeplane.embodied.vision_pipe import PIPE_LIMIT, request_vision
from homeplane.embodied import camera_recognition, previews, ptz_control, vision_pipe
from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.party_camera import PartyCamera
from homeplane.embodied.camera_audio import AudioIngress
from homeplane.embodied.frame_sampling import CAPTURE_RECEIVE_TIMEOUT, offer_frame
from homeplane.embodied.pet_tracks import PetTracks
from homeplane.people.live import close_identity
from tests.embodied.test_camera_audio import actual
from tests.embodied.test_capture_stream_recovery import closed_scope
from tests.embodied.test_room_process import room_port, until


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
    (b'{"boxes":[],"poses":[]}', 'invalid_vision_reply'),
    (b'{private-invalid-json', 'invalid_vision_reply'), (b'{}', 'invalid_vision_reply'),
    (b'{"boxes":{},"poses":[]}', 'invalid_vision_reply'),
    (b'{"boxes":[],"poses":[],"hands":null}', 'invalid_vision_reply'),
    (b'{"boxes":[],"poses":[],"inference_ms":NaN}', 'invalid_vision_reply'),
    (b'{"error":"private-native-error","domain":"private"}\n', 'vision_request_failed'),
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


def owned_port():
    process = pipe()
    process.returncode = None
    process.terminate = process.kill = Mock(side_effect=lambda: setattr(process, 'returncode', 0))
    process.wait = AsyncMock(return_value=0)
    return process


def camera_ports(monkeypatch, fault):
    capture, failed, replacement = owned_port(), owned_port(), owned_port()
    queue = asyncio.Queue()
    capture.stdout.readline.side_effect = queue.get
    clock = [20.]
    monkeypatch.setattr(vision_pipe, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(vision_pipe, 'request_vision', partial(request_vision, timeout=.03))
    spawn = AsyncMock(side_effect=[capture, failed, replacement])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)

    async def drain():
        if fault == 'write_stall' and failed.stdin.drain.await_count > 1:
            await asyncio.Event().wait()

    async def read():
        if failed.stdout.readline.await_count == 1:
            return replacement.stdout.readline.return_value
        if fault == 'read_stall':
            await asyncio.Event().wait()
        return b'[]\n'

    failed.stdin.drain.side_effect = drain
    failed.stdout.readline.side_effect = read

    def frame(marker):
        at = time.time()
        jpeg = b'\xff\xd8' + marker + b'\xff\xd9'
        encoded = base64.b64encode(jpeg).decode()
        scope = json.loads(capture.stdin.write.call_args_list[0].args[0])
        queue.put_nowait(json.dumps(dict(type='frame', received_at=at, preview_at=at, jpeg=encoded,
            preview=encoded, capture_session=scope['capture_session'], camera_source_context='a'*64,
            width=640, height=360)).encode())
        return jpeg

    return capture, failed, replacement, clock, queue, frame, spawn


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['write_stall', 'read_stall', 'bad_reply'])
async def test_primary_vision_failure_restarts_only_recognition_and_rebinds_fresh_frame(tmp_path, monkeypatch, fault):
    from fastapi import HTTPException, Request
    from starlette.responses import Response

    owner, _, _, pending, _ = closed_scope()
    pending.cancel()
    capture, failed, replacement, clock, queue, frame, spawn = camera_ports(monkeypatch, fault)
    camera = owner['SELECTED']
    owner['state']['processed_frames'] = 0
    owner.update(asyncio=asyncio, time=time, secrets=secrets, base64=base64, json=json, HTTPException=HTTPException,
        Request=Request, Response=Response, SESSION='synthetic', camera_lease=None, camera_planner=None, motor_waiters={},
        Tracks=lambda:SimpleNamespace(update=Mock(return_value=[]),invalidate=Mock(),input_valid=True),
        PetTracks=PetTracks, PET_MODES=set(), processed_times=deque(), captured_times=deque(),
        PYTHON='synthetic-capture', VISION='synthetic-vision', MODELS=tmp_path, PRIVATE=tmp_path,
        offer_frame=offer_frame, CAPTURE_RECEIVE_TIMEOUT=CAPTURE_RECEIVE_TIMEOUT, AudioIngress=AudioIngress,
        close_identity=close_identity, camera_is_open=AsyncMock(return_value=True), capture_environment=lambda:{},
        VisionProcess=vision_pipe.VisionProcess, RoomSession=lambda *_,**__:room_port(), room=None,
        CAMERA_STORAGE=tmp_path, room_lock=asyncio.Lock(), close_rooms=AsyncMock(),
        SceneProcess=lambda _:SimpleNamespace(analyze=AsyncMock(return_value=None),
            close=AsyncMock(return_value=True), status=lambda:dict(state='idle')),
        analyze=AsyncMock(return_value=[]), wall=CameraWall([camera]), interaction_enabled=False,
        practice=SimpleNamespace(suppresses=lambda _:False,observe=Mock()), party_camera=PartyCamera(),
        registry=SimpleNamespace(observe=Mock(return_value=[])), dispatch_interactions=AsyncMock(),
        calibration_frames=SimpleNamespace(clear=Mock(),publish=Mock()),rate=lambda _:0,
        recorder=SimpleNamespace(public=lambda:{}),configuration=lambda:{'cameras':[camera]})
    owner['home'].gesture = SimpleNamespace(reset=Mock())
    owner['home'].observe = AsyncMock()
    owner['home'].speaking = False
    owner['home'].last_audio = None
    owner['home'].attention = SimpleNamespace(candidate=lambda:None)
    owner['home'].public = lambda:{}
    owner['identities'].close = Mock()
    owner['identities'].reason = None
    owner['fleet'].update = Mock(return_value=[])
    owner['fleet'].voice = Mock(return_value=False)
    owner['fleet'].public = lambda _:{}
    owner['fleet'].cameras = {}
    owner['safety'].offer = Mock()
    owner['safety'].sound_requested = lambda _:False
    owner['safety'].recording_status = lambda:{'state':'ready'}
    owner['camera_intents'] = SimpleNamespace(load=lambda:SimpleNamespace(mode='auto'))
    owner['games'].reset('ski')
    for name in ('reap','invalidate_calibration','calibration_source','primary_frame_current','scene_frame_current',
                 'primary_recovery_frame','view','preview_status','preview_frame','run_source'):
        actual('homeplane/embodied/worker.py', name, owner)
    task = asyncio.create_task(owner['run_source'](1))
    state = owner['state']
    owner['require_preview_source'] = Mock()
    owner['preview_selection'] = lambda _: camera
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'), headers={})
    try:
        await until(lambda:capture.stdin.write.called)
        frame(b'SYNTHETIC-FIRST')
        await until(lambda:state['processed_frames']==1 or task.done() or state.get('recognition')=='recovering')
        assert state['processed_frames']==1, (state.get('error_class'),state.get('error_stage'))
        previous = state['session_id']
        frame(b'SYNTHETIC-FAILED')
        await until(lambda:failed.wait.await_count==1)
        raw = frame(b'SYNTHETIC-RAW-DURING-RECOVERY')
        await until(lambda:(owner['wall'].frame(camera['id']) or (None,))[0]==raw)
        assert capture.returncode is None and owner['capture'] is capture and spawn.await_count==2
        assert state['status']=='connecting' and state['recognition']=='recovering'
        assert state['error_class']==('ValueError' if fault=='bad_reply' else 'TimeoutError')
        assert state['session_id']!=previous and state['frame'] is None and not state['people']
        assert owner['games'].source_resume_required and not owner['home'].armed
        assert owner['safety'].offer.call_count==1
        assert camera['id'] not in owner['wall'].observations
        status = await owner['preview_status'](camera['id'], request)
        assert status['recognition']=='recovering' and status['frame_available']
        assert (await owner['preview_frame'](camera['id'], request)).body==raw
        assert owner['view']()['camera_sessions'][0]['latency_ms'] is None

        clock[0] += 2
        frame(b'SYNTHETIC-RECOVERED')
        await until(lambda:state['processed_frames']==2)
        assert state['status']=='live' and state['recognition']=='running' and state['error_class'] is None
        assert owner['safety'].offer.call_count==2 and spawn.await_count==3
        assert capture.returncode is None and failed.returncode==0
        queue.put_nowait(b'')
        await asyncio.wait_for(task, 1)
        assert owner['primary_recovery_frame']() is None and owner['capture'] is None
        assert capture.returncode==replacement.returncode==0
        failed.wait.assert_awaited_once()
        capture.wait.assert_awaited_once()
        replacement.wait.assert_awaited_once()
        owner['identities'].close.assert_called_once()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_unreaped_vision_child_cannot_be_replaced_or_receive_another_frame(monkeypatch):
    failed, replacement = owned_port(), owned_port()
    failed.stdout.readline.return_value = b'[]\n'
    failed.terminate.side_effect = OSError('synthetic-reap-fault')
    clock = [20.]
    monkeypatch.setattr(vision_pipe,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    spawn = AsyncMock(side_effect=[failed,replacement])
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    owner = vision_pipe.VisionProcess('synthetic-vision')
    fault = Mock()
    assert await owner.detect({'jpeg':'SYNTHETIC'},on_failure=fault) is None
    clock[0] += 2
    assert await owner.detect({'jpeg':'SYNTHETIC'},on_failure=fault) is None
    assert owner.process is failed and spawn.await_count==1
    failed.stdin.write.assert_called_once()
    fault.assert_called_once_with('ValueError')
    failed.returncode = 0
    clock[0] += 2
    assert (await owner.detect({'jpeg':'FRESH-SYNTHETIC'},on_failure=fault))['boxes']==[]
    assert owner.process is replacement
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['write_stall', 'read_stall', 'bad_reply'])
@pytest.mark.parametrize('close_during_recovery', [False, True])
async def test_secondary_vision_failure_keeps_preview_and_privacy_gate_on_same_capture(
        tmp_path, monkeypatch, fault, close_during_recovery):
    from tests.embodied.test_secondary_stream_recovery import CAMERA

    capture, failed, replacement, clock, queue, frame, spawn = camera_ports(monkeypatch, fault)
    lease = SimpleNamespace(snapshot={'before': {'camera_power': True}},
                            acquire=AsyncMock(),release=AsyncMock(return_value=True))
    io = SimpleNamespace(close=AsyncMock())
    identities = SimpleNamespace(discard=Mock(), close=Mock())
    monkeypatch.setattr(previews,'DATA',tmp_path)
    monkeypatch.setattr(previews,'camera_storage',lambda _:tmp_path)
    monkeypatch.setattr(previews,'capture_environment',lambda:{})
    monkeypatch.setattr(previews,'camera_is_open',AsyncMock(return_value=True))
    monkeypatch.setattr(ptz_control,'Transport',lambda _:io)
    monkeypatch.setattr(ptz_control,'CameraLease',lambda *_,**__:lease)
    monkeypatch.setattr(camera_recognition,'RoomSession',lambda _:room_port())
    monkeypatch.setattr(camera_recognition,'camera_storage',lambda _:tmp_path)
    monkeypatch.setattr(camera_recognition,'IdentitySession',lambda *args:identities)
    monkeypatch.setattr(camera_recognition,'analyze',AsyncMock(return_value=[]))
    wall = CameraWall([CAMERA])
    observation, clear = AsyncMock(), Mock()
    pool = previews.PreviewPool(continuous=True, recognize=True, wall=wall,
                                on_observation=observation, on_clear=clear)
    camera = CAMERA['id']
    item = dict(state='connecting',frames=0,frame=None,received_at=0,
                expires=time.monotonic()+60,shutter_changed=False)
    pool.sessions[camera] = item
    task = asyncio.create_task(pool._run(CAMERA,item))
    try:
        await until(lambda:capture.stdin.write.called)
        frame(b'SYNTHETIC-FIRST')
        await until(lambda:pool.status(camera)['recognition'] in {'running','recovering'} or task.done())
        assert pool.status(camera)['recognition']=='running', (item.get('error_class'),item.get('error_stage'))
        previous = observation.await_args.args[0]['session']
        frame(b'SYNTHETIC-FAILED')
        await until(lambda:failed.wait.await_count==1)
        raw = frame(b'SYNTHETIC-RAW-DURING-RECOVERY')
        await until(lambda:pool.frame(camera)==raw)
        status = pool.status(camera)
        assert status['recognition']=='recovering' and status['frame_available']
        assert status['latency_ms'] is None and not status['follow_target_available']
        assert status['error_class']==('ValueError' if fault=='bad_reply' else 'TimeoutError')
        assert capture.returncode is None and item['process'] is capture and spawn.await_count==2
        clear.assert_called_once_with(camera)
        assert observation.await_count==1

        if not close_during_recovery:
            clock[0] += 2
            frame(b'SYNTHETIC-RECOVERED')
            await until(lambda:observation.await_count==2 and pool.status(camera)['recognition']=='running')
            assert observation.await_args.args[0]['session']!=previous
            assert observation.await_args.args[1] is capture and spawn.await_count==3
            assert capture.returncode is None and failed.returncode==0
        queue.put_nowait(b'{"type":"error","reason":"privacy_shutter_closed"}')
        await asyncio.wait_for(task, 1)
        assert pool.status(camera)['state']=='shutter_closed' and pool.frame(camera) is None
        failed.wait.assert_awaited_once()
        capture.wait.assert_awaited_once()
        if close_during_recovery:
            replacement.wait.assert_not_awaited()
            assert spawn.await_count==2 and observation.await_count==1
        else:
            replacement.wait.assert_awaited_once()
        lease.release.assert_awaited_once()
        io.close.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)
