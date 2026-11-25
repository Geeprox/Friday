"""用合成镜头、语音和 HA 响应验证指向语音的派发边界。"""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from homeplane.embodied import api, home_service, interaction, paths
from homeplane.embodied.camera_audio import AudioIngress, PCMFramer
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.interaction import Binding, Configuration
from homeplane.embodied.interaction_fleet import InteractionFleet
from homeplane.embodied.pointing_voice import PointingFrame, PointingSpeech, pointing_command
from homeplane.embodied.voice_routing import VoiceRouting
from tests.embodied.test_camera_audio import CAMERA
from tests.embodied.test_interaction_fleet import people
from tests.embodied.test_whole_home_voice import candidate, clock_at, command_transcript, voice_at
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import rig


CAMERA_ID = CAMERA['id']
CAMERA_SESSION = 'SYNTHETIC-pointing-session'
SOURCE = ('a' * 32, camera_source_context(CAMERA))
TARGET = 'ha:synthetic:light'
OTHER = 'ha:synthetic:other'
LOCKED = {'status': 'locked'}
READBACK = dict(status='reported_match', reached_target=True, acknowledged=True)


@pytest.mark.parametrize(('body', 'action', 'kind'), [
    ('把这个打开', 'open', None),
    ('请帮我关掉这扇窗帘', 'close', 'cover'),
    ('这盏灯暗一点。', 'dim', 'light'),
    ('这个亮一点', 'brighten', 'light'),
    ('先不要把这个打开', 'no_op', None),
    ('把这个打开，再关另一个', 'clarify', None),
    ('她说「把这个打开」', 'clarify', None),
    ('打开客厅灯', None, None),
])
def test_strict_pointed_grammar(body, action, kind):
    result = pointing_command(body)
    assert ((result.action, result.kind) if result else (None, None)) == (action, kind)


def binding(target=TARGET, *, identifier='a', anchor=(.8, .5)):
    return Binding(id=identifier * 32, camera=CAMERA_ID, scope='camera', gesture='hand.point',
                   target=target, action='on', enabled=True, anchor=list(anchor))


@pytest.fixture
def view(monkeypatch):
    configuration = Configuration(revision=3, enabled=True, bindings=[
        binding(),
        binding(OTHER, identifier='b', anchor=(.8, .7)),
    ])
    monkeypatch.setattr(interaction, 'load', lambda: configuration)
    fleet = InteractionFleet()

    def update(at, pointing=.5, *, actor=7, crowd=False, registration=LOCKED, captured=None,
               session=CAMERA_SESSION, active=True):
        hand = [dict(person_id=actor, gesture='pointing',
                     ray=dict(origin=[.1, pointing], direction=[1, 0]))] if pointing is not None else []
        population = people(track=actor) + (people(track=9) if crowd else [])
        return fleet.update(CAMERA_ID, session, population, hand, registration, active, at,
                            captured_mono=at if captured is None else captured)

    for stamp in (10, 10.8, 11.2, 11.7):
        assert not update(stamp)
    assert fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 11.7).target.target == TARGET
    return fleet, configuration, update


@pytest.mark.parametrize('change', ['point_elsewhere', 'move', 'compensation', 'change_actor', 'disable',
                                     'change_session', 'far_away', 'stale'])
def test_fresh_calibration_and_camera_ownership_invalidate_old_point(view, change):
    fleet, config, update = view
    first = fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 11.7)
    assert fleet.pointing_frame('cam_2222222222', CAMERA_SESSION, 11.7) is None
    if change == 'point_elsewhere':
        update(11.8, .7)
        assert fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 11.8) is None
        update(12.3, .7)
        update(12.65, .7)
        second = fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 12.65)
        assert second.target.target == OTHER and second.target.sequence != first.target.sequence
        return
    now = 12
    if change == 'move':
        update(now, registration={'status': 'moving'})
    elif change == 'compensation':
        update(now, registration={**LOCKED, 'compensated': True})
    elif change == 'change_actor':
        update(now, actor=8)
    elif change == 'disable':
        config.enabled = False
        update(12.8)
        now = 12.8
    elif change == 'change_session':
        update(now, session='other-session')
    elif change == 'far_away':
        update(now, .9)
    else:
        now = 13.3
        update(now, captured=11.7)
    assert fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, now) is None


def test_delayed_audio_reads_historical_frame_and_dispatch_reads_latest(view):
    fleet, _, update = view
    update(11.9, None)
    before = fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 11.8)
    assert before.observed_mono == 11.7
    assert before.target == fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 11.9).target
    update(12.1, .7)
    assert fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 12.1) is None
    assert fleet.pointing_frame(CAMERA_ID, CAMERA_SESSION, 11.8) == before


@pytest.fixture
def speech(tmp_path, monkeypatch, view):
    fleet, _, update = view
    clock = clock_at(11.7)
    monkeypatch.setattr(home_service, 'time', clock)
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'paused')
    quiet = tmp_path / 'quiet'
    quiet.touch()
    monkeypatch.setattr(home_service, 'VOICE_PAUSED', quiet)
    route = VoiceRouting(lambda: True, clock=clock.monotonic)
    route.COLLECT_SECONDS = 0
    home = voice_at(CAMERA, CAMERA_SESSION, route)
    home.last_video = clock.at
    home.pointing_frame = lambda session, now: fleet.pointing_frame(CAMERA_ID, session, now)
    target = home.pointing_frame(CAMERA_SESSION, clock.at).target
    device = AsyncMock(return_value=READBACK)
    monkeypatch.setattr(home_service.bridge, 'command', device)
    return home, route, clock, target, device, update


@pytest.mark.asyncio
async def test_original_capture_and_vad_feed_the_calibrated_target(speech):
    home, route, clock, target, device, update = speech
    home.request.side_effect = lambda *_, **kwargs: command_transcript('Friday，把这个打开', **kwargs)
    framer, ingress = PCMFramer(CAMERA, SOURCE[0]), AudioIngress(CAMERA, SOURCE[0])
    for n in range(10):
        clock.at = stamp = 11.8 + n * .2
        update(stamp, pointing=None)
        home.last_video = stamp
        frame = dict(type='frame', received_at=clock.time(), capture_session=ingress.session,
                     camera_source_context=ingress.context)
        assert ingress.observe_frame(frame, wall=clock.time(), mono=stamp)
        raw = np.full(3200, 1200 if n < 6 else 0, dtype='<i2').tobytes()
        packet, = framer.push(raw, received=clock.time()-.12, decoded=clock.time(), wall=clock.time(), mono=stamp)
        home.offer_audio(ingress.accept(packet, wall=clock.time(), mono=stamp))
        await home.audio_task
    await route.task
    device.assert_awaited_once()
    assert device.await_args.args[1] == 'open'
    assert device.await_args.kwargs == {
        'camera_id': CAMERA_ID, 'target': TARGET,
        'pointing': dict(binding=target.binding, revision=target.revision,
                         observed_mono=clock.at, voice_revision=0, camera_source_context=SOURCE[1], kind=None),
    }
    assert home.state['voice_command']['state'] == 'readback'
    assert home.wake_camera is None


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no_point', 'new_target', 'ptz', 'multiple', 'other_gaze',
                                  'owned_gaze', 'negative', 'compound'])
async def test_asr_must_match_the_original_point_and_operator(speech, case):
    home, route, clock, target, device, update = speech
    point = None if case == 'no_point' else PointingSpeech(target, case != 'multiple')
    phrase = replace(candidate(clock.at), source=SOURCE, view_revision=home.view_revision, pointing=point)
    if case in {'other_gaze', 'owned_gaze'}:
        owner = 8 if case == 'other_gaze' else 7
        home.attention.latest = {owner: True}
        phrase = replace(phrase, owner=owner, attending=True, pointing=replace(point, single_person=False))

    def decode(*_, **kwargs):
        if case == 'ptz':
            home.clear_view_for_motion()
        if case == 'new_target':
            for stamp in (11.8, 12.3, 12.7):
                update(stamp, .7)
                clock.at = home.last_video = stamp
        text = {'negative': 'Friday，不要把这个打开', 'compound': 'Friday，把这个打开，再关另一个',
                'other_gaze': '把这个打开', 'owned_gaze': '把这个打开'}.get(case, 'Friday，把这个打开')
        return command_transcript(text, **kwargs)

    home.request.side_effect = decode
    assert route.offer(home, phrase)
    await route.task
    if case == 'owned_gaze':
        device.assert_awaited_once()
        assert home.state['voice_command']['state'] == 'readback'
    else:
        device.assert_not_awaited()
        assert home.state['voice_command']['state'] == ('ignored' if case == 'negative' else 'clarify')


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['wake', 'gaze', 'different_words', 'two_alternates', 'primary_conflict',
                                  'alternate_lost_view'])
async def test_only_one_independently_qualified_camera_can_recheck_pointed_words(speech, case):
    home, route, clock, primary_target, device, _ = speech
    home.request.side_effect = lambda *_, **kwargs: command_transcript('Friday，把这个打开', **kwargs)
    phrase = replace(candidate(clock.at), source=SOURCE, view_revision=home.view_revision,
                     pointing_conflict=case == 'primary_conflict')
    assert route.offer(home, phrase)
    alternates = []
    for n in range(2 if case == 'two_alternates' else 1):
        camera = dict(id='cam_' + str(n+2) * 10, device={'did': 'synthetic-alternate-' + str(n)})
        alternate = voice_at(camera, 'alternate-view-' + str(n), route, owner=8 if case == 'gaze' else None)
        alternate.last_video = clock.at
        target = replace(primary_target, owner=8, target=OTHER, sequence=2)
        evidence = PointingSpeech(target, case != 'gaze')
        alternate.pointing_frame = lambda *_, pt=target, single=evidence.single_person: PointingFrame(pt, clock.at, single)
        alternate_source = (str(n+2) * 32, camera_source_context(camera))
        item = replace(phrase, owner=alternate.voice_owner, attending=case == 'gaze', source=alternate_source,
                       level=.01, pointing=evidence, pointing_conflict=False)

        def decode(*_, service=alternate, **kwargs):
            if case == 'alternate_lost_view':
                service.clear_view_for_motion()
            utterance = {
                'gaze': '把这个打开。',
                'different_words': 'Friday，把这个关掉',
            }.get(case, 'Friday，把这个打开')
            return command_transcript(utterance, **kwargs)

        alternate.request.side_effect = decode
        assert route.offer(alternate, item)
        alternates.append(alternate)
    await route.task
    if case in {'wake', 'gaze'}:
        device.assert_awaited_once()
        assert device.await_args.kwargs['target'] == OTHER
        assert device.await_args.kwargs['camera_id'] == alternates[0].camera_id
    else:
        device.assert_not_awaited()
    if case in {'two_alternates', 'primary_conflict'}:
        for alternate in alternates:
            alternate.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['reported_match', 'accepted', 'unknown_outcome'])
async def test_only_confirmed_device_state_allows_pointed_followup(speech, status):
    home, route, clock, target, device, update = speech
    device.return_value = dict(status=status, reached_target=status == 'reported_match',
                               acknowledged=status in {'reported_match', 'accepted'})
    home.request.side_effect = lambda *_, **kwargs: command_transcript('Friday，把这个打开', **kwargs)
    phrase = replace(candidate(clock.at), source=SOURCE, view_revision=home.view_revision,
                     pointing=PointingSpeech(target, True))
    assert route.offer(home, phrase)
    await route.task
    for stamp in (12.2, 12.8, 13.4, 14, 14.6, 15.2):
        update(stamp, pointing=None)
    clock.at = home.last_video = 15.2
    evidence = home._pointing_at_audio_ingress(clock.at, SOURCE)
    assert home._pointing_at_audio_ingress(clock.at, ('b' * 32, SOURCE[1])) is None
    if status == 'reported_match':
        assert evidence is not None and evidence.reply is home.pointing_reply
    else:
        assert evidence is None
    home.request.side_effect = lambda *_, **kwargs: command_transcript('Friday，这个暗一点', **kwargs)
    assert route.offer(home, replace(phrase, pcm=b'SYNTHETIC followup', captured=clock.at,
                                     started=clock.at-.4, ended=clock.at-.2, pointing=evidence))
    await route.task
    assert [entry.args[1] for entry in device.await_args_list] == (
        ['open', 'dim'] if status == 'reported_match' else ['open'])


@pytest.mark.asyncio
async def test_view_invalidation_cancels_outbound_request_and_does_not_retry(speech):
    home, route, clock, target, device, _ = speech
    started = asyncio.Event()

    async def waiting(*_, **__):
        started.set()
        await asyncio.Future()

    device.side_effect = waiting
    home.request.side_effect = lambda *_, **kwargs: command_transcript('Friday，把这个打开', **kwargs)
    assert route.offer(home, replace(candidate(clock.at), source=SOURCE, view_revision=home.view_revision,
                                    pointing=PointingSpeech(target, True)))
    await asyncio.wait_for(started.wait(), 2)
    home.clear_view_for_motion()
    await route.task
    device.assert_awaited_once()
    assert home.state['voice_command']['state'] == 'unknown_outcome'
    assert home.pointing_reply is None


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['open', 'dim', 'brighten', 'limit', 'off', 'unknown_level', 'device_changed',
                                  'camera_changed', 'paused', 'revised', 'without_evidence', 'named_cover'])
async def test_command_checks_pointing_and_device_state_at_actual_send(tmp_path, monkeypatch, case):
    core, rows, requests, *_ = rig(tmp_path)
    try:
        row = next(row for row in rows if row['entity_id'] == 'light.simulated')
        row['state'] = 'off' if case == 'off' else 'on'
        row['attributes']['brightness'] = 255 if case == 'limit' else 100
        if case == 'unknown_level':
            del row['attributes']['brightness']
        target = next(e.id for e in (await core.snapshot()).entities if e.kind == 'light')
        config = Configuration(revision=3, enabled=True, bindings=[binding(target)])
        monkeypatch.setattr(interaction, 'load', lambda: config)
        monkeypatch.setattr(api, 'PRIVATE', tmp_path)
        monkeypatch.setattr(paths, 'camera', lambda _: CAMERA)
        key = 'f' * 48
        (tmp_path / 'control-key').write_text(key)
        service = SimpleNamespace(process=SimpleNamespace(returncode=None),
                                  capture_enabled=True, interaction_enabled=True)
        if case == 'paused':
            (tmp_path / 'voice-input-paused').touch()
        if case == 'device_changed':
            get = core.adapter._get

            async def changed(path):
                result = await get(path)
                if path == '/api/states/light.simulated':
                    return dict(result, attributes=dict(result['attributes'], brightness=180))
                return result

            monkeypatch.setattr(core.adapter, '_get', changed)
        command = {
            'camera': CAMERA_ID, 'target': target, 'event_id': 'SYNTHETIC-relative',
            'action': 'open' if case == 'open' else 'brighten' if case in {'brighten', 'limit'} else 'dim',
            'pointing': dict(binding=config.bindings[0].id, revision=2 if case == 'revised' else 3,
                             observed_mono=time.monotonic(), voice_revision=0,
                             camera_source_context=('b' * 64 if case == 'camera_changed' else SOURCE[1]),
                             kind='cover' if case == 'named_cover' else 'light'),
        }
        if case == 'without_evidence':
            command.pop('pointing')
        web = FastAPI()
        web.include_router(api.create_router(service, core, asyncio.Lock()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url='http://testserver',
                                     headers={'X-Friday-Worker': key}) as client:
            response = await client.post('/api/embodied/command', json=command)
            rejected = case in {'camera_changed', 'paused', 'revised'}
            assert response.status_code == (422 if case == 'without_evidence' else 409 if rejected else 200)
            if response.status_code == 200:
                receipt = response.json()
                assert receipt['reached_target'] is (case in {'open', 'dim', 'brighten', 'limit'})
                assert receipt['write_attempted'] is (case in {'open', 'dim', 'brighten'})
                if case == 'device_changed':
                    assert receipt['outcomes'][0]['code'] == 'brightness_changed'
            if case in {'open', 'dim', 'brighten'}:
                repeated = await client.post('/api/embodied/command', json=command)
                assert repeated.json()['status'] == ('reported_match' if case == 'open' else 'unavailable')
                assert len(requests) == 1
                expected = {'entity_id': 'light.simulated'}
                if case != 'open':
                    expected['brightness'] = 74 if case == 'dim' else 126
                assert json.loads(requests[0].content) == expected
            else:
                assert not requests
    finally:
        await close(core)
