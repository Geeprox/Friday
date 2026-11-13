"""运行原电视 HTTP 与 WebSocket 处理函数，取流和设备命令使用合成边界。"""

import asyncio
import hmac
import json
import secrets
import struct
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.catalog import MOTION_GAME_MODES
from homeplane.embodied.party_camera import PartyCamera
from homeplane.embodied.static_page import static_asset, static_page
from tests.embodied.test_game_controls import handler
from tests.embodied.test_game_presentation import view
from tests.embodied.test_review_motion import source_function


PATH = 'homeplane/embodied/worker.py'
TV = '192.0.2.10'
OTHER_TV = '192.0.2.11'
WALL = '192.0.2.12'
WALL_KEY = 'SYNTHETIC-wall-key'


def television_app(clock=time.monotonic):
    game = MotionGames(clock=clock, require_presentation=True)
    app = FastAPI()
    state = {'status': 'live', 'processed_frames': 1}
    config = {'tv_viewer_hosts': [TV, OTHER_TV], 'speaker_hosts': [],
              'main_origin': 'http://127.0.0.1', 'cameras': [{'id': 'SYNTHETIC-camera'}]}
    report = Mock(return_value={'version': 1, 'cameras': []})
    fake_wall = Mock(view=report)
    fake_names = Mock()
    fake_names.labels.return_value = {}
    start = AsyncMock()
    namespace = dict(secrets=secrets, time=time, games=game, state=state, KEY=WALL_KEY,
                     party_camera=PartyCamera(), MOTION_GAME_MODES=MOTION_GAME_MODES,
                     ALLOWED_IPS={TV, OTHER_TV, WALL}, configuration=lambda: config,
                     last_frame=time.monotonic(), start=start, home=Mock(enable=AsyncMock()),
                     practice=Mock(active=lambda: False), previews=Mock(), wall=fake_wall,
                     identity_store=fake_names, SELECTED={'id': 'SYNTHETIC-camera'},
                     view=lambda: {'status': 'live'}, changed=asyncio.Condition())
    namespace.update(hmac=hmac, struct=struct, asyncio=asyncio, json=json,
                     last_jpeg=b'SYNTHETIC-private-jpeg', last_preview=b'SYNTHETIC-private-preview', last_preview_at=time.time())
    for name in ('authorized', 'television_host', 'television_key', 'television_access', 'television_packet'):
        namespace[name] = source_function(PATH, name, namespace)
    namespace.update(Request=Request, WebSocket=WebSocket, JSONResponse=JSONResponse, HTMLResponse=HTMLResponse,
                     HERE=Path(PATH).parent, static_page=static_page, static_asset=static_asset,
                     __package__='homeplane.embodied')
    for name in ('game_presentation', 'game_control'):
        namespace[name] = handler(PATH, name, **namespace)
    app.middleware('http')(handler(PATH, 'privacy', **namespace))
    app.add_api_websocket_route('/ws', handler(PATH, 'websocket', **namespace))
    for path, name, method in (
            ('/api/wall', 'camera_wall_status', 'GET'), ('/games', 'game_page', 'GET'),
            ('/assets/{name:path}', 'asset', 'GET'),
            ('/api/game/tv', 'television_game_control', 'POST'),
            ('/api/game/tv/presentation', 'television_presentation', 'POST'),
            ('/api/game', 'local_game_control', 'POST'),
            ('/api/game/presentation', 'local_game_presentation', 'POST')):
        app.add_api_route(path, handler(PATH, name, **namespace), methods=[method])
    return app, game, namespace


def motion_command(game, action, **changes):
    return dict(action=action, mode=game.selected_mode, expected_context=game.control_context,
                expected_revision=game.control_revision, **changes)


@pytest.mark.asyncio
async def test_wall_and_television_credentials_have_different_authority():
    app, game, ports = television_app()
    game.select('ski', ski_trail='pine-slalom', difficulty='easy')
    path = '/api/game/tv'
    transport = httpx.ASGITransport
    async with (httpx.AsyncClient(transport=transport(app, client=(TV, 0)), base_url='http://fixture') as tv,
                httpx.AsyncClient(transport=transport(app, client=(OTHER_TV, 0)), base_url='http://fixture') as other,
                httpx.AsyncClient(transport=transport(app, client=(WALL, 0)), base_url='http://fixture') as wall):
        wall_params = {'k': WALL_KEY}
        wall_report = await tv.get('/api/wall', params=wall_params)
        tv_params = {'k': wall_report.json()['game_display_key']}
        assert tv_params['k'] != WALL_KEY
        assert 'game_display_key' not in (await wall.get('/api/wall', params=wall_params)).json()
        assert (await tv.get('/games', params={'tv': '1', **tv_params})).status_code == 200
        for client, params in ((tv, wall_params), (other, tv_params), (wall, tv_params)):
            assert (await client.post(path, params=params, json=motion_command(game, 'start'))).status_code == 403
        assert (await tv.post('/api/game', params=tv_params, json=motion_command(game, 'start'))).status_code == 403
        assert (await tv.get('/api/wall', params=tv_params)).status_code == 403
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'select'))).status_code == 409
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'start', difficulty='challenge'))).status_code == 409
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'start'))).status_code == 409
        local = view(game)
        assert (await tv.post('/api/game/presentation', params=wall_params, json=local)).status_code == 200
        assert not game.viewer_state()['presentation']['television_ready']
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'start'))).status_code == 409
        assert (await tv.post(path+'/presentation', params=tv_params, json=local)).status_code == 409
        report = view(game, view_id='2'*32)
        assert (await tv.post(path+'/presentation', params=tv_params, json=report)).status_code == 200
        assert game.viewer_state()['presentation']['television_ready']
        before_start = motion_command(game, 'start')
        accepted = await tv.post(path, params=tv_params, json=before_start)
        assert accepted.status_code == 200
        assert accepted.json()['ski_trail'] == 'pine-slalom'
        assert accepted.json()['difficulty'] == 'easy'
        assert not accepted.json()['presentation']['television_ready']
        assert (await tv.post(path, params=tv_params, json=before_start)).status_code == 409
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'pause'))).status_code == 200
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'resume'))).status_code == 409
        assert (await tv.post(path+'/presentation', params=tv_params, json=view(game))).status_code == 200
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'resume'))).status_code == 200
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'start'))).status_code == 409
        game.reset('ski', ski_warmup=True)
        assert (await tv.post(path+'/presentation', params=tv_params, json=view(game))).status_code == 200
        ready = await tv.post(path, params=tv_params, json=motion_command(game, 'start'))
        assert ready.status_code == 200 and ready.json()['ski']['warmup'] is None
        ports['configuration']()['tv_viewer_hosts'].remove(TV)
        assert (await tv.post(path, params=tv_params, json=motion_command(game, 'pause'))).status_code == 403
    ports['start'].assert_not_awaited()
    ports['home'].enable.assert_not_awaited()


def test_television_receives_one_scoped_round_and_zero_camera_bytes():
    app, game, ports = television_app()
    client = TestClient(app, client=(TV, 0))
    key = client.get('/api/wall', params={'k': WALL_KEY}).json()['game_display_key']
    with client.websocket_connect(f'/ws?view=television&k={key}') as stream:
        def receive():
            data = stream.receive_bytes()
            size = int.from_bytes(data[:4], 'big')
            assert len(data) == size+8 and data[4:8] == bytes(4)
            return json.loads(data[8:])

        assert receive() == {'television_idle': True}
        game.reset('tennis')
        game.score[:] = [4, 3]
        game.publish([dict(id='private-player', joints={'SYNTHETIC': 1}, lateral=.2,
                           swings=4, observation='fresh')], 'playing', '合成比赛')
        packet = receive()
        assert set(packet) == {'game', 'status'}
        assert packet['game']['tennis']['score'] == [4, 3]
        assert packet['game']['players'] == [{'lateral': .2, 'swings': 4, 'observation': 'fresh'}]
        assert 'quest' not in packet['game'] and 'owner_track' not in packet['game']
        game.reset('off')
        assert receive() == {'television_idle': True}
    ports['start'].assert_not_awaited()


def test_television_readiness_expires_independently_of_computer_and_other_hosts():
    now = [0.]
    _, game, _ = television_app(clock=lambda: now[0])
    game.select('quest', quest_route='stream')
    game.observe_presentation(view(game, view_id='2'*32), host=TV)
    assert game.presentation.ready(game.presentation_scope(), host=TV)
    assert not game.presentation.ready(game.presentation_scope(), host=OTHER_TV)
    assert game.viewer_state()['presentation']['television_ready']
    now[0] = 2.
    game.observe_presentation(view(game, view_id='3'*32))
    now[0] = 2.6
    result = game.viewer_state()['presentation']
    assert result['ready'] and not result['television_ready']
