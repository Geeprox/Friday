"""Exercise actual motion policy, dispatch and SDK boundary with inert fakes."""
import ast
import asyncio
import ctypes
import json
from pathlib import Path
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.local_motor import LocalMotor
from homeplane.embodied.local_motor import check_motor_deadline
from tests.embodied.test_camera_director import patrol_view
from tests.embodied.test_review_motion import recorded_sound_only


def view(at, *, x=.78, visible=True, tracking=True, session='source', people=None):
    return dict(status='live', _local_received=at, session_id=session,
                scene_visible=visible, tracking_current=tracking, _patrol_view=patrol_view(at),
                people=people if people is not None else [dict(id=1,
                    box=dict(x=x, y=.1, w=.12, h=.7), identity={})])


def failed_director():
    director = CameraDirector()
    for at in (1, 5, 9):
        director.failed(at)
    return director


def test_failure_recovery_uses_new_direction_and_never_replays_old_target():
    director = failed_director()

    def registered_view(at, x):
        frame = view(at, x=x)
        frame['people'][0]['identity'] = dict(state='matched', person_id='synthetic-target')
        return frame

    assert director.choose(registered_view(68, .05), 68) is None
    assert director.choose(registered_view(69, .05), 69) is None
    assert director.choose(registered_view(69.2, .05), 69.2) == 'left'
    # Reserving the probe before its outcome prevents an unknown result from
    # being retried on every subsequent frame.
    assert director.choose(registered_view(70, .05), 70) is None
    assert director.failed_moves == 3
    director.sent(70, 'left')
    assert director.failed_moves == 0
    assert director.choose(registered_view(74, .78), 74) is None
    assert director.choose(registered_view(74.2, .78), 74.2) == 'right'


def test_recovery_still_requires_current_video_and_honors_hold():
    director = failed_director()
    assert director.choose(view(70, tracking=False), 70) is None
    assert director.choose(view(70), 72) is None
    director.apply_mode('hold', 1)
    assert director.choose(view(73), 73) is None
    director.apply_mode('auto', 2)
    assert director.choose(view(74), 74) is None
    assert director.choose(view(74.2), 74.2) == 'right'


def test_a_late_pre_move_frame_cannot_start_the_next_impulse():
    director = CameraDirector()
    assert director.choose(view(1), 1) is None
    assert director.choose(view(1.1), 1.1) == 'right'
    director.sent(1.2)
    assert director.choose(view(1.15), 3.2) is None
    assert director.choose(view(1.18), 3.3) is None
    assert director.visible_samples == 0


@pytest.mark.parametrize('change', ['center', 'direction', 'lost', 'ambiguous', 'invalid', 'session', 'game', 'focus', 'hold'])
def test_follow_decision_is_rechecked_after_the_view_or_intent_changes(change):
    director = CameraDirector()
    director.apply_mode('auto', 1)
    director.choose(view(1), 1)
    assert director.choose(view(1.1), 1.1) == 'right'
    current = view(1.2)
    kwargs = {}
    if change == 'center': current = view(1.2, x=.44)
    if change == 'direction': current = view(1.2, x=.05)
    if change == 'lost': current = view(1.2, people=[])
    if change == 'ambiguous':
        current = view(1.2, people=[dict(id=2, box=dict(x=.8, y=.1, w=.1, h=.7)),
                                         dict(id=3, box=dict(x=.1, y=.1, w=.1, h=.7))])
    if change == 'invalid': current = view(1.2, tracking=False)
    if change == 'session': current = view(1.2, session='replacement')
    if change == 'game': current['game'] = dict(status='playing')
    if change == 'focus':
        current['people'].append(dict(id=2, box=dict(x=.2, y=.1, w=.12, h=.7), identity={}))
        kwargs['focus'] = dict(track=2, at=1.2, session_id=current['session_id'])
    if change == 'hold': director.apply_mode('hold', 2)
    assert not director.decision_current(current, 1.2, 'right', **kwargs)
    assert director.survey_attempts == 0


def test_current_follow_and_empty_room_guards_accept_only_their_original_purpose():
    director = CameraDirector()
    director.choose(view(1), 1)
    assert director.choose(view(1.1), 1.1) == 'right'
    assert director.decision_current(view(1.2), 1.2, 'right')
    director.sent(1.2)
    director.choose(view(20, people=[]), 20)
    assert director.choose(view(20.2, people=[]), 20.2) == 'right'
    assert director.decision_current(view(20.3, people=[]), 20.3, 'right')
    for anchor in (None, patrol_view(20.3, x=.04), patrol_view(20.3, basis='replacement')):
        current = dict(view(20.3, people=[]), _patrol_view=anchor)
        assert not director.decision_current(current, 20.3, 'right')
    assert not director.decision_current(view(20.3), 20.3, 'right')
    assert not director.decision_current(view(20.3, people=[]), 20.3, 'right', occupied_elsewhere=True)


def test_auto_patrol_requires_a_live_reference_before_pulsing():
    director = CameraDirector()
    for at in (0, .5, 16, 17):
        assert director.choose(dict(view(at, people=[]), _patrol_view=None), at) is None
    assert director.phase == 'patrol_waiting_view'
    assert director.choose(view(18, people=[]), 18) == 'right'


def extract(path, names, scope):
    tree = ast.parse(Path(path).read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, 'exec'), scope)


def primary_scope():
    from homeplane.embodied.calibration_frames import CalibrationFrames
    now = time.monotonic()
    sent = []
    noop = lambda *_, **__: None
    scope = dict(asyncio=asyncio, time=time, secrets=secrets, json=json,
                 state=dict(status='live', tracking_current=True), last_frame=now,
                 camera_planner=SimpleNamespace(mode='auto', intent_revision='original'),
                 camera_lease=SimpleNamespace(snapshot=dict(status='owned')),
                 camera_is_open=AsyncMock(return_value=True), close_primary_view=noop,
                 SELECTED=dict(id='synthetic'), motor_sequence=0, motion_generation=0,
                 motor_last_sent=-100, motor_waiters={}, room_lock=asyncio.Lock(),calibration_frames=CalibrationFrames(),
                 room=SimpleNamespace(expect_motion=noop), identities=SimpleNamespace(discard=noop),
                 safety=SimpleNamespace(clear_view=noop),
                 fleet=SimpleNamespace(clear=noop),
                 home=SimpleNamespace(selected=True, epoch=0, vad=[], pending_audio=None,
                                      runtime_log=SimpleNamespace(record=noop)))
    def write(data):
        command = json.loads(data)
        sent.append(command)
        scope['motor_waiters'][command['request_id']].set_result(dict(native_code=0))
    scope['capture'] = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()))
    extract('homeplane/embodied/worker.py', {'motor_step'}, scope)
    return scope, sent


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['tracking', 'post_move', 'decision'])
async def test_primary_revalidates_actual_inputs_after_waiting(change):
    scope, sent = primary_scope()
    current = True
    class Wait:
        async def __aenter__(self):
            nonlocal current
            await asyncio.sleep(0)
            if change == 'tracking': scope['state']['tracking_current'] = False
            if change == 'post_move': scope['motor_last_sent'] = scope['last_frame'] + .01
            if change == 'decision': current = False
        async def __aexit__(self, *_): pass
    scope['room_lock'] = Wait()
    with pytest.raises(RuntimeError, match='stale_input'):
        await scope['motor_step']('right', scope['last_frame'], expected_intent='original', decision_current=lambda: current)
    assert not sent and not scope['motor_waiters']
    assert scope['motion_generation'] == 0


@pytest.mark.asyncio
async def test_primary_reserves_motion_before_first_await_and_bounds_child_command():
    scope, sent = primary_scope()
    entered = asyncio.Event()
    release = asyncio.Event()
    async def is_open(_):
        entered.set()
        await release.wait()
        return True
    scope['camera_is_open'] = is_open
    first = asyncio.create_task(scope['motor_step']('left'))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        with pytest.raises(RuntimeError, match='motor_busy'):
            await asyncio.wait_for(scope['motor_step']('right'), .1)
    finally:
        release.set()
        await first
    assert len(sent) == 1 and not scope['motor_waiters']
    assert 0 < sent[0]['expires_mono'] - time.monotonic() <= 1.5


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['direction', 'tracking', 'post_move', 'epoch', 'privacy', 'source', 'review', 'fall'])
async def test_secondary_checks_latest_observation_after_privacy_read(change):
    director = CameraDirector()
    now = time.monotonic()
    director.choose(view(now-.1), now-.1)
    assert director.choose(view(now), now) == 'right'
    noop = lambda *_, **__: None
    sent = []
    scope = dict(asyncio=asyncio, time=time, secrets=secrets, json=json, director=director,
                 item=dict(privacy_closed=False, control_epoch=1, last_motor_sent=-100, source_active=True),
                 motion_lock=asyncio.Lock(), selected=dict(id='synthetic'),
                 process=SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=sent.append, drain=AsyncMock())),
                 waiters={}, recognizer=None, privacy_closed=noop,
                 self=SimpleNamespace(on_motion=None, focus=lambda _: None, occupied_elsewhere=lambda _: False,
                                      review_pending=lambda *_:scope.get('review_pending',False),
                                      fall_pending=lambda *_:scope.get('fall_pending',False),
                                      sound_cue=lambda *_:None,
                                      runtime_log=SimpleNamespace(record=noop)),
                 latest=dict(now=now, captured_mono=now, session='source', tracking_current=True,
                             people=view(now)['people'], scene_visible=True))
    async def read_power(_):
        await asyncio.sleep(0)
        if change == 'direction': scope['latest']['people'] = view(now, x=.05)['people']
        if change == 'tracking': scope['latest']['tracking_current'] = False
        if change == 'post_move': scope['item']['last_motor_sent'] = now + .01
        if change == 'epoch': scope['item']['control_epoch'] = 2
        if change == 'privacy': scope['item']['privacy_closed'] = True
        if change == 'source': scope['item']['source_active'] = False
        if change == 'review': scope['review_pending'] = True
        if change == 'fall':
            scope['fall_pending'] = True
        return True
    scope['camera_is_open'] = read_power
    extract('homeplane/embodied/previews.py', {'current_sample', 'send_motion'}, scope)
    reason = {'epoch':'camera_intent_changed', 'privacy':'privacy_shutter_closed',
              'source':'capture_source_changed'}.get(change, 'stale_input')
    with pytest.raises(RuntimeError, match=reason):
        await scope['send_motion']('right', now, 1)
    assert not sent and not scope['waiters'] and director.failed_moves == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[None,'target','review','fall'])
async def test_primary_search_original_guard_rechecks_the_current_target(change):
    from homeplane.embodied.party_camera import PartyCamera
    scope, sent = primary_scope()
    at = scope['last_frame']
    holder=PartyCamera();scope['party_camera']=holder
    def current(stamp):
        return dict(view(stamp),party_camera=holder.status(camera=scope['SELECTED']['id'],
            session=view(stamp)['session_id'],frame_mono=stamp,now=stamp))
    director = CameraDirector()
    director.choose(current(at-.1), at-.1)
    assert director.choose(current(at), at) == 'right'
    scope['state'].update(current(at))
    scope.update(director=director, direction='right', games=SimpleNamespace(public={}),
                 previews=SimpleNamespace(sessions={}),
                 safety=SimpleNamespace(clear_view=lambda _:None, review_pending=lambda *_:False,fall_pending=lambda *_:False,
                                        sound_search_cue=lambda *_:None))
    scope['camera_motion_cue'] = recorded_sound_only(scope['safety'])
    extract('homeplane/embodied/worker.py', {'decision_current'}, scope)
    class Wait:
        async def __aenter__(self):
            await asyncio.sleep(0)
            if change=='target':scope['state']['people'] = view(at, x=.05)['people']
            elif change=='review':scope['safety'].review_pending=lambda *_:True
            elif change == 'fall':
                scope['safety'].fall_pending = lambda *_: True
        async def __aexit__(self, *_): pass
    scope['room_lock'] = Wait()
    if change is None:
        await scope['motor_step']('right',at,expected_intent='original',decision_current=scope['decision_current'])
        assert len(sent)==1
    else:
        with pytest.raises(RuntimeError, match='stale_input'):
            await scope['motor_step']('right', at, expected_intent='original', decision_current=scope['decision_current'])
        assert not sent
    assert not scope['motor_waiters']


@pytest.mark.asyncio
async def test_primary_expired_manual_wait_preserves_current_observation():
    scope, sent = primary_scope()
    clock = [scope['last_frame']]
    scope['time'] = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    class Wait:
        async def __aenter__(self):
            await asyncio.sleep(0)
            clock[0] += 1.6
        async def __aexit__(self, *_): pass
    scope['room_lock'] = Wait()
    with pytest.raises(RuntimeError, match='motor_request_expired'):
        await scope['motor_step']('left')
    assert not sent and not scope['motor_waiters'] and scope['motion_generation'] == 0


@pytest.mark.asyncio
async def test_cancelled_pre_send_releases_reservation_and_logs_the_actual_stage():
    scope, sent = primary_scope()
    entered = asyncio.Event()
    never = asyncio.Event()
    events = []
    scope['home'].runtime_log.record = lambda *args, **kwargs: events.append(kwargs)
    async def read_power(_):
        entered.set()
        await never.wait()
    scope['camera_is_open'] = read_power
    task = asyncio.create_task(scope['motor_step']('left'))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not sent and not scope['motor_waiters'] and scope['motion_generation'] == 0
    assert events[-1]['code'] == 'control_cancelled_before_dispatch'


def inert_motor(monkeypatch, clock):
    from homeplane.embodied import local_motor
    monkeypatch.setattr(local_motor.time, 'monotonic', lambda: clock[0])
    # Only fake SDK structs are accessed; LocalMotor.__init__ is deliberately
    # bypassed so no library, camera, native code or device is loaded.
    session = ctypes.c_uint32(4)
    outer = ctypes.create_string_buffer(80)
    ctypes.c_void_p.from_buffer(outer, 0x38).value = ctypes.addressof(session)
    ctypes.c_uint32.from_buffer(outer, 0x40).value = 4
    motor = object.__new__(LocalMotor)
    motor.camera = SimpleNamespace(_c_instance=ctypes.c_void_p(ctypes.addressof(outer)))
    motor.last = -10
    sent = []
    motor.send = lambda *args: sent.append(args) or 0
    return motor, sent, (outer, session)


@pytest.mark.parametrize('deadline', [None, True, float('nan'), float('inf'), 12])
def test_native_boundary_rejects_missing_invalid_or_overlong_expiry(monkeypatch, deadline):
    motor, sent, keepalive = inert_motor(monkeypatch, [10])
    with pytest.raises((ValueError, RuntimeError)):
        motor.step('left', expires_mono=deadline)
    assert not sent


def test_native_boundary_rechecks_after_thread_wait_and_before_sdk_send(monkeypatch):
    motor, sent, keepalive = inert_motor(monkeypatch, [10])
    with pytest.raises(RuntimeError, match='motor_request_expired'):
        motor.step('left', expires_mono=9.9)
    assert not sent
    result = motor.step('left', expires_mono=11)
    assert len(sent) == 1 and result['physical_movement_verified'] is False


def test_native_expiry_during_session_preparation_never_calls_sdk(monkeypatch):
    from homeplane.embodied import local_motor
    clock = [10]
    motor, sent, keepalive = inert_motor(monkeypatch, clock)
    dumps = json.dumps
    def delayed_payload(*args, **kwargs):
        clock[0] = 11.1
        return dumps(*args, **kwargs)
    monkeypatch.setattr(local_motor.json, 'dumps', delayed_payload)
    with pytest.raises(RuntimeError, match='motor_request_expired'):
        motor.step('right', expires_mono=11)
    assert not sent and motor.last == -10


@pytest.mark.asyncio
@pytest.mark.parametrize('expired_in_thread', [False, True])
async def test_capture_controls_pass_expiry_through_the_actual_child_and_native_method(monkeypatch, expired_in_thread):
    clock = [10]
    motor, sent, keepalive = inert_motor(monkeypatch, clock)
    outputs = []
    command = dict(action='motor_step', request_id=1, direction='left',
                   expires_mono=11 if expired_in_thread else 9.9)
    reader = SimpleNamespace(readline=AsyncMock(side_effect=[json.dumps(command).encode(), b'']))
    calls = []
    async def delayed_thread(function, *args, **kwargs):
        calls.append(True)
        clock[0] = 11.1
        return function(*args, **kwargs)
    scope = dict(reader=reader, json=json, request=dict(motor=True), motor=motor,
                 emit=outputs.append, check_motor_deadline=check_motor_deadline,
                 asyncio=SimpleNamespace(to_thread=delayed_thread), time=time)
    source = ast.parse(Path('homeplane/embodied/capture_worker.py').read_text())
    controls = next(node for node in ast.walk(source) if isinstance(node, ast.AsyncFunctionDef) and node.name == 'controls')
    factory = ast.parse('async def factory():\n    audio_until = attention_until = video_until = 0\n    return controls').body[0]
    factory.body.insert(1, controls)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])), '<actual capture controls>', 'exec'), scope)
    await (await scope['factory']())()
    assert calls == ([True] if expired_in_thread else [])
    assert not sent and len(outputs) == 1
    assert outputs[0]['request_id'] == 1 and outputs[0]['error_code'] == 'motor_request_expired'
