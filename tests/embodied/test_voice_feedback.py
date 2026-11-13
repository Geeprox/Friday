"""验证原语音、Console 与游戏传输使用各自的数据范围。"""

import asyncio
import json
import secrets
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from homeplane.embodied import home_service, service
from homeplane.embodied.api import create_router
from homeplane.embodied.intent import address
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.party_camera import PartyCamera
from tests.embodied.test_camera_audio import actual
from tests.embodied.test_game_controls import handler


WORKER = 'homeplane/embodied/worker.py'
PRIMARY = 'cam_1111111111'
SECONDARY = 'cam_2222222222'
VIEWER = 'SYNTHETIC-viewer'
CONTROL = 'SYNTHETIC-worker-capability'.ljust(40, 'a')
UTTERANCE = 'Friday，打开合成测试灯'
REPLY = '合成房间的温度为 24.5 度。'


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setattr(home_service, 'DATA', tmp_path)
    monkeypatch.setattr(home_service, 'VOICE_INPUT_PAUSED', tmp_path / 'voice-input-paused')
    monkeypatch.setattr(service, 'PRIVATE', tmp_path)
    (tmp_path / 'viewer-key').write_text(VIEWER)
    (tmp_path / 'control-key').write_text(CONTROL)
    config = dict(viewer_port=8740, speaker_hosts=[], tv_viewer_hosts=[], main_origin='http://testserver',
                  cameras=[{'id': PRIMARY}, {'id': SECONDARY}])
    monkeypatch.setattr(service, 'configuration', lambda: config)
    policy = SimpleNamespace(revision=1, wake_word='Friday')
    primary = home_service.HomeService(PRIMARY, voice_preferences=lambda: policy)
    other = home_service.HomeService(SECONDARY, voice_provider=primary, voice_preferences=lambda: policy)
    for home in (primary, other):
        home.curtain.close = AsyncMock()
        home.speaker.say = AsyncMock()
        home.enabled = home.armed = home.selected = True
        home.camera_session = 'synthetic-session'
        home.last_video = time.monotonic()
    scope = dict(state=dict(status='live', camera_id=PRIMARY, processed_frames=1, received_at=time.time()),
                 time=time, asyncio=asyncio, struct=struct, json=json, secrets=secrets, Request=Request,
                 HTTPException=HTTPException,
                 JSONResponse=JSONResponse, StreamingResponse=StreamingResponse, WebSocket=WebSocket,
                 games=MotionGames(), party_camera=PartyCamera(), recorder=SimpleNamespace(public=lambda: {}),
                 safety=SimpleNamespace(recording_status=lambda: {}, review_status=lambda: {}, clear=Mock()),
                 home=primary, secondary_homes={SECONDARY: other}, fleet=SimpleNamespace(public=lambda _: {}, clear=Mock()),
                 PRIVATE=tmp_path, last_frame=time.monotonic(), SELECTED={'id': PRIMARY}, camera_planner=None,
                 configuration=lambda: config, previews=SimpleNamespace(status=lambda _: {'state': 'live'}),
                 interaction_enabled=True, KEY=VIEWER, ALLOWED_IPS={'127.0.0.1', '192.0.2.1'},
                 last_jpeg=b'synthetic-frame', last_preview=b'', last_preview_at=0,
                 changed=asyncio.Condition())
    for name in ('authorized', 'view', 'clear_interactions', 'clear_camera', 'require_console_worker'):
        actual(WORKER, name, scope)
    worker = FastAPI()
    worker.middleware('http')(handler(WORKER, 'privacy', **scope))
    worker.add_api_websocket_route('/ws', handler(WORKER, 'websocket', **scope))
    for path, name in (('/api/status', 'status'), ('/api/frame', 'frame'), ('/api/live', 'live'),
                       ('/api/internal/voice-receipts', 'voice_receipts')):
        worker.add_api_route(path, handler(WORKER, name, **scope), methods=['GET'])
    owner = service.EmbodiedService()
    owner.configured = lambda: True
    owner.selected = PRIMARY
    owner.process = SimpleNamespace(returncode=None)
    owner.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=worker, client=('127.0.0.1', 0)))
    console = FastAPI()
    console.include_router(create_router(owner, SimpleNamespace(), asyncio.Lock()))
    return SimpleNamespace(primary=primary, other=other, owner=owner, worker=worker, console=console,
                           scope=scope, policy=policy, private=tmp_path)


async def admit(home, text=UTTERANCE):
    home.last_video = time.monotonic()
    home.request = AsyncMock(return_value=dict(text=text, voice_revision=1, address=address(text)))
    await home.transcribe(b'synthetic-pcm', None, time.monotonic())


@pytest.mark.asyncio
async def test_only_the_console_receipt_route_returns_admitted_speech(system, monkeypatch):
    device = AsyncMock(return_value=dict(status='reported_match', acknowledged=True, reached_target=True))
    monkeypatch.setattr(home_service.bridge, 'command', device)
    await admit(system.other)
    system.other.state.update(reason=REPLY, last_result={'text': REPLY}, last_address={'body': UTTERANCE})
    system.primary.state.update(last_transcript=UTTERANCE, voice_command={'text': UTTERANCE},
                                reason=REPLY, last_result={'reply': REPLY})
    system.primary.curtain.state.update(status='ready', position=42, last_action={'reply': REPLY})
    try:
        local = httpx.ASGITransport(app=system.console, client=('127.0.0.1', 0))
        async with httpx.AsyncClient(transport=local, base_url='http://testserver') as console:
            for path in ('/api/embodied/status', '/api/games'):
                response = await console.get(path)
                assert response.status_code == 200
                assert all(value not in json.dumps(response.json(), ensure_ascii=False) for value in (UTTERANCE, REPLY))
            response = await console.get('/api/embodied/voice-receipts', headers={'Sec-Fetch-Site': 'same-origin'})
            assert response.headers['cache-control'] == 'no-store'
            receipt, = response.json()['receipts']
            assert receipt['camera'] == SECONDARY and receipt['text'] == UTTERANCE
            assert receipt['receipt'] == dict(status='reported_match', acknowledged=True, reached_target=True)
            assert 0 < receipt['remaining_ms'] <= 60000
            assert (await console.get('/api/embodied/voice-receipts',
                                      headers={'Sec-Fetch-Site': 'cross-site'})).status_code == 403
            device.assert_awaited_once()
        with TestClient(system.worker, client=('127.0.0.1', 0)) as browser:
            params = {'k': VIEWER}
            assert browser.get('/api/internal/voice-receipts', params=params).status_code == 403
            assert browser.get('/api/frame', params=params).status_code == 403
            for path in ('/api/status', '/api/frame'):
                report = browser.get(path, params=params, headers={'X-Friday-Worker': CONTROL}).json()
                assert all(value not in json.dumps(report, ensure_ascii=False) for value in (UTTERANCE, REPLY))
                assert report['home']['curtain'] == dict(position=42, status='ready')
            with browser.websocket_connect('/ws?view=game&k=' + VIEWER) as connection:
                packet = connection.receive_bytes()
                size, _ = struct.unpack('>II', packet[:8])
                report = json.loads(packet[8:8 + size])
                assert all(value not in json.dumps(report, ensure_ascii=False) for value in (UTTERANCE, REPLY))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=system.worker, client=('192.0.2.1', 0)),
                                     base_url='http://fixture') as remote:
            response = await remote.get('/api/internal/voice-receipts', params={'k': VIEWER},
                                        headers={'X-Friday-Worker': CONTROL})
            assert response.status_code == 403
    finally:
        await system.owner.client.aclose()
        await system.other.close()
        await system.primary.close()


@pytest.mark.asyncio
async def test_stage_plays_without_pixels_and_retires_its_own_preview(system):
    system.scope['state'].update(people=[dict(joints={str(i): {} for i in range(8)})],
                                 room=dict(registration=dict(status='locked'), private='synthetic-room-detail'))
    endpoint = next(route.endpoint for route in system.worker.routes if route.path == '/ws')
    endpoint.__globals__.update(last_preview=b'synthetic-player-preview', last_preview_at=time.time())

    def packet(connection):
        value = connection.receive_bytes()
        metadata, size = struct.unpack('>II', value[:8])
        return json.loads(value[8:8 + metadata]), value[-size:] if size else b''

    try:
        with TestClient(system.worker, client=('127.0.0.1', 0)) as browser:
            with browser.websocket_connect('/ws?view=game&k=' + VIEWER) as connection:
                initial, jpeg = packet(connection)
                assert jpeg == b'' and 'people' not in initial and initial['player_visible'] is True
                assert initial['room'] == {'registration': {'status': 'locked'}}
                connection.send_text('show-player-view')
                opened, jpeg = packet(connection)
                assert jpeg == b'synthetic-player-preview' and 'people' in opened
                connection.send_text('hide-player-view')
                closed, jpeg = packet(connection)
                assert jpeg == b'' and 'people' not in closed
        with TestClient(system.worker, client=('192.0.2.1', 0)) as browser:
            with pytest.raises(WebSocketDisconnect), browser.websocket_connect('/ws?k=' + VIEWER):
                pass
    finally:
        await system.owner.client.aclose()
        await system.other.close()
        await system.primary.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['expiry', 'pause', 'policy', 'camera', 'worker'])
async def test_reading_cannot_extend_or_recover_an_old_voice_receipt(system, monkeypatch, change):
    monkeypatch.setattr(home_service.bridge, 'command', AsyncMock(return_value=dict(
        status='accepted', acknowledged=True, reached_target=False)))
    try:
        await admit(system.other)
        original = system.other.voice_receipt()['expires_mono']
        assert (await system.owner.voice_receipts())[0]['state'] == 'accepted'
        assert system.other.voice_receipt()['expires_mono'] == original
        if change == 'expiry':
            monkeypatch.setattr(home_service, 'time', SimpleNamespace(monotonic=lambda: original))
        elif change == 'pause':
            (system.private / 'voice-input-paused').touch()
        elif change == 'policy':
            system.policy.revision += 1
        elif change == 'camera':
            system.scope['clear_camera'](SECONDARY)
        else:
            system.owner.process.returncode = 0
        assert await system.owner.voice_receipts() == []
        if change not in ('pause', 'worker'):
            assert system.other.state['last_transcript'] is None
    finally:
        await system.owner.client.aclose()
        await system.other.close()
        await system.primary.close()


@pytest.mark.asyncio
async def test_late_worker_receipt_is_dropped_if_console_replaces_its_owner(system):
    started, finish = asyncio.Event(), asyncio.Event()

    async def read(*_, **__):
        started.set()
        await finish.wait()
        return {'receipts': [dict(text=UTTERANCE, camera=PRIMARY, expires_mono=time.monotonic() + 30)]}

    system.owner._private_worker_request = read
    pending = asyncio.create_task(system.owner.voice_receipts())
    try:
        await started.wait()
        system.owner.process = SimpleNamespace(returncode=None)
        finish.set()
        assert await pending == []
    finally:
        await system.owner.client.aclose()
