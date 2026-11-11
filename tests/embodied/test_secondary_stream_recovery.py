"""Actual secondary owner lifecycle with inert capture and inference ports."""

import asyncio
import ast
import base64
import copy
import json
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeplane.embodied.party_camera import PartyCamera

from homeplane.embodied import camera_recognition, previews, ptz_control
from homeplane.embodied.camera_wall import CameraWall
from homeplane.embodied.motion_games import MotionGames
from homeplane.people.live import close_identity
from tests.embodied.test_camera_audio import actual


CAMERA = dict(id='cam_1111111111', name='Synthetic', room='Synthetic')


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [b'', b'{private-invalid-json', b'[]\n',
                                   OSError('private-pipe-error'), ValueError('private-line-limit')])
async def test_capture_fault_revokes_live_inputs_before_blocked_inference_returns(tmp_path, monkeypatch, fault):
    reads = asyncio.Queue()
    accepted, entered, release, delivered = (asyncio.Event() for _ in range(4))
    observed, cleared, restored = [], [], []
    jpeg = b'\xff\xd8SYNTHETIC-NOT-CAMERA-PIXELS\xff\xd9'
    wall = CameraWall([CAMERA])

    class Recognizer:
        session = 'synthetic-recognition-session'
        timing = {}

        def __init__(self):
            self.frames = 0

        async def start(self):
            pass

        async def observe(self, packet, **_):
            self.frames += 1
            if self.frames == 2:
                entered.set()
                await release.wait()
            return dict(camera=CAMERA['id'], session=self.session, people=[dict(id=self.frames)],
                        hands=[], now=time.monotonic(), captured_mono=time.monotonic())

        async def close(self):
            pass

    class Lease:
        snapshot = {'before': {'camera_power': True}}

        def __init__(self, *_, **__):
            pass

        async def acquire(self):
            pass

        async def release(self):
            restored.append(True)
            return True

    async def read():
        value = await reads.get()
        if value is fault:
            delivered.set()
        if isinstance(value, Exception):
            raise value
        return value

    process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
                              stdout=SimpleNamespace(readline=read), wait=AsyncMock())

    def terminate():
        process.returncode = 0

    process.terminate = process.kill = terminate
    io = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(previews, 'DATA', tmp_path)
    monkeypatch.setattr(previews, 'camera_is_open', AsyncMock(return_value=True))
    monkeypatch.setattr(previews, 'camera_storage', lambda _: tmp_path)
    monkeypatch.setattr(previews, 'capture_environment', lambda: {})
    monkeypatch.setattr(ptz_control, 'Transport', lambda _: io)
    monkeypatch.setattr(ptz_control, 'CameraLease', Lease)
    monkeypatch.setattr(camera_recognition, 'CameraRecognition', lambda *_: Recognizer())
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))

    async def observation(value, _):
        observed.append(value)
        accepted.set()

    pool = previews.PreviewPool(continuous=False, recognize=True, wall=wall,
                               on_observation=observation, on_clear=cleared.append)
    item = dict(state='connecting', recognizer=None, process=None, shutter_changed=False,
                expires=time.monotonic()+60, frames=0, frame=None, received_at=0)
    pool.sessions[CAMERA['id']] = item

    def frame():
        at = time.time()
        return json.dumps(dict(type='frame', jpeg=base64.b64encode(jpeg).decode(),
            preview=base64.b64encode(jpeg).decode(), preview_at=at, received_at=at,
            width=640, height=360)).encode()

    task = asyncio.create_task(pool._run(CAMERA, item))
    try:
        reads.put_nowait(frame())
        await asyncio.wait_for(accepted.wait(), .5)
        await asyncio.sleep(0)
        assert item['state'] == 'live' and item['frames'] == 1
        reads.put_nowait(frame())
        await asyncio.wait_for(entered.wait(), .5)
        assert not task.done() and wall.frame(CAMERA['id'])
        reads.put_nowait(fault)
        await asyncio.wait_for(delivered.wait(), .5)
        await asyncio.sleep(0)
        assert item['state'] == 'unavailable', 'EOF/fault remained queued behind inference'
        assert item['frame'] is None and item['received_at'] == 0
        assert wall.frame(CAMERA['id']) is None
        assert cleared == [CAMERA['id']]
        assert item.get('control') is None
        assert pool.status(CAMERA['id'])['recognition'] == 'waiting'
        assert 'private-' not in json.dumps(pool.status(CAMERA['id']))
        release.set()
        await asyncio.wait_for(task, .5)
        assert len(observed) == 1, 'Retired inference reached the real observation callback'
        assert item['frames'] == 1 and item['frame'] is None
        assert restored == [True] and io.close.await_count == 1
        assert process.returncode is not None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['recognizer_close', 'capture_reap', 'stop_during_cleanup'])
async def test_continuous_owner_reconnects_after_cleanup_failure(tmp_path, monkeypatch, fault):
    """用原生产重连循环验证清理故障、镜头关闭和主动停止。"""
    retry_delays, resume, observed = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    reads = [asyncio.Queue(), asyncio.Queue()]
    shutter_open = True
    read_shutter = AsyncMock(side_effect=lambda _: shutter_open)

    async def sleep(seconds):
        if seconds in {5, 15}:
            retry_delays.put_nowait(seconds)
            await resume.get()
        else:
            await asyncio.sleep(seconds)

    def capture(index):
        process = SimpleNamespace(returncode=None, stdout=SimpleNamespace(readline=reads[index].get),
                                  stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()), wait=AsyncMock())

        def terminate():
            if index == 0 and fault == 'capture_reap':
                raise OSError('synthetic-reap-failure')
            process.returncode = 0

        process.terminate = process.kill = Mock(side_effect=terminate)
        return process

    processes = [capture(0), capture(1)]
    spawn = AsyncMock(side_effect=processes)
    leases, recognizers = [], []

    def lease(*_, **__):
        owner = SimpleNamespace(snapshot={'before': {'camera_power': True}},
                                acquire=AsyncMock(), release=AsyncMock(return_value=True))
        leases.append(owner)
        return owner

    def recognizer(*_):
        index = len(recognizers)
        session = f'synthetic-reconnect-{index}'
        cleanup_error = (ProcessLookupError('synthetic-cleanup-failure')
                         if index == 0 and fault != 'capture_reap' else None)

        async def observe(*_, **__):
            now = time.monotonic()
            return dict(camera=CAMERA['id'], session=session, people=[], hands=[], now=now, captured_mono=now)

        owner = SimpleNamespace(start=AsyncMock(), close=AsyncMock(side_effect=cleanup_error),
                                observe=observe, timing={}, session=session)
        recognizers.append(owner)
        return owner

    async def observation(value, process):
        observed.put_nowait((value['session'], process))

    monkeypatch.setattr(previews, 'asyncio', SimpleNamespace(**(vars(asyncio) | {
        'sleep': sleep, 'create_subprocess_exec': spawn})))
    monkeypatch.setattr(previews, 'DATA', tmp_path)
    monkeypatch.setattr(previews, 'camera', lambda _: CAMERA)
    monkeypatch.setattr(previews, 'camera_is_open', read_shutter)
    monkeypatch.setattr(previews, 'camera_storage', lambda _: tmp_path)
    monkeypatch.setattr(previews, 'capture_environment', lambda: {})
    monkeypatch.setattr(ptz_control, 'Transport', lambda _: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(ptz_control, 'CameraLease', lease)
    monkeypatch.setattr(camera_recognition, 'CameraRecognition', recognizer)
    intent = SimpleNamespace(mode='hold', revision='synthetic-hold', status='saved')
    pool = previews.PreviewPool(continuous=True, recognize=True, on_observation=observation,
                                intent_factory=lambda _: SimpleNamespace(load=lambda **__: intent))

    def frame():
        jpeg = base64.b64encode(b'\xff\xd8SYNTHETIC-NOT-CAMERA-PIXELS\xff\xd9').decode()
        return json.dumps(dict(type='frame', jpeg=jpeg, received_at=time.time(), width=640, height=360)).encode()

    camera_id = CAMERA['id']
    await pool.start(camera_id)
    item = pool.sessions[camera_id]
    try:
        reads[0].put_nowait(frame())
        assert await asyncio.wait_for(observed.get(), 2) == ('synthetic-reconnect-0', processes[0])
        assert pool.status(camera_id)['recognition'] == 'running'
        if fault == 'stop_during_cleanup':
            await asyncio.wait_for(pool.close(), 2)
            assert item['task'].cancelled()
            assert not pool.source_current(camera_id, processes[0])
            assert retry_delays.empty() and spawn.await_count == 1
            return

        shutter_reads = read_shutter.await_count
        shutter_open = False
        reads[0].put_nowait(b'')
        assert await asyncio.wait_for(retry_delays.get(), 2) == 5
        assert pool.status(camera_id)['state'] == 'unavailable'
        assert pool.status(camera_id)['error_stage'] == 'cleanup'
        assert pool.frame(camera_id) is None and not pool.source_current(camera_id, processes[0])
        leases[0].release.assert_awaited_once()
        resume.put_nowait(None)
        if fault == 'capture_reap':
            assert await asyncio.wait_for(retry_delays.get(), 2) == 5
            assert processes[0].returncode is None and read_shutter.await_count == shutter_reads
            assert spawn.await_count == 1
            processes[0].returncode = 0
            resume.put_nowait(None)
        assert await asyncio.wait_for(retry_delays.get(), 2) == 15
        assert pool.status(camera_id)['state'] == 'shutter_closed'
        assert spawn.await_count == len(leases) == 1
        shutter_open = True
        resume.put_nowait(None)
        reads[1].put_nowait(frame())
        assert await asyncio.wait_for(observed.get(), 2) == ('synthetic-reconnect-1', processes[1])
        status = pool.status(camera_id)
        assert status['recognition'] == 'running' and status['error_stage'] is None
        assert item['mode'] == intent.mode and item['mode_revision'] == intent.revision
        assert processes[0].returncode == 0 and pool.source_current(camera_id, processes[1])
        assert spawn.await_count == len(leases) == 2
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('silence', 'open_after_gap', 'outcome'), [
    (65, True, 'live'), (65, False, 'shutter_closed'), (90, True, 'unavailable'),
])
async def test_background_owner_waits_for_native_stream_recovery(tmp_path, monkeypatch, silence, open_after_gap, outcome):
    """推进合成时钟，核对旧画面失效、原进程恢复和父进程兜底超时。"""
    now = 1000.0
    shutter_open = True
    native_packets, receive_waits, observed = asyncio.Queue(), asyncio.Queue(), asyncio.Queue()

    async def wait_for(awaitable, timeout):
        if timeout < 40:
            return await asyncio.wait_for(awaitable, timeout)
        entered = now
        receive_waits.put_nowait(entered)
        packet = await awaitable
        if now - entered >= timeout:
            raise asyncio.TimeoutError
        return packet

    capture = SimpleNamespace(returncode=None, stdout=SimpleNamespace(readline=native_packets.get),
                              stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()), wait=AsyncMock())
    capture.terminate = capture.kill = lambda: setattr(capture, 'returncode', 0)
    spawn = AsyncMock(return_value=capture)
    lease = SimpleNamespace(snapshot={'before': {'camera_power': True}}, acquire=AsyncMock(),
                            release=AsyncMock(return_value=True))
    read_shutter = AsyncMock(side_effect=lambda _: shutter_open)

    async def recognize(packet, *, source_current):
        assert source_current()
        return dict(camera=CAMERA['id'], session='synthetic-stream', people=[], hands=[],
                    now=now, captured_mono=packet['received_at'])

    async def observation(value, process):
        observed.put_nowait((value['captured_mono'], process))

    recognition = SimpleNamespace(timing={}, observe=recognize, start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(previews, 'asyncio', SimpleNamespace(**(vars(asyncio) | {
        'wait_for': wait_for, 'create_subprocess_exec': spawn})))
    monkeypatch.setattr(previews, 'time', SimpleNamespace(time=lambda: now, monotonic=lambda: now))
    monkeypatch.setattr(previews, 'DATA', tmp_path)
    monkeypatch.setattr(previews, 'camera_is_open', read_shutter)
    monkeypatch.setattr(previews, 'camera_storage', lambda _: tmp_path)
    monkeypatch.setattr(previews, 'capture_environment', lambda: {})
    monkeypatch.setattr(ptz_control, 'Transport', lambda _: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(ptz_control, 'CameraLease', lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(camera_recognition, 'CameraRecognition', lambda *_: recognition)
    wall = CameraWall([CAMERA], clock=lambda: now)
    cleared = Mock()
    pool = previews.PreviewPool(continuous=True, recognize=True, wall=wall,
                                on_observation=observation, on_clear=cleared)
    camera_id = CAMERA['id']
    item = dict(state='connecting', mode='hold', mode_revision='synthetic-hold', received_at=0, frames=0,
                expires=now + 90, shutter_changed=False)
    pool.sessions[camera_id] = item

    def frame():
        jpeg = base64.b64encode(b'\xff\xd8SYNTHETIC-NOT-CAMERA-PIXELS\xff\xd9').decode()
        return json.dumps(dict(type='frame', jpeg=jpeg, preview=jpeg, received_at=now, preview_at=now,
                               width=640, height=360)).encode()

    owner = asyncio.create_task(pool._run(CAMERA, item))
    try:
        assert await asyncio.wait_for(receive_waits.get(), 2) == now
        native_packets.put_nowait(frame())
        assert await asyncio.wait_for(observed.get(), 2) == (now, capture)
        assert await asyncio.wait_for(receive_waits.get(), 2) == now
        assert pool.status(camera_id)['state'] == 'live' and wall.frame(camera_id)
        shutter_reads = read_shutter.await_count
        shutter_open = open_after_gap
        now += silence
        assert pool.status(camera_id)['state'] == 'stale'
        assert pool.frame(camera_id) is None and wall.frame(camera_id) is None
        assert pool.source_current(camera_id, capture)
        native_packets.put_nowait(frame())
        if outcome == 'live':
            assert await asyncio.wait_for(observed.get(), 2) == (now, capture)
            assert item['frames'] == 2 and pool.frame(camera_id) and wall.frame(camera_id)
            assert read_shutter.await_count > shutter_reads
            cleared.assert_not_called()
        else:
            await asyncio.wait_for(owner, 2)
            assert observed.empty() and item['frames'] == 1
            assert pool.frame(camera_id) is None and wall.frame(camera_id) is None
            assert not pool.source_current(camera_id, capture)
            assert capture.returncode == 0
            cleared.assert_called_once_with(camera_id)
        assert pool.status(camera_id)['state'] == outcome
        spawn.assert_awaited_once()
        assert all(json.loads(call.args[0]).get('action') != 'motor_step' for call in capture.stdin.write.call_args_list)
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
    lease.release.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',[None,'vision','registration','layout','identity','room_unavailable'])
async def test_original_secondary_recognizer_stops_at_the_first_retired_stage(monkeypatch,phase):
    from tests.embodied.test_room_process import room_port
    current=[True]
    identities=SimpleNamespace(discard=Mock())
    room=room_port()
    recognizer=camera_recognition.CameraRecognition.__new__(camera_recognition.CameraRecognition)
    recognizer.camera=CAMERA['id'];recognizer.session='synthetic-session'
    recognizer.identities=identities;recognizer.room=room
    recognizer.motion_generation=0;recognizer.motion_until=0
    recognizer.animals_enabled=False;recognizer.animals_intent=None
    recognizer.tracks=SimpleNamespace(update=Mock(return_value=[]),input_valid=True)
    recognizer.tracks.invalidate=lambda:setattr(recognizer.tracks,'input_valid',False)
    recognizer.pet_tracks=camera_recognition.PetTracks()
    recognizer.last={'old':'private'}
    def retire(at):
        if at==phase:current[0]=False
    async def read():
        retire('vision')
        return b'{"boxes":[],"poses":[],"hands":[],"faces":[]}\n'
    async def register(*_,**__):
        retire('registration')
        if phase=='room_unavailable':room.scene_visible=False
        return phase!='room_unavailable'
    async def layout(*_,**__):
        retire('layout')
        return {'registration':{'status':'locked'}}
    async def identity(*_,**__):retire('identity');return []
    room.register.side_effect=register;room.update.side_effect=layout
    monkeypatch.setattr(camera_recognition,'analyze',AsyncMock(side_effect=identity))
    recognizer.vision=SimpleNamespace(stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()),stdout=SimpleNamespace(readline=read))
    result=await recognizer.observe(dict(received_at=time.time(),jpeg=base64.b64encode(b'SYNTHETIC').decode(),width=640,height=360),source_current=lambda:current[0])
    if phase in {None,'room_unavailable'}:
        assert result is recognizer.last
        assert result['tracking_current'] is (phase is None)
        if phase=='room_unavailable':assert result['registration']['status']=='unavailable' and not result['scene_visible']
    else:
        assert result is None and recognizer.last is None
        identities.discard.assert_called()
    if phase=='vision':room.register.assert_not_called()
    if phase in {'vision','registration','room_unavailable'}:room.update.assert_not_called()
    if phase in {'vision','registration','layout'}:camera_recognition.analyze.assert_not_awaited()



@pytest.mark.asyncio
@pytest.mark.parametrize('phase', [None, 'before', 'safety', 'dispatch', 'enable'])
async def test_actual_worker_callback_cannot_rearm_or_dispatch_after_source_retirement(tmp_path, phase):
    process = SimpleNamespace(returncode=None)
    entry = dict(source_active=phase != 'before', process=process, state='live', privacy_closed=False)
    pool = previews.PreviewPool.__new__(previews.PreviewPool)
    pool.sessions = {CAMERA['id']: entry}
    service = SimpleNamespace(enabled=False, armed=True, selected=True, epoch=1, vad=[b'private'],
        pending_audio=object(), speaking=False, last_audio=None, audio_allowed=True,
        attention=SimpleNamespace(candidate=lambda: None), observe=AsyncMock())
    scope = dict(previews=pool, time=SimpleNamespace(monotonic=lambda: 10, time=lambda: 1000),
        practice=SimpleNamespace(observe=Mock()),
        safety=SimpleNamespace(offer=None, clear=Mock(), sound_requested=lambda _: False),
        fleet=SimpleNamespace(update=Mock(return_value=[]), clear=Mock(), cameras={}, voice=Mock(return_value=False)),
        wall=SimpleNamespace(observe=Mock()), secondary_homes={CAMERA['id']: service},
        SELECTED={'id': 'synthetic-primary'}, home=object(), PRIVATE=tmp_path, interaction_enabled=True)
    clear = actual('homeplane/embodied/worker.py', 'clear_camera', scope)
    actual('homeplane/embodied/worker.py', 'secondary_frame_current', scope)

    def retire(at):
        if at == phase:
            entry['source_active'] = False
            clear(CAMERA['id'])

    # Admission is synchronous; the later dispatch and enable remain async owners.
    def safety(*_, **__):retire('safety')
    async def dispatch(*_, **__):retire('dispatch')
    async def enable():
        service.enabled = True
        retire('enable')
    scope['safety'].offer = Mock(side_effect=safety)
    scope['dispatch_interactions'] = AsyncMock(side_effect=dispatch)
    service.enable = AsyncMock(side_effect=enable)
    callback = actual('homeplane/embodied/worker.py', 'secondary_observation', scope)
    await callback(dict(camera=CAMERA['id'], session='synthetic-session', people=[], hands=[], faces=[],
        registration={'status': 'locked'}, now=10, captured_mono=9.8, scene_visible=True), process)
    if phase == 'before':
        scope['practice'].observe.assert_not_called()
        scope['safety'].offer.assert_not_called()
    if phase in {'before', 'safety'}:
        scope['wall'].observe.assert_not_called()
        scope['dispatch_interactions'].assert_not_awaited()
    if phase in {'before', 'safety', 'dispatch'}:service.enable.assert_not_awaited()
    if phase is None:
        service.observe.assert_awaited_once()
        assert service.observe.await_args.kwargs['captured_mono'] == 9.8
        assert service.armed is True
    else:
        service.observe.assert_not_awaited()
        if phase != 'before':
            assert not service.armed and not service.selected and not service.audio_allowed
            assert not service.vad and service.pending_audio is None


@pytest.mark.asyncio
@pytest.mark.parametrize('ownership', ['current', 'removed', 'replacement'])
async def test_original_retirement_fails_pending_motion_once_and_does_not_clear_a_replacement(ownership):
    replacement = ownership == 'replacement'
    wall = CameraWall([CAMERA])
    old_epoch = wall.begin(CAMERA['id'])
    old_process, new_process = SimpleNamespace(returncode=None), SimpleNamespace(returncode=None)
    old = dict(source_active=True, process=old_process, state='live', control_epoch=2,
               frame=b'private', received_at=1000, people=1, hands=1, animals=1)
    new = dict(source_active=True, process=new_process, state='live', privacy_closed=False)
    cleared = Mock()
    pool = previews.PreviewPool.__new__(previews.PreviewPool)
    pool.sessions = {CAMERA['id']: new if replacement else old}
    if ownership == 'removed':pool.sessions.clear()
    pool.wall = wall;pool.on_clear = cleared
    if replacement:
        wall.begin(CAMERA['id'])
        wall.frames[CAMERA['id']] = (b'replacement', time.time())
    pending = asyncio.get_running_loop().create_future()
    acknowledged = asyncio.get_running_loop().create_future()
    acknowledged.set_result({'native_code': 0})
    scope = dict(latest={'private': 'old'}, retired=False, item=old, self=pool, selected=CAMERA,
                 wall_epoch=old_epoch, waiters={'pending': pending, 'ack': acknowledged})
    retire = actual('homeplane/embodied/previews.py', 'retire_source', scope)
    retire('source_closed');retire('capture_stream_unavailable')
    with pytest.raises(RuntimeError, match='^source_closed$'):await pending
    assert acknowledged.result() == {'native_code': 0}
    assert not old['source_active'] and old['frame'] is None and scope['latest'] is None
    assert old['control_epoch'] == 3
    if replacement:
        cleared.assert_not_called()
        assert wall.frame(CAMERA['id'])[0] == b'replacement'
        assert pool.source_current(CAMERA['id'], new_process)
    else:
        cleared.assert_called_once_with(CAMERA['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['clear', 'recognizer', 'pipeline'])
async def test_cleanup_failure_cannot_skip_remaining_owned_resources(fault):
    tree = ast.parse(Path('homeplane/embodied/previews.py').read_text())
    owner = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run')
    final = next(n for n in owner.body if isinstance(n, ast.Try)).finalbody
    function = ast.parse('async def cleanup():\n pass').body[0]
    function.body = copy.deepcopy(final)
    tasks = [asyncio.create_task(asyncio.sleep(60)) for _ in range(3)]
    process = SimpleNamespace(returncode=None, wait=AsyncMock())
    def terminate():process.returncode = 0
    process.terminate = process.kill = terminate
    recognizer = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('synthetic-cleanup-failure')
        if fault == 'recognizer' else None))
    pipeline = None
    if fault == 'pipeline':
        recognizer = None
        pipeline = SimpleNamespace(close=Mock(side_effect=RuntimeError('synthetic-cleanup-failure')))
    lease, io = SimpleNamespace(release=AsyncMock(return_value=True)), SimpleNamespace(close=AsyncMock())
    item = {'manual_tasks': {tasks[0]}, 'control_epoch': 1, 'recognizer': recognizer, 'frame': b'old-private-frame'}
    scope = dict(asyncio=asyncio, item=item,
        retire_source=Mock(side_effect=RuntimeError('synthetic-cleanup-failure') if fault == 'clear' else None),
        motion_task=tasks[1], receiver=tasks[2], recognizer=recognizer, pipeline=pipeline,
        process=process, lease=lease, io=io, close_identity=close_identity)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 '<original secondary owner cleanup>', 'exec'), scope)
    try:
        with pytest.raises(RuntimeError, match='^synthetic-cleanup-failure$'):
            await scope['cleanup']()
        if recognizer is not None:recognizer.close.assert_awaited_once()
        if pipeline is not None:pipeline.close.assert_called_once()
        assert item['recognizer'] is None and item['frame'] is None
        assert all(t.done() for t in tasks)
        assert process.returncode == 0
        lease.release.assert_awaited_once();io.close.assert_awaited_once()
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_race', [False, True])
async def test_identity_cleanup_error_still_reaps_the_original_native_vision_child(exit_race):
    recognizer = camera_recognition.CameraRecognition.__new__(camera_recognition.CameraRecognition)
    recognizer.identities = SimpleNamespace(close=Mock(side_effect=RuntimeError('synthetic-identity-close')))
    process = SimpleNamespace(returncode=None)
    process.terminate = Mock(side_effect=ProcessLookupError() if exit_race else None)
    async def wait():process.returncode = 0;return 0
    process.wait = AsyncMock(side_effect=wait)
    recognizer.vision = process
    recognizer.room = None
    with pytest.raises(RuntimeError, match='^synthetic-identity-close$'):
        await recognizer.close()
    process.terminate.assert_called_once();process.wait.assert_awaited_once()
    assert process.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('camera', ['primary', 'secondary'])
@pytest.mark.parametrize('change', [None, 'retire', 'replace'])
async def test_queued_original_gesture_rechecks_source_even_if_clear_callback_failed(monkeypatch, camera, change):
    from homeplane.embodied import bridge
    primary = 'synthetic-primary'
    cid = primary if camera == 'primary' else CAMERA['id']
    process = SimpleNamespace(returncode=None)
    entry = dict(source_active=True, process=process, state='live', privacy_closed=False,
                 recognizer=SimpleNamespace(session='original-session'))
    pool = previews.PreviewPool.__new__(previews.PreviewPool)
    pool.sessions = {CAMERA['id']: entry}
    jobs = []
    scope = dict(SELECTED={'id': primary}, identities=SimpleNamespace(session='original-session'),
        state={'status': 'live'}, previews=pool, interaction_enabled=True,
        games=MotionGames(),party_camera=PartyCamera(),
        home=SimpleNamespace(spawn=jobs.append, armed=True),
        secondary_homes={CAMERA['id']: SimpleNamespace(armed=True)},
        fleet=SimpleNamespace(current=Mock(return_value=True), receipts=deque()),
        practice=SimpleNamespace(suppresses=lambda _: False), wall=SimpleNamespace(notice=Mock()),
        time=SimpleNamespace(monotonic=lambda: 10, time=lambda: 1000))
    command = AsyncMock(return_value={'status': 'synthetic', 'acknowledged': False, 'reached_target': False})
    monkeypatch.setattr(bridge, 'command', command)
    actual('homeplane/embodied/worker.py', 'interaction_source_current', scope)
    dispatch = actual('homeplane/embodied/worker.py', 'dispatch_interactions', scope)
    event = dict(camera=cid, session='original-session', action='on', target='synthetic-light',
                 event_id='synthetic-event', binding='synthetic-binding', revision=1, observed_mono=10)
    await dispatch([event])
    assert len(jobs) == 1
    # Keep the old service/fleet permissive on purpose: source ownership must
    # independently refuse this delayed action when normal cleanup failed.
    if change == 'retire':
        if camera == 'primary':scope['state']['status'] = 'unavailable'
        else:entry['source_active'] = False
    elif change == 'replace':
        if camera == 'primary':scope['identities'].session = 'replacement-session'
        else:entry['recognizer'].session = 'replacement-session'
    await jobs[0]
    if change is None:
        command.assert_awaited_once()
        assert command.call_args.kwargs['gesture'] == dict(binding='synthetic-binding', revision=1, observed_mono=10)
    else:
        command.assert_not_awaited()
        scope['wall'].notice.assert_not_called()
        assert not scope['fleet'].receipts
