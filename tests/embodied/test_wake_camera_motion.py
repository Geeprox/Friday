"""合成收音、人物观测和云台管道贯通唤醒转向。"""

import asyncio
import base64
import json
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeplane.embodied import home_service, voice_worker
from homeplane.embodied.camera_audio import AudioIngress, PCMFramer, WakeCameraCue, deliver_audio
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.intent import address
from homeplane.embodied.party_camera import PartyCamera
from tests.embodied.test_camera_audio import CAMERA, audio, video, voice_service
from tests.embodied.test_camera_director import patrol_view, person, sample
from tests.embodied.test_camera_follow_appearance import picture
from tests.embodied.test_motor_recovery import extract, primary_scope
from tests.embodied.test_review_motion import source_function


SESSION = 'SYNTHETIC-wake-view'
WORKER = 'homeplane/embodied/worker.py'
PREVIEWS = 'homeplane/embodied/previews.py'


def current_frame(at, source, *, offset=0, people=()):
    return dict(sample(at, people=people, x=offset, session=SESSION),
                tracking_current=True, _wake_source=source)


def subject(track, x):
    row = person(track, x, identity=f'synthetic-person-{track}')
    row['box']['confidence'] = .92
    return row


async def hear_wake(tmp_path, monkeypatch, clock, word, *, text=None, voiced_frames=6):
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'voice-input-paused')
    monkeypatch.setattr(home_service, 'time', clock)
    h = voice_service(home_service)
    h.camera_id, h.camera_session = CAMERA['id'], SESSION
    h.voice_preferences = lambda: SimpleNamespace(wake_word=word, revision=7)
    h.runtime_log = SimpleNamespace(record=Mock())
    h.transcribe = home_service.HomeService.transcribe.__get__(h)
    jobs = []
    h.spawn = lambda coro: jobs.append(asyncio.create_task(coro))
    text = word if text is None else text
    h.request = AsyncMock(return_value=dict(
        text=text, voice_revision=7, address=address(text, False, wake_word=word)))
    h.command = AsyncMock()
    # 先给出完整的语音活动，再用连续的静音帧关闭语音段。
    measurements = [(True, 1.0, .03)] * voiced_frames + [(False, 0.0, .004)] * 4
    h.activity = SimpleNamespace(probability=.9, measure=Mock(side_effect=measurements))
    capture = 'a' * 32
    gate = AudioIngress(CAMERA, capture)
    framer = PCMFramer(CAMERA, capture)
    for index in range(len(measurements)):
        clock.at = index * .2
        h.last_video = clock.at
        assert video(gate, clock.at)
        admitted = gate.accept(audio(clock.at, framer=framer), wall=clock.time(), mono=clock.at)
        assert await deliver_audio(h, admitted)
    await asyncio.gather(*jobs)
    assert h.request.await_count == int(voiced_frames >= 2)
    h.command.assert_not_awaited()
    return h, (gate.session, gate.context)


def cue_source(h, clock, owner):
    quiet = SimpleNamespace(review_pending=Mock(return_value=False),
                            fall_pending=Mock(return_value=False),
                            sound_search_cue=Mock(return_value=None), clear_view=Mock())
    selected = CAMERA if owner == 'primary' else {'id': 'cam_2222222222'}
    scope = dict(time=clock, SELECTED=selected, home=h if owner == 'primary' else None,
                 secondary_homes={} if owner == 'primary' else {h.camera_id: h},
                 interaction_enabled=True, safety=quiet, WakeCameraCue=WakeCameraCue)
    return source_function(WORKER, 'camera_motion_cue', scope), quiet


def dispatch_for(owner, state, clock, director, h, cue, safety, party, *, recognizer=None, room_lock=None):
    """执行主、副镜头原来的发送函数，原生端只以管道回执替身回应。"""
    if owner == 'primary':
        scope, sent = primary_scope()
        scope.update(time=clock, SELECTED=CAMERA, home=h, state=state, director=director,
                     safety=safety, previews=SimpleNamespace(sessions={}), direction='right',
                     games=SimpleNamespace(public={}), party_camera=party, camera_motion_cue=cue)
        extract(WORKER, {'decision_current'}, scope)

        async def send(direction):
            scope.update(last_frame=state['_local_received'], direction=direction)
            await scope['motor_step'](direction, state['_local_received'], expected_intent='original',
                                      decision_current=scope['decision_current'])
            director.sent(clock.at, direction)
    else:
        sent = []
        waiters = {}

        def write(data):
            command = json.loads(data)
            sent.append(command)
            waiters[command['request_id']].set_result(dict(native_code=0))

        motion = SimpleNamespace(focus=lambda _: None, occupied_elsewhere=lambda _: False,
                                 review_pending=safety.review_pending, fall_pending=safety.fall_pending,
                                 sound_cue=cue, on_motion=None, runtime_log=h.runtime_log)
        item = dict(privacy_closed=False, source_active=True, control_epoch=1)
        scope = dict(time=clock, asyncio=asyncio, secrets=secrets, json=json,
                     director=director, selected=CAMERA, self=motion, item=item, waiters=waiters,
                     process=SimpleNamespace(returncode=None,
                         stdin=SimpleNamespace(write=write, drain=AsyncMock())),
                     camera_is_open=AsyncMock(return_value=True), motion_lock=asyncio.Lock(),
                     recognizer=recognizer, room_lock=room_lock or asyncio.Lock(), privacy_closed=Mock())
        extract(PREVIEWS, {'current_sample', 'send_motion'}, scope)

        async def send(direction):
            capture, context = state['_wake_source']
            scope['latest'] = dict(state, session=SESSION, now=state['_local_received'],
                                   capture_session=capture, camera_source_context=context)
            await scope['send_motion'](direction, state['_local_received'], 1)
    return send, sent


@pytest.mark.asyncio
@pytest.mark.parametrize('owner, word', [('primary', 'Friday'), ('secondary', '小福')])
@pytest.mark.parametrize('target_side, target_x', [('left', .10), ('right', .76)])
@pytest.mark.parametrize('known_face', [False, True])
async def test_wake_search_turns_and_follows_on_the_same_capture(
        tmp_path, monkeypatch, owner, word, target_side, target_x, known_face):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, word)
    assert home.wake_camera.source == source and not home.camera_focus
    cue, safety = cue_source(home, clock, owner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party = PartyCamera()
    state = {}
    send, sent = dispatch_for(owner, state, clock, director, home, cue, safety, party)

    def frame(at, offset, people):
        value = current_frame(at, source, offset=offset, people=people)
        if not known_face:
            value.update(
                people=[dict(person, identity={}) for person in people],
                _follow_jpeg=picture([(person, 7) for person in people]),
            )
        if owner == 'primary':
            value['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        return value

    def observe(at, *, offset=0, people=()):
        clock.at = at
        state.update(frame(at, offset, people))
        wake = cue(CAMERA['id'], state, director.sound_search_cue)
        result = director.choose(state, at, sound_cue=wake)
        assert director.take_sound_view() is None
        return result

    assert director.choose(frame(1.0, 0, ()), 1.0) is None
    assert observe(1.8) == 'right'
    await send('right')
    assert [(row['action'], row['direction']) for row in sent] == [('motor_step', 'right')]

    found = subject(7, target_x)
    assert observe(3.9, offset=.05, people=[found]) is None
    assert director.phase == 'confirming_view'
    assert observe(4.3, offset=.05, people=[found]) is None
    # 背景已确认转过，同一人体连续两帧即可对准，无须等人脸匹配。
    assert observe(4.7, offset=.05, people=[found]) == target_side
    if not known_face:
        assert director.pending_decision['appearance'] is not None
    await send(target_side)
    assert [row['direction'] for row in sent] == ['right', target_side]

    centered = subject(9, .43)
    centered['identity'] = found['identity']
    offset = 0 if target_side == 'left' else .1
    assert observe(7.1, offset=offset, people=[centered]) is None
    assert observe(7.5, offset=offset, people=[centered]) is None
    assert not director.public()['target_locked']
    assert observe(7.9, offset=offset, people=[centered]) is None
    assert director.target == 9 and director.public()['target_locked']
    assert not director.public()['physical_motion_verified']
    assert not home.attention.latest
    assert cue('cam_3333333333', state, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
@pytest.mark.parametrize('shift', [.05, 0], ids=['turned', 'unchanged'])
async def test_wake_search_resumes_after_the_followup_utterance(tmp_path, monkeypatch, owner, shift):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    original = home.wake_camera
    cue, safety = cue_source(home, clock, owner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party, state = PartyCamera(), {}
    send, sent = dispatch_for(owner, state, clock, director, home, cue, safety, party)
    found = subject(7, .605)

    def observe(at, offset=0, *people, with_wake=True):
        clock.at = at
        state.update(current_frame(at, source, offset=offset, people=people))
        if owner == 'primary':
            state['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        wake = cue(CAMERA['id'], state, director.sound_search_cue) if with_wake else None
        return director.choose(state, at, sound_cue=wake)

    assert observe(1.0, with_wake=False) is None
    assert observe(1.8) == 'right'
    await send('right')

    # 转动后短暂没有识别帧。主镜头仍带原会话，副镜头暂时无观测。
    clock.at = 2.3
    no_frame = dict(status='connecting', session_id=None, _wake_source=None, _local_received=-100)
    if owner == 'primary':
        no_frame.update(session_id=SESSION, _wake_source=source, party_camera=state['party_camera'])
    assert cue(CAMERA['id'], no_frame, director.sound_search_cue) is None
    assert home.wake_camera is original
    assert director.choose(no_frame, clock.at) is None

    # 后续语句的采集和识别跨过第一次转动的两次画面确认。
    people = (found,) if shift else ()
    home.speaking = True
    assert observe(3.9, shift, *people) is None
    assert observe(4.3, shift, *people) is None
    home.speaking = False
    home.voice_busy = True
    assert observe(4.7, shift, *people) is None
    assert director.sound_search_cue is original and director.sound_step == 1
    assert [row['direction'] for row in sent] == ['right']

    home.voice_busy = False
    if not shift:
        assert observe(5.1) == 'left'
        await send('left')
        assert [row['direction'] for row in sent] == ['right', 'left']
        return

    assert observe(5.1, .05, found) is None
    assert observe(5.5, .05, found) == 'right'
    home.speaking = True
    with pytest.raises(RuntimeError, match='stale_input'):
        await send('right')
    assert observe(5.9, .05, found) is None
    assert [row['direction'] for row in sent] == ['right']
    home.speaking = False
    assert observe(5.9, .05, found) is None
    assert observe(6.3, .05, found) is None
    assert observe(6.7, .05, found) == 'right'
    await send('right')

    centered = dict(subject(9, .43), identity=found['identity'])
    for at in (9.1, 9.5):
        assert observe(at, .10, centered) is None
        assert not director.public()['target_locked']
    assert observe(9.9, .10, centered) is None
    assert director.target == 9 and director.public()['target_locked']
    assert [row['direction'] for row in sent] == ['right', 'right']


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', ['session', 'source', 'policy', 'pause', 'expired'])
async def test_wake_does_not_resume_after_revocation(tmp_path, monkeypatch, revoked):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    original = home.wake_camera
    home.voice_busy = True
    clock.at += .1
    frame = current_frame(clock.at, source)
    assert home.wake_motion_cue(frame, clock.at).cue is original
    if revoked == 'session':
        frame['session_id'] = 'SYNTHETIC-replaced-view'
    elif revoked == 'source':
        frame['_wake_source'] = ('b' * 32, source[1])
    elif revoked == 'policy':
        home.voice_preferences = lambda: SimpleNamespace(wake_word='小福', revision=8)
    elif revoked == 'pause':
        home_service.VOICE_INPUT_PAUSED.touch()
    else:
        clock.at += home_service.WAKE_CAMERA_SECONDS
    assert home.wake_motion_cue(frame, clock.at) is None
    assert home.wake_camera is None
    home.voice_busy = False
    assert home.wake_motion_cue(current_frame(clock.at, source), clock.at) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner, direction, x, shift', [
    ('primary', 'right', .605, .04),
    ('secondary', 'left', .245, -.04),
])
@pytest.mark.parametrize('result, remaining', [
    ('still_off_center', .14),
    ('centered', .08),
    ('no_progress', .18),
    ('overshot', -.14),
])
async def test_wake_rechecks_aim_after_turning(
        tmp_path, monkeypatch, owner, direction, x, shift, result, remaining):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    cue, safety = cue_source(home, clock, owner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party = PartyCamera()
    state = {}
    send, sent = dispatch_for(owner, state, clock, director, home, cue, safety, party)

    def observe(at, *, offset=0, hear=False, target_x=None):
        clock.at = at
        target = subject(7 if offset == 0 else 9, x - offset if target_x is None else target_x)
        target['identity'] = subject(7, x)['identity']
        state.update(current_frame(at, source, offset=offset, people=[target]))
        if owner == 'primary':
            state['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        wake = cue(CAMERA['id'], state, director.sound_search_cue) if hear else None
        return director.choose(state, at, sound_cue=wake)

    assert observe(1.0) is None
    assert observe(1.4) is None  # 普通跟随不为这个偏移转动。
    assert observe(1.8, hear=True) is None
    assert observe(2.2, hear=True) == direction
    assert not director.public()['target_locked']
    await send(direction)
    assert [(row['action'], row['direction']) for row in sent] == [('motor_step', direction)]
    assert not director.public()['target_locked']

    # 云台收到回执后，仍须通过新画面核对转向及同一人。
    side = 1 if direction == 'right' else -1
    observed_x = .425 + side * remaining
    for at in (4.3, 4.7):
        assert observe(at, offset=shift, hear=True, target_x=observed_x) is None
        assert not director.public()['target_locked']
    needs_correction = result == 'still_off_center'
    assert observe(5.1, offset=shift, hear=True, target_x=observed_x) == (
        direction if needs_correction else None)
    assert director.public()['target_locked'] is not needs_correction
    if needs_correction:
        await send(direction)
        assert not director.public()['target_locked']
        # 第二次转动也要核对。即使人稍向外走，仍只补一次。
        for at in (7.2, 7.6, 8.0):
            assert observe(at, offset=2 * shift, hear=True, target_x=observed_x - side * .01) is None
            assert director.public()['target_locked'] == (at == 8.0)
    assert [row['direction'] for row in sent] == [direction] * (2 if needs_correction else 1)
    assert director.target == 9
    assert not director.public()['physical_motion_verified']


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
@pytest.mark.parametrize('shift', [.012, .05], ids=['small-pan', 'full-pan'])
async def test_wake_can_take_over_a_settled_ordinary_patrol_step(
        tmp_path, monkeypatch, owner, shift):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday', voiced_frames=2)
    # 环视先发出，再交付由合成收音和原语音入口生成的唤醒事件。
    heard, home.wake_camera = home.wake_camera, None
    cue, safety = cue_source(home, clock, owner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party, state = PartyCamera(), {}
    send, sent = dispatch_for(owner, state, clock, director, home, cue, safety, party)

    def observe(at, *, offset=0, people=(), registered_at=None, wake=False, review=False):
        clock.at = at
        state.update(current_frame(at, source, offset=offset, people=people))
        if registered_at is not None:
            state['_patrol_view'] = patrol_view(registered_at, x=offset)
        if owner == 'primary':
            state['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        return director.choose(state, at, review_pending=review,
                               sound_cue=cue(CAMERA['id'], state, director.sound_search_cue) if wake else None)

    assert observe(-17) is None
    assert observe(-16.6) is None
    assert observe(.8) == 'right'
    assert director.phase == 'patrolling'
    await send('right')
    assert director.patrol_confirmation is not None
    home.wake_camera = heard

    caller = subject(7, .24)
    assert observe(2.4, offset=shift, people=[caller], wake=True) is None
    assert observe(3.0, registered_at=.7, people=[caller], wake=True) is None
    assert observe(3.1, offset=shift, people=[caller], wake=True, review=True) is None
    assert director.patrol_confirmation is not None
    # 已有新的稳定画面，先确认同一个人，再用新视角向其转动。
    assert observe(3.4, offset=shift, people=[caller], wake=True) is None
    assert observe(3.8, offset=shift, people=[caller], wake=True) == 'left'
    assert director.pending_decision['sound_cue'] is home.wake_camera
    assert not director.public()['target_locked']
    await send('left')
    assert [(row['action'], row['direction']) for row in sent] == [
        ('motor_step', 'right'), ('motor_step', 'left'),
    ]
    assert not director.public()['physical_motion_verified']


@pytest.mark.asyncio
@pytest.mark.parametrize('busy_action', ['register', 'layout'])
@pytest.mark.parametrize('known_face', [False, True])
async def test_secondary_wake_keeps_the_pre_turn_reference_while_a_frame_is_in_flight(
        tmp_path, monkeypatch, busy_action, known_face):
    import cv2
    import numpy as np

    from homeplane.embodied import camera_recognition

    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    cue, safety = cue_source(home, clock, 'secondary')
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    body = subject(7, .605)
    before = picture([(body, 7)], textured_background=True)
    pixels = cv2.imdecode(np.frombuffer(before, np.uint8), cv2.IMREAD_GRAYSCALE)
    after = cv2.imencode('.jpg', cv2.warpAffine(
        pixels, np.float32([[1, 0, -104], [0, 1, 0]]), (800, 480)))[1].tobytes()
    box = dict(body['box'])
    monkeypatch.setattr(camera_recognition, 'time', clock)
    monkeypatch.setattr(camera_recognition, 'camera_storage', lambda _: tmp_path)
    monkeypatch.setattr(camera_recognition, 'VisionProcess', lambda _: SimpleNamespace(
        detect=AsyncMock(side_effect=lambda *_a, **_kw: dict(boxes=[dict(box)], poses=[])), close=AsyncMock()))

    def identity(_jpeg, people, _faces, *, moving, **_):
        if moving or not known_face:
            return []
        return [dict(body['identity'], track=str(person['id'])) for person in people]

    monkeypatch.setattr(camera_recognition, 'IdentitySession', lambda *_: SimpleNamespace(
        discard=Mock(), process=identity, close=lambda: True))
    recognizer = camera_recognition.CameraRecognition(CAMERA['id'], Mock())
    recognizer.session = SESSION
    room_lock = asyncio.Lock()
    state = {}
    send, sent = dispatch_for('secondary', state, clock, director, home, cue, safety, PartyCamera(),
                              recognizer=recognizer, room_lock=room_lock)

    async def observe(at, jpeg):
        clock.at = at
        return await recognizer.observe(dict(jpeg=base64.b64encode(jpeg).decode(),
            received_at=clock.time(), width=800, height=480, capture_session=source[0],
            camera_source_context=source[1]), room_lock=room_lock)

    async def choose(at, jpeg):
        observation = await observe(at, jpeg)
        state.update(current_frame(at, source, people=observation['people']),
                     _patrol_view=observation['_patrol_view'], scene_visible=observation['scene_visible'],
                     tracking_current=observation['tracking_current'], safety_jpeg=observation['safety_jpeg'],
                     _follow_jpeg=observation['safety_jpeg'])
        return director.choose(state, at, sound_cue=cue(CAMERA['id'], state, director.sound_search_cue)
                               if at >= home.wake_camera.captured else None)

    release = asyncio.Event()
    jobs = []
    try:
        for at in (.8, 1.1, 1.4, 1.8):
            assert await choose(at, before) is None
        assert await choose(2.2, before) == 'right'
        appearance = director.pending_decision.get('appearance')
        if not known_face:
            assert appearance is not None
        room = recognizer.room
        original_process, basis = room.owner.process, room.patrol_view[0]
        entered = asyncio.Event()
        exchange = room._exchange

        async def held_exchange(action, value, frame):
            if action == busy_action and not entered.is_set():
                entered.set()
                await release.wait()
            return await exchange(action, value, frame)

        monkeypatch.setattr(room, '_exchange', held_exchange)
        jobs.append(asyncio.create_task(observe(2.25, before)))
        await asyncio.wait_for(entered.wait(), 1)
        jobs.append(asyncio.create_task(send('right')))
        await asyncio.sleep(0)
        assert not jobs[-1].done()
        release.set()
        await asyncio.gather(*jobs)
        assert [row['direction'] for row in sent] == ['right']
        assert room.ready and room.owner.process is original_process, '转向使后台配准进程丢失原视野'
        assert room.patrol_view is None and not director.public()['target_locked']

        box['x'] -= .13
        phases = []
        for at in (2.7, 3.1, 3.5, 3.9, 4.3, 4.7, 5.1, 5.5, 5.9, 6.3, 6.7):
            assert await choose(at, after) is None
            phases.append((at, director.phase, len(state['people']), director.reframe_candidate))
        assert room.patrol_view[0] == basis
        assert director.public()['target_locked'], phases
        assert director.public()['reason'] == '已唤醒，镜头跟随中'
        assert not director.public()['physical_motion_verified']
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await recognizer.close()


@pytest.mark.asyncio
async def test_wake_adjustment_is_cancelled_if_person_centers_before_dispatch(tmp_path, monkeypatch):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    director = CameraDirector()
    for at in (1.0, 1.4, 1.8, 2.2):
        clock.at = at
        frame = current_frame(at, source, people=[subject(7, .605)])
        wake = home.wake_motion_cue(frame, at) if at >= 1.8 else None
        assert director.choose(frame, at, sound_cue=wake) == ('right' if at == 2.2 else None)
    assert director.decision_current(frame, 2.2, 'right', sound_cue=wake)

    clock.at = 2.4
    centered = current_frame(2.4, source, people=[subject(7, .52)])
    assert not director.decision_current(centered, 2.4, 'right', sound_cue=wake)
    assert director.choose(centered, 2.4, sound_cue=wake) is None
    assert director.public()['target_locked']
    assert director.public()['reason'] == '已唤醒，镜头跟随中'
    assert director.choose(current_frame(2.8, source, people=[subject(7, .605)]),
                           2.8, sound_cue=wake) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner, word', [('primary', 'Friday'), ('secondary', '小福')])
async def test_wake_tries_the_other_side_when_the_first_motor_step_keeps_the_same_view(
        tmp_path, monkeypatch, owner, word):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, word, voiced_frames=2)
    cue, safety = cue_source(home, clock, owner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party = PartyCamera()
    state = {}
    send, sent = dispatch_for(owner, state, clock, director, home, cue, safety, party)

    def observe(at, *, offset=0, registered_at=None, people=()):
        clock.at = at
        state.update(current_frame(at, source, offset=offset, people=people))
        if registered_at is not None:
            state['_patrol_view'] = patrol_view(registered_at, x=offset)
        if owner == 'primary':
            state['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        wake = cue(CAMERA['id'], state, director.sound_search_cue)
        direction = director.choose(state, at, sound_cue=wake)
        assert director.take_sound_view() is None
        return direction

    assert observe(1.0) is None
    assert observe(1.2) == 'right'
    await send('right')
    assert observe(3.4, registered_at=1.1) is None
    assert observe(3.8) is None
    assert observe(4.2, registered_at=3.8) is None
    assert observe(4.6) == 'left'
    assert director.position == (0, 0)
    assert director.public()['patrol_view_state'] == 'unconfirmed'
    await send('left')
    assert [(row['action'], row['direction']) for row in sent] == [
        ('motor_step', 'right'), ('motor_step', 'left'),
    ]

    caller = subject(7, .43)
    assert observe(6.8, offset=-.05, people=[caller]) is None
    assert observe(7.2, offset=-.05, people=[caller]) is None
    assert observe(7.6, offset=-.05, people=[caller]) is None
    assert director.position == (-1, 0)
    assert director.target == caller['id'] and director.public()['target_locked']
    assert not director.public()['physical_motion_verified']


@pytest.mark.parametrize('change', ['source', 'replaced_wake', 'opposite_motion', 'small_motion', 'obscured'])
def test_wake_does_not_reverse_when_the_original_clear_view_cannot_be_verified(change):
    source = ('a' * 32, 'synthetic-source')
    cue = WakeCameraCue(.5, SESSION, source, 7)
    director = CameraDirector()
    assert director.choose(current_frame(.1, source), .1) is None
    assert director.choose(current_frame(.6, source), .6, sound_cue=cue) == 'right'
    director.sent(.6)
    replacement = WakeCameraCue(3, SESSION, source, 7)
    for at in (2.8, 3.2, 3.6, 4.0):
        frame = current_frame(at, source, offset={'opposite_motion': -.05, 'small_motion': .018}.get(change, 0))
        if change == 'obscured':
            frame['scene_visible'] = False
        if change == 'source':
            frame['_wake_source'] = ('b' * 32, source[1])
        assert director.choose(frame, at, sound_cue=replacement if change == 'replaced_wake' else cue) is None
        assert director.public()['patrol_view_state'] == 'checking'


def test_wake_stops_after_two_confirmed_unchanged_views():
    source = ('a' * 32, 'synthetic-source')
    cue = WakeCameraCue(.5, SESSION, source, 7)
    director = CameraDirector()
    assert director.choose(current_frame(.1, source), .1) is None
    for first, direction in ((.6, 'right'), (3.2, 'left')):
        frame = current_frame(first, source)
        assert director.choose(frame, first, sound_cue=cue) == direction
        assert director.decision_current(frame, first, direction, sound_cue=cue)
        director.sent(first)
        assert director.choose(current_frame(first + 2.2, source), first + 2.2, sound_cue=cue) is None
    assert director.choose(current_frame(5.8, source), 5.8, sound_cue=cue) is None
    assert director.patrol_confirmation is None
    assert director.sound_search_cue is None
    assert director.position == (0, 0)


@pytest.mark.asyncio
async def test_wake_target_selection_does_not_guess_between_people(tmp_path, monkeypatch):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    director = CameraDirector()
    assert director.choose(current_frame(1, source), 1) is None
    for at in (1.8, 2.2, 2.6):
        frame = current_frame(at, source, people=[subject(1, .1), subject(2, .78)])
        cue = home.wake_motion_cue(frame, at)
        assert director.choose(frame, at, sound_cue=cue) is None
        assert director.phase == 'target_ambiguous'
    for at in (3.0, 3.4):
        frame = current_frame(at, source, people=[subject(2, .78)])
        direction = director.choose(frame, at, sound_cue=home.wake_motion_cue(frame, at))
    assert direction == 'right'

@pytest.mark.asyncio
async def test_wake_requires_two_clear_observations_of_one_continuous_track(tmp_path, monkeypatch):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    director = CameraDirector()
    assert director.choose(current_frame(1, source), 1) is None
    for at, revision, overlapping in ((1.8, 0, True), (2.2, 1, False), (2.6, 2, False), (3.0, 2, False)):
        target = dict(subject(2, .78), tracking={'follow_revision': revision, 'overlapping': overlapping})
        frame = current_frame(at, source, people=[target])
        cue = home.wake_motion_cue(frame, at)
        direction = director.choose(frame, at, sound_cue=cue)
        assert direction == ('right' if at == 3.0 else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'pause', 'speaking', 'policy', 'fall', 'game', 'hold', 'survey'])
async def test_wake_move_is_rechecked_before_dispatch(tmp_path, monkeypatch, change):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    director = CameraDirector()
    director.choose(current_frame(1, source), 1)
    now = clock.at
    frame = current_frame(now, source)
    cue = home.wake_motion_cue(frame, now)
    assert director.choose(frame, now, sound_cue=cue) == 'right'
    assert director.decision_current(frame, now, 'right', sound_cue=cue)
    kwargs = {}
    if change == 'source':
        frame['_wake_source'] = ('b' * 32, source[1])
    elif change == 'pause':
        home_service.VOICE_INPUT_PAUSED.touch()
    elif change == 'speaking':
        home.speaking = True
    elif change == 'policy':
        home.voice_preferences = lambda: SimpleNamespace(wake_word='小福', revision=8)
    elif change == 'fall':
        kwargs['fall_pending'] = True
    elif change == 'game':
        frame['game'] = {'status': 'playing'}
    else:
        director.apply_mode(change, 'new-intent')
    assert not director.decision_current(frame, now, 'right',
                                         sound_cue=home.wake_motion_cue(frame, now), **kwargs)


@pytest.mark.asyncio
async def test_ordinary_transcript_never_requests_camera_motion(tmp_path, monkeypatch):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday', text='Fridayish')
    assert home.wake_motion_cue(current_frame(clock.at, source), clock.at) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('frames, text', [(1, 'Friday'), (2, 'Friday，打开玄关灯'), (2, '打开玄关灯')])
async def test_short_input_reaches_neither_motor_nor_device_commands_without_exact_wake(
        tmp_path, monkeypatch, frames, text):
    clock = SimpleNamespace(at=0.0)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    device = AsyncMock(return_value=dict(status='reported_match', reached_target=True))
    monkeypatch.setattr(home_service.bridge, 'command', device)
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday', text=text, voiced_frames=frames)
    assert home.wake_motion_cue(current_frame(clock.at, source), clock.at) is None
    assert home.state.get('voice_command') is None
    device.assert_not_awaited()


def test_voice_worker_accepts_only_the_exact_configured_wake_on_short_input(monkeypatch):
    texts = ['', '小福。', '小福，打开玄关灯', 'Friday', '打开玄关灯', '小福']
    transcribe = Mock(side_effect=[(text, {}) for text in texts[:-1]] + [(texts[-1], {'avg_logprob': -1.2})])
    classify = Mock()
    output = []
    request = dict(op='transcribe', wake_only=True, attending=True, voice_revision=7,
                   pcm=base64.b64encode(bytes(16000)).decode())
    monkeypatch.setattr(voice_worker, 'LocalASR', lambda _: SimpleNamespace(label='synthetic', transcribe=transcribe))
    monkeypatch.setattr(voice_worker, 'read_preferences', lambda: SimpleNamespace(wake_word='小福', revision=7))
    monkeypatch.setattr(voice_worker, 'signal', SimpleNamespace(signal=Mock(), SIGTERM=15))
    monkeypatch.setattr(voice_worker, 'sys', SimpleNamespace(
        stdin=[json.dumps(dict(request, id=i)) for i in range(1, len(texts))], stderr=sys.stderr))
    monkeypatch.setattr(voice_worker, 'emit', output.append)
    monkeypatch.setitem(sys.modules, 'homeplane.shared_laya', SimpleNamespace(
        SharedLaya=lambda: SimpleNamespace(status=lambda: {'state': 'synthetic'}, predict=classify)))

    voice_worker.main()

    assert output[0]['type'] == 'ready'
    _, wake, *rejected = output
    assert wake['text'] == '小福。' and wake['address']['reason'] == 'waiting_instruction'
    assert all(row['type'] == 'transcript' and not row['text'] and not row['address']['admitted'] for row in rejected)
    assert rejected[-1]['asr_quality']['filter'] == 'low_asr_confidence'
    classify.assert_not_called()
