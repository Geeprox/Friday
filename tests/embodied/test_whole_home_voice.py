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
    SESSION, WORKER, cue_source, current_frame, dispatch_for, hear_wake,
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
    route = address(text)
    result = (dict(type='wake', voice_revision=0, latency_ms=1) if route.get('reason') == 'waiting_instruction'
              else dict(type='transcript', text=text, address=route, voice_revision=0))
    service.request = AsyncMock(return_value=result)
    service.speaker = SimpleNamespace(say=AsyncMock())

    def measure(pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(float) / 32768
        rms = float(np.sqrt(np.mean(samples ** 2)))
        return rms > .003, 1.0 if rms > .003 else 0.0, rms

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
    await asyncio.gather(*services[CAMERA['id']].jobs)
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
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
async def test_wake_motor_waits_for_collected_followup_to_reach_the_actuator(tmp_path, monkeypatch, owner):
    clock = clock_at()
    home, source = await hear_wake(tmp_path, monkeypatch, clock, 'Friday')
    monkeypatch.setattr(home_service, 'VOICE_PAUSED', tmp_path / 'voice-quiet')
    home_service.VOICE_PAUSED.touch()
    home.speaker = SimpleNamespace(say=AsyncMock())
    routing = VoiceRouting(lambda: True, clock=clock.monotonic)
    routing.COLLECT_SECONDS = 0
    home.voice_routing = routing
    entered, release = asyncio.Event(), asyncio.Event()
    selected, start = asyncio.Event(), asyncio.Event()

    async def after_selection(coro):
        try:
            selected.set()
            await start.wait()
            return await coro
        finally:
            coro.close()

    home.spawn = lambda coro: asyncio.create_task(after_selection(coro))

    async def recognize(*args, **kwargs):
        entered.set()
        await release.wait()
        text = 'Friday，打开玄关灯'
        return dict(type='transcript', text=text, address=address(text), voice_revision=7)

    home.request = AsyncMock(side_effect=recognize)
    device = AsyncMock(return_value=dict(status='reported_match', reached_target=True, acknowledged=True))
    monkeypatch.setattr(home_service.bridge, 'command', device)
    cue, safety = cue_source(home, clock, owner)
    director = CameraDirector()
    director.apply_mode('auto', 'original')
    party, state = PartyCamera(), {}

    def choose(at):
        clock.at = home.last_video = at
        state.update(current_frame(at, source))
        if owner == 'primary':
            state['party_camera'] = party.status(camera=CAMERA['id'], session=SESSION, frame_mono=at, now=at)
        return director.choose(state, at, sound_cue=cue(CAMERA['id'], state, director.sound_search_cue))

    director.choose(current_frame(1.0, source), 1.0)
    assert choose(4.0) == 'right'
    send, sent = dispatch_for(owner, state, clock, director, home, cue, safety, party)
    routing.offer(home, replace(candidate(clock.at), source=source, epoch=home.epoch))
    job = routing.task
    try:
        # 语音段已结束，调度尚未进入识别；两条真实发送路径都必须保住这一语句。
        assert not home.speaking and not home.voice_busy
        with pytest.raises(RuntimeError, match='stale_input'):
            await send('right')
        assert not sent and not entered.is_set()
        await asyncio.wait_for(selected.wait(), 1)
        assert not routing.pending and not home.voice_busy
        assert choose(4.2) is None
        start.set()
        await asyncio.wait_for(entered.wait(), 1)
        assert choose(4.4) is None
        assert not sent
    finally:
        start.set()
        release.set()
        await asyncio.wait_for(job, 1)
        await asyncio.gather(*routing.inflight.values())
    device.assert_awaited_once()
    assert device.await_args.args[:2] == ('玄关灯', 'on')
    assert home.state['voice_command']['state'] == 'readback'
    assert choose(4.8) == 'right'
    await send('right')
    assert [row['direction'] for row in sent] == ['right']


@pytest.mark.asyncio
@pytest.mark.parametrize('short_first, detected', [(False, False), (True, False), (True, True)])
async def test_short_cue_only_deduplicates_complete_audio_after_confirmed_wake(short_first, detected):
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    async def decode(*args, decoded, **kwargs):
        decoded(detected)

    short = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                            spawn=asyncio.create_task, transcribe=AsyncMock(side_effect=decode))
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
@pytest.mark.parametrize('revocation', ['other_camera', 'new_source', 'stale_epoch', 'expired', 'discard'])
async def test_pending_voice_only_holds_its_own_current_camera(revocation):
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    service = SimpleNamespace(camera_id=CAMERA['id'], current=Mock(return_value=True),
                              spawn=asyncio.create_task, transcribe=AsyncMock())
    item = candidate(clock.at)
    route.offer(service, item)
    try:
        assert route.waiting(service, item.source, clock.at)
        checked = service
        source = item.source
        if revocation == 'other_camera':
            checked = SimpleNamespace(camera_id=OTHER['id'])
        elif revocation == 'new_source':
            source = ('c' * 32, item.source[1])
        elif revocation == 'stale_epoch':
            service.current.return_value = False
        elif revocation == 'expired':
            clock.at += route.START_LIMIT + .01
        else:
            route.discard(service.camera_id)
        assert not route.waiting(checked, source, clock.at)
    finally:
        route.discard(service.camera_id)
        await route.task
    service.transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_phrases_use_the_shared_transcriber_in_order():
    clock = clock_at(10)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(*_, **kwargs):
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


@pytest.mark.asyncio
@pytest.mark.parametrize('effect', ['device', 'task'])
async def test_another_room_can_turn_while_first_voice_request_waits_for_result(tmp_path, monkeypatch, effect):
    clock = clock_at(10)
    monkeypatch.setattr(home_service, 'time', clock)
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'voice-input-paused')
    monkeypatch.setattr(home_service, 'VOICE_PAUSED', tmp_path / 'voice-quiet')
    home_service.VOICE_PAUSED.touch()
    routing = VoiceRouting(lambda: True, clock=clock.monotonic)
    routing.COLLECT_SECONDS = 0
    command = 'Friday，打开玄关灯' if effect == 'device' else 'Friday，每天晚上十点关掉客厅的灯'
    first = voice_at(OTHER, 'SYNTHETIC-other-view', routing, text=command)
    second = voice_at(CAMERA, SESSION, routing)
    for service in (first, second):
        service.last_video = clock.at
    reached, release = asyncio.Event(), asyncio.Event()

    async def wait_for_result(*args, **kwargs):
        reached.set()
        await release.wait()
        return dict(status='reported_match', reached_target=True, acknowledged=True)

    device = AsyncMock(side_effect=wait_for_result)

    async def task(*args, **kwargs):
        await wait_for_result()
        return SimpleNamespace(valid_until=home_service.datetime.max.replace(tzinfo=home_service.UTC),
                               status='pending_confirmation', proposal_ref=None, reply='合成方案待确认',
                               model_dump=lambda **_: {})

    monkeypatch.setattr(home_service.bridge, 'command', device)
    monkeypatch.setattr(home_service.bridge, 'task', AsyncMock(side_effect=task))
    routing.offer(first, candidate(clock.at))
    try:
        await asyncio.wait_for(reached.wait(), 1)
        assert first.voice_busy
        clock.at += 2
        for service in (first, second):
            service.last_video = clock.at
        source = ('c' * 32, 'd' * 64)
        routing.offer(second, replace(candidate(clock.at), source=source, wake_only=True))

        async def wait_for_wake():
            while second.wake_camera is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_wake(), .25)
        assert first.voice_busy and not release.is_set()
        assert second.wake_camera.source == source
        cue, safety = cue_source(second, clock, 'secondary')
        director = CameraDirector()
        director.apply_mode('auto', 'original')
        director.choose(current_frame(clock.at - 2, source), clock.at - 2)
        frame = current_frame(clock.at, source)
        assert director.choose(frame, clock.at, sound_cue=cue(CAMERA['id'], frame, None)) == 'right'
        send, sent = dispatch_for('secondary', frame, clock, director, second, cue, safety, PartyCamera())
        await send('right')
        assert [row['direction'] for row in sent] == ['right']
        assert first.voice_busy
    finally:
        release.set()
        for running in (routing.task, *first.jobs, *second.jobs):
            if running is not None:
                await asyncio.wait_for(running, 1)
        result = first.state['voice_command']
        for service in (first, second):
            service.clear_voice_receipt()
    assert result['state'] == ('readback' if effect == 'device' else 'awaiting_confirmation')
    assert device.await_count == int(effect == 'device')


@pytest.mark.asyncio
async def test_audible_reply_invalidates_an_unconfirmed_overlapping_wake(tmp_path, monkeypatch):
    clock = clock_at(10)
    monkeypatch.setattr(home_service, 'time', clock)
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'voice-input-paused')
    routing = VoiceRouting(lambda: True, clock=clock.monotonic)
    routing.COLLECT_SECONDS = 0
    home = voice_at(CAMERA, SESSION, routing)
    home.last_video = clock.at
    entered, release = asyncio.Event(), asyncio.Event()

    async def recognize(*args, **kwargs):
        entered.set()
        await release.wait()
        return dict(type='wake', voice_revision=0, latency_ms=1)

    home.request = AsyncMock(side_effect=recognize)
    routing.offer(home, replace(candidate(clock.at), wake_only=True))
    job = routing.task
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with routing.replying(clock.at + 3):
            assert not routing.listening(clock.at)
            release.set()
            await asyncio.wait_for(job, 1)
    finally:
        release.set()
        await asyncio.gather(*home.jobs)
    assert home.wake_camera is None and not routing.recent


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
        assert second.measure(block)[0]
        assert build.call_count == 2
        assert not first.state.any() and (second.state == 1).all()
    finally:
        voice_activity._model_session.cache_clear()
