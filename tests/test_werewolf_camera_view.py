"""Original host → party → bounded source transport; inert frame bytes only."""

import asyncio
import base64
import copy
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from homeplane.embodied import service as camera_module
from homeplane.werewolf import WerewolfGame
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import Join, WerewolfService, phone_app
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_camera import observation
from tests.test_werewolf_service import SilentLights


def packet():
    # The server projects JPEG framing; actual codec/size agreement is checked
    # by the browser before a picture and its numbers become visible together.
    value = observation()
    value.update(width=640, height=360,
                 frame=base64.b64encode(b'\xff\xd8SYNTHETIC-TRANSPORT\xff\xd9').decode(),
                 voice_sessions=[dict(last_transcript='NEVER-EXPOSE-VOICE')],
                 home=dict(secret='NEVER-EXPOSE-HOME'))
    value['people'][0]['identity'] = dict(person_id='NEVER-EXPOSE-IDENTITY')
    return value


@asynccontextmanager
async def host(directory, monkeypatch):
    monkeypatch.setattr(camera_module, 'PRIVATE', directory)
    monkeypatch.setattr(camera_module, 'configuration', lambda: dict(viewer_port=41973))
    (directory / 'viewer-key').write_text('SYNTHETIC-LOCAL-KEY')
    camera = camera_module.EmbodiedService()
    await camera.client.aclose()
    camera.process = SimpleNamespace(returncode=None)
    camera.selected = 'cam_1234567890'
    state = SimpleNamespace(report=packet(), calls=[], handler=None)

    async def transport(request):
        state.calls.append(request)
        assert request.url.host == '127.0.0.1' and request.url.port == 41973
        assert request.url.params['k'] == 'SYNTHETIC-LOCAL-KEY' and request.method == 'GET'
        if state.handler and request.url.path == '/api/frame':
            return await state.handler(request)
        return httpx.Response(200, json=state.report)

    camera.client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    clock = Clock()
    party = WerewolfService(HostEffects(SilentLights()), embodied=camera, clock=clock,
                            game_factory=partial(WerewolfGame, rng=FixedRoles()))
    await party.create(6, camera=True)
    party.pump.cancel()
    await asyncio.gather(party.pump, return_exceptions=True)
    party.pump = None
    for seat in range(1, 7):
        joined = await party.join(Join(code=party.join_code, seat=seat, name=f'合成{seat}'))
        await party.ready(joined['token'], True)
    app = FastAPI()
    app.include_router(create_router(None, None, camera, directory, owner=party))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='http://host.invalid', headers={'Origin': 'http://host.invalid'}) as client:
            yield party, camera, state, clock, client
    finally:
        await party.close()
        await camera.client.aclose()


async def view(client, party):
    return await client.post('/api/games/werewolf/camera-view', json=dict(session=party.session))


@pytest.mark.asyncio
async def test_same_snapshot_numbers_reach_host_and_bind_through_original_source(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        response = await view(client, party)
        data = response.json()
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert data == dict(available=True, ttl_ms=980, camera='cam_1234567890', session='SYNTHETIC',
                           width=640, height=360, jpeg=state.report['frame'],
                           people=[dict(track=p['id'], box={k: p['box'][k] for k in ('x', 'y', 'w', 'h')})
                                   for p in state.report['people']])
        assert 'NEVER-EXPOSE' not in response.text
        assert [r.url.path for r in state.calls] == ['/api/frame']
        body = dict(seat=1, camera=data['camera'], session=data['session'], track=data['people'][0]['track'],
                    game_session=party.session, revision=party.game.revision, event_id='SYNTHETIC-picture-bind')
        bound = await client.post('/api/games/werewolf/camera-bind', json=body)
        assert bound.status_code == 200
        assert bound.json()['camera_bindings'] == [dict(seat=1, camera=data['camera'], session=data['session'], track=7)]
        assert not party.game.choices and party.game.phase == 'lobby'
        assert not any(k in party.host() for k in ('jpeg', 'frame', 'people'))
        assert not any(k in party.public() for k in ('jpeg', 'frame', 'people'))


@pytest.mark.asyncio
async def test_picture_requires_current_room_exact_origin_opt_in_and_lobby(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        path = '/api/games/werewolf/camera-view'
        assert (await client.post(path, json=dict(session=party.session), headers={'Origin': 'http://elsewhere.invalid'})).status_code == 403
        assert (await client.post(path, json=dict(session='SYNTHETIC-old-room'))).status_code == 409
        party.camera_enabled = False
        assert (await view(client, party)).status_code == 409
        party.camera_enabled = True
        await party.control('start')
        assert (await view(client, party)).status_code == 409
        assert state.calls == []


@pytest.mark.asyncio
async def test_phone_gateway_cannot_deliver_host_picture(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, _):
        app = phone_app(party, 'http://phone.invalid')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://phone.invalid',
                                    headers={'Origin': 'http://phone.invalid'}) as phone:
            response = await view(phone, party)
            assert response.status_code == 404 and 'jpeg' not in response.text
        assert state.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['tracking', 'camera', 'age', 'age_bool', 'width_bool', 'width_huge',
                                  'jpeg', 'jpeg_huge', 'people', 'box', 'registration'])
async def test_invalid_or_stale_photo_never_returns_picture_or_numbers(tmp_path, monkeypatch, fault):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        if fault == 'tracking':
            state.report['tracking_current'] = False
        elif fault == 'camera':
            state.report['camera_id'] = 'cam_0987654321'
        elif fault.startswith('age'):
            state.report['frame_age_ms'] = True if fault == 'age_bool' else 1000
        elif fault.startswith('width'):
            state.report['width'] = True if fault == 'width_bool' else 5000
        elif fault.startswith('jpeg'):
            state.report['frame'] = '*' if fault == 'jpeg' else 'A' * 2_700_001
        elif fault == 'people':
            state.report['people'].append(copy.deepcopy(state.report['people'][0]))
        elif fault == 'box':
            state.report['people'][0]['box']['x'] = -1
        else:
            state.report['room']['registration']['status'] = 'moving'
        response = await view(client, party)
        assert response.status_code == 200 and response.json() == dict(available=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['worker', 'selected', 'capture', 'closed', 'recovery', 'lifecycle', 'room', 'phase'])
async def test_retired_photo_cannot_arrive_from_a_held_original_source_read(tmp_path, monkeypatch, change):
    async with host(tmp_path, monkeypatch) as (party, camera, state, _, client):
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=state.report)
        state.handler = held
        task = asyncio.create_task(view(client, party))
        await asyncio.wait_for(entered.wait(), 1)
        if change == 'worker':
            camera.process = SimpleNamespace(returncode=None)
        elif change == 'selected':
            camera.selected = 'cam_0987654321'
        elif change == 'capture':
            camera.capture_enabled = False
        elif change == 'closed':
            camera.closed = True
        elif change == 'recovery':
            camera.recovery_pending = True
        elif change == 'lifecycle':
            camera._game_lifecycle = 'SYNTHETIC-replacement'
        elif change == 'room':
            await party.close_game()
        else:
            await party.control('start')
        release.set()
        assert (await task).json() == dict(available=False)


@pytest.mark.asyncio
async def test_read_latency_and_overlapping_tabs_cannot_extend_or_queue_picture(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, clock, client):
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(request):
            entered.set()
            await release.wait()
            clock.now += .99
            return httpx.Response(200, json=state.report)
        state.handler = held
        task = asyncio.create_task(view(client, party))
        await entered.wait()
        assert (await view(client, party)).json() == dict(available=False)
        assert len(state.calls) == 1
        release.set()
        assert (await task).json() == dict(available=False)


@pytest.mark.asyncio
async def test_slow_worker_does_not_hold_up_game_control(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        entered = asyncio.Event()
        async def held(request):
            entered.set()
            await asyncio.Event().wait()
        state.handler = held
        task = asyncio.create_task(view(client, party))
        await entered.wait()
        await asyncio.wait_for(party.control('start'), .1)
        assert (await asyncio.wait_for(task, 1.4)).json() == dict(available=False)
        assert not party.camera_view_busy


@pytest.mark.asyncio
async def test_oversized_stream_is_bounded_and_closed(tmp_path, monkeypatch):
    async with host(tmp_path, monkeypatch) as (party, _, state, _, client):
        class Stream(httpx.AsyncByteStream):
            closed = False
            chunks = 0
            async def __aiter__(self):
                for _ in range(10):
                    self.chunks += 1
                    yield b'A' * 1_000_000
            async def aclose(self):
                self.closed = True
        stream = Stream()
        async def large(request):
            return httpx.Response(200, stream=stream)
        state.handler = large
        assert (await view(client, party)).json() == dict(available=False)
        assert stream.closed and stream.chunks == 4
