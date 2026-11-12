"""合成收音、人物观测和云台管道贯通唤醒转向。"""

import asyncio
import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeplane.embodied import home_service
from homeplane.embodied.camera_audio import AudioIngress, PCMFramer, WakeCameraCue, deliver_audio
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.intent import address
from homeplane.embodied.party_camera import PartyCamera
from tests.embodied.test_camera_audio import CAMERA, audio, video, voice_service
from tests.embodied.test_camera_director import person, sample
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


async def hear_wake(tmp_path, monkeypatch, clock, word, *, text=None):
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
    measurements = [(True, 1.0, .03)] * 6 + [(False, 0.0, .004)] * 4
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
    h.request.assert_awaited_once()
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


def dispatch_for(owner, state, clock, director, h, cue, safety, party):
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
                     recognizer=None, privacy_closed=Mock())
        extract(PREVIEWS, {'current_sample', 'send_motion'}, scope)

        async def send(direction):
            capture, context = state['_wake_source']
            scope['latest'] = dict(state, session=SESSION, now=state['_local_received'],
                                   capture_session=capture, camera_source_context=context)
            await scope['send_motion'](direction, state['_local_received'], 1)
    return send, sent


@pytest.mark.asyncio
@pytest.mark.parametrize('owner, word', [('primary', 'Friday'), ('secondary', '小福')])
async def test_wake_search_turns_and_follows_on_the_same_capture(tmp_path, monkeypatch, owner, word):
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

    found = subject(7, .76)
    assert observe(3.9, offset=.05, people=[found]) is None
    assert director.phase == 'confirming_view'
    assert observe(4.3, offset=.05, people=[found]) is None
    assert observe(4.7, offset=.05, people=[found]) == 'right'
    await send('right')
    assert [row['direction'] for row in sent] == ['right', 'right']

    centered = subject(9, .43)
    centered['identity'] = found['identity']
    assert observe(7.1, offset=.1, people=[centered]) is None
    assert observe(7.5, offset=.1, people=[centered]) is None
    assert not director.public()['target_locked']
    assert observe(7.9, offset=.1, people=[centered]) is None
    assert director.target == 9 and director.public()['target_locked']
    assert not director.public()['physical_motion_verified']
    assert not home.attention.latest
    assert cue('cam_3333333333', state, None) is None


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
