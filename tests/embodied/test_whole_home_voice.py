"""各路摄像头收音到实际云台发送的合成回归。"""

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from homeplane.embodied import home_service
from homeplane.embodied import voice_activity
from homeplane.embodied.camera_audio import AudioIngress, PCMFramer, deliver_audio
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.intent import address
from homeplane.embodied.interaction_fleet import InteractionFleet
from homeplane.embodied.party_camera import PartyCamera
from homeplane.embodied.voice_routing import Utterance, VoiceRouting
from tests.embodied.test_camera_audio import CAMERA, actual, voice_service
from tests.embodied.test_wake_camera_motion import (
    SESSION, WORKER, cue_source, current_frame, dispatch_for,
)


OTHER = dict(id='cam_2222222222', device={'did': 'synthetic-other-camera'})


def clock_at(at=0.0):
    clock = SimpleNamespace(at=at)
    clock.monotonic, clock.time = lambda: clock.at, lambda: 1000 + clock.at
    return clock


def voice_at(camera, session, routing, *, owner=None, text='Friday'):
    service = voice_service(home_service)
    service.camera_id, service.camera_session = camera['id'], session
    service.voice_routing = routing
    service.voice_owner = owner
    if owner is not None:
        service.attention.latest = {owner: True}
    service.voice_preferences = lambda: SimpleNamespace(wake_word='Friday', revision=0)
    service.runtime_log = SimpleNamespace(record=Mock())
    service.transcribe = home_service.HomeService.transcribe.__get__(service)
    service.spawn = home_service.HomeService.spawn.__get__(service)
    service.request = AsyncMock(return_value=dict(text=text, address=address(text)))
    service.speaker = SimpleNamespace(say=AsyncMock())

    def measure(pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
        rms = float(np.sqrt(np.mean(samples ** 2)))
        return voice_activity.SpeechActivity(rms > .003, 1.0 if rms > .003 else 0.0, rms, 0.0)

    service.activity = SimpleNamespace(probability=.9, measure=measure)
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize('winner', ['primary', 'secondary'])
@pytest.mark.parametrize('text, voiced_frames', [('Friday', 2), ('Friday', 6), ('Friday，打开玄关灯', 6)])
async def test_offscreen_caller_gets_the_original_motor_even_when_other_room_is_occupied(
        tmp_path, monkeypatch, winner, text, voiced_frames):
    clock = clock_at()
    monkeypatch.setattr(home_service, 'time', clock)
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'voice-paused')
    monkeypatch.setattr(home_service, 'VOICE_PAUSED', tmp_path / 'voice-quiet')
    device = AsyncMock(return_value=dict(status='reported_match', reached_target=True, acknowledged=True))
    monkeypatch.setattr(home_service.bridge, 'command', device)
    routing = VoiceRouting(lambda: True, clock=clock.monotonic)
    routing.COLLECT_SECONDS = .01
    fleet = InteractionFleet(voice_revoked=routing.discard)
    primary, secondary = (CAMERA, OTHER) if winner == 'primary' else (OTHER, CAMERA)
    services = {
        CAMERA['id']: voice_at(CAMERA, SESSION, routing, text=text),
        OTHER['id']: voice_at(OTHER, 'SYNTHETIC-other-view', routing, owner=7, text=text),
    }
    receivers, framer, ingress = {}, {}, {}
    for camera, generation in ((CAMERA, 'a' * 32), (OTHER, 'b' * 32)):
        cid = camera['id']
        fleet.session(cid, services[cid].camera_session)
        framer[cid] = PCMFramer(camera, generation)
        ingress[cid] = AudioIngress(camera, generation)
        # 使用两个原收音入口和同一收音调度器。
        scope = dict(time=clock, deliver_audio=deliver_audio, fleet=fleet, home=services[cid],
                     safety=SimpleNamespace(observe_audio=Mock()), SELECTED=camera,
                     secondary_homes={cid: services[cid]}, asyncio=asyncio, json=json, capture=None,
                     generation=1, my_generation=1, state={'status': 'live'},
                     primary_capture_closed=AsyncMock(), audio_ingress=ingress[cid],
                     wall_epoch=1, primary_wall_epoch=1, queue=Mock(), offer_frame=Mock())
        if camera == primary:
            receive = actual(WORKER, 'receive', scope)

            async def on_packet(packet, admission, *, port=scope, loop=receive):
                port['capture'] = SimpleNamespace(stdout=SimpleNamespace(
                    readline=AsyncMock(side_effect=[json.dumps(packet).encode(), b''])))
                await loop()
                assert port['primary_capture_closed'].call_args.args[-2:] == ('source_closed', None)
        else:
            assert camera == secondary
            receive = actual(WORKER, 'secondary_audio', scope)

            async def on_packet(packet, admission, *, camera_id=cid, loop=receive):
                await loop(camera_id, admission())
        receivers[cid] = on_packet

    for index in range(voiced_frames + 4):
        for camera in (primary, secondary):
            cid = camera['id']
            home, gate = services[cid], ingress[cid]
            clock.at = index * .2 + (0.02 if camera == secondary else 0)
            home.last_video = clock.at
            home.audio_allowed = fleet.voice(cid, clock.at, enabled=True, session=home.camera_session)
            assert home.audio_allowed
            frame = dict(type='frame', received_at=clock.time(),
                         capture_session=gate.session, camera_source_context=gate.context)
            assert gate.observe_frame(frame, wall=clock.time(), mono=clock.at)
            amplitude = (1200 if camera == CAMERA else 400) if index < voiced_frames else 0
            raw = np.full(3200, amplitude, dtype='<i2').tobytes()
            packet, = framer[cid].push(raw, received=clock.time(), decoded=clock.time(),
                                       wall=clock.time(), mono=clock.at)
            await receivers[cid](packet, lambda: gate.accept(packet, wall=clock.time(), mono=clock.at))

    assert routing.task is not None
    await routing.task
    home = services[CAMERA['id']]
    home.request.assert_awaited_once()
    assert home.request.await_args.kwargs['wake_only'] == (voiced_frames == 2)
    services[OTHER['id']].request.assert_not_awaited()
    assert services[OTHER['id']].wake_camera is None
    source = ingress[CAMERA['id']].session, ingress[CAMERA['id']].context
    assert home.wake_camera.source == source and not home.attention.latest
    if '灯' in text:
        device.assert_awaited_once()
        assert device.await_args.kwargs['camera_id'] == CAMERA['id']
        assert home.state['voice_command']['state'] == 'readback'
        assert not routing.listening(clock.at)
    else:
        device.assert_not_awaited()

    cue, safety = cue_source(home, clock, winner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party = PartyCamera()
    frame = {}

    def choose(at):
        clock.at = at
        frame.update(current_frame(at, source))
        if winner == 'primary':
            frame['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        return director.choose(frame, at, sound_cue=cue(CAMERA['id'], frame, director.sound_search_cue))

    director.choose(current_frame(1.0, source), 1.0)
    assert choose(clock.at + .1) == 'right'
    send, sent = dispatch_for(winner, frame, clock, director, home, cue, safety, party)
    await send('right')
    assert [(row['action'], row['direction']) for row in sent] == [('motor_step', 'right')]


def candidate(at):
    return Utterance(b'SYNTHETIC', None, at, False, 1, ('a'*32, 'b'*64), at-1.3, at-.7, .04)


@pytest.mark.asyncio
@pytest.mark.parametrize('short_first, detected', [(False, False), (True, False), (True, True)])
async def test_short_cue_only_deduplicates_complete_audio_after_confirmed_wake(short_first, detected):
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    short = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                            spawn=asyncio.create_task, transcribe=AsyncMock(return_value=detected))
    full = SimpleNamespace(camera_id=OTHER['id'], current=Mock(return_value=True),
                           spawn=asyncio.create_task, transcribe=AsyncMock())
    phrase = candidate(clock.at)
    route.offer(short, replace(phrase, wake_only=True, level=.08))
    if short_first:
        await route.task
        clock.at += .5
    route.offer(full, replace(phrase, captured=clock.at))
    if route.task:
        await route.task
    assert short.transcribe.await_count == int(short_first)
    assert full.transcribe.await_count == int(not detected)


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['same_source_gap', 'stale_video', 'disabled'])
async def test_brief_wake_closes_at_the_audio_gap_only_with_current_authority(tmp_path, monkeypatch, problem):
    clock = clock_at()
    monkeypatch.setattr(home_service, 'time', clock)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'paused')
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    service = voice_at(CAMERA, SESSION, route)
    gate = AudioIngress(CAMERA, 'a'*32)
    framer = PCMFramer(CAMERA, 'a'*32)
    speech = np.full(3200, 360, dtype='<i2').tobytes()
    quiet = bytes(6400)
    service.activity.measure = Mock(side_effect=[
        voice_activity.SpeechActivity(False, .25, .001, .128),
        voice_activity.SpeechActivity(False, 0, 0, 0),
    ])

    async def deliver(at, pcm, *, fresh=True, enabled=True):
        clock.at = at
        if fresh:
            service.last_video = at
            assert gate.observe_frame(dict(type='frame', received_at=clock.time(),
                capture_session=gate.session, camera_source_context=gate.context),
                wall=clock.time(), mono=at)
        service.audio_allowed = enabled
        packet, = framer.push(pcm, received=clock.time(), decoded=clock.time(), wall=clock.time(), mono=at)
        await deliver_audio(service, gate.accept(packet, wall=clock.time(), mono=at))

    await deliver(1, speech)
    assert service.speaking and route.task is None
    await deliver(3 if problem == 'stale_video' else 1.7, quiet,
                  fresh=problem != 'stale_video', enabled=problem != 'disabled')
    if route.task:
        await route.task
    if problem == 'same_source_gap':
        assert service.request.await_args.kwargs['wake_only'] is True
        assert service.wake_camera is not None
        assert len(service.request.await_args.kwargs['pcm']) == (6400 + 2) // 3 * 4
    else:
        service.request.assert_not_awaited()
        assert service.wake_camera is None


@pytest.mark.asyncio
async def test_short_fragment_does_not_replace_a_pending_full_utterance_from_the_same_camera():
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    service = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                              spawn=asyncio.create_task, transcribe=AsyncMock())
    full = candidate(clock.at)
    route.offer(service, full)
    clock.at += 1.25
    route.offer(service, replace(candidate(clock.at), wake_only=True, pcm=b'SYNTHETIC-short'))
    await route.task
    service.transcribe.assert_awaited_once()
    assert service.transcribe.await_args.args[0] == full.pcm


@pytest.mark.asyncio
async def test_late_echo_of_the_same_phrase_cannot_reach_asr_twice():
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    first = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                            spawn=asyncio.create_task, transcribe=AsyncMock())
    second = SimpleNamespace(camera_id=OTHER['id'], current=Mock(return_value=True),
                             spawn=asyncio.create_task, transcribe=AsyncMock())
    phrase = candidate(clock.at)
    route.offer(first, phrase)
    await route.task
    clock.at += .8
    route.offer(second, replace(phrase, captured=clock.at, level=.08))
    assert route.task is None
    first.transcribe.assert_awaited_once()
    second.transcribe.assert_not_awaited()
    clock.at += 1.2
    route.offer(second, candidate(clock.at))
    await route.task
    second.transcribe.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('revocation', ['source', 'input', 'reply'])
async def test_collected_voice_is_revoked_before_the_shared_transcriber(revocation):
    clock = clock_at(10)
    enabled = True
    route = VoiceRouting(lambda: enabled, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    service = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                              spawn=asyncio.create_task, transcribe=AsyncMock())
    route.offer(service, candidate(clock.at))
    if revocation == 'source':
        service.current.return_value = False
    elif revocation == 'input':
        enabled = False
    else:
        with route.replying(clock.at + 2):
            assert not route.listening(clock.at + 3)
    await route.task
    assert not route.pending
    service.transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_phrases_use_the_shared_transcriber_in_order():
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(*_):
        entered.set()
        await release.wait()

    first = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                            spawn=asyncio.create_task, transcribe=AsyncMock(side_effect=hold))
    second = SimpleNamespace(camera_id=OTHER['id'], current=Mock(return_value=True),
                             spawn=asyncio.create_task, transcribe=AsyncMock())
    route.offer(first, candidate(clock.at))
    async with asyncio.timeout(1):
        await entered.wait()
        clock.at += 2
        route.offer(second, candidate(clock.at))
        await asyncio.sleep(0)
        second.transcribe.assert_not_awaited()
        release.set()
        await route.task
    second.transcribe.assert_awaited_once()


def test_cameras_share_model_weights_with_independent_voice_state_and_reload_after_failure(monkeypatch):
    initial = SimpleNamespace(run=Mock(side_effect=RuntimeError('SYNTHETIC-inference-fault')))
    recovered = SimpleNamespace(run=Mock(side_effect=lambda _, data: (
        np.array([[.9]], dtype=np.float32), data['state'] + 1)))
    build = Mock(side_effect=[initial, recovered])
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(
        SessionOptions=SimpleNamespace, disable_telemetry_events=Mock(), InferenceSession=build))
    voice_activity._model_session.cache_clear()
    try:
        first, second = voice_activity.VoiceActivity(), voice_activity.VoiceActivity()
        assert build.call_count == 1
        block = np.full(512, 1800, dtype='<i2').tobytes()
        with pytest.raises(RuntimeError, match='SYNTHETIC-inference-fault'):
            first.measure(block)
        assert second.measure(block).speech
        assert build.call_count == 2
        assert not first.state.any() and (second.state == 1).all()
    finally:
        voice_activity._model_session.cache_clear()
