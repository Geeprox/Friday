"""Original library bridge and game handler; inert transport, never worker import."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException

from homeplane.embodied import api as api_module
from homeplane.embodied import service as module
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_game_controls import game_handler, handler, request

CAMERA = 'cam_1111111111'


@pytest_asyncio.fixture
async def bridge(monkeypatch, tmp_path):
    """Actual service request/status/start bodies over original game rules."""
    config = dict(viewer_port=8860, viewer_host='127.0.0.1', cameras=[dict(id=CAMERA)])
    monkeypatch.setattr(module, 'PRIVATE', tmp_path)
    monkeypatch.setattr(module, 'configuration', lambda: config)
    monkeypatch.setattr(module, 'camera', lambda *_: config['cameras'][0])
    (tmp_path / 'viewer-key').write_text('synthetic-viewer-key')
    owner = module.EmbodiedService()
    await owner.client.aclose()
    owner.process = SimpleNamespace(returncode=None, pid=99999999)
    owner.selected = CAMERA
    owner.configured = lambda: True
    owner._spawn_guarded_worker = AsyncMock(side_effect=AssertionError('no native worker'))
    owner._stop = AsyncMock(side_effect=AssertionError('game control must not stop perception'))
    game = MotionGames()
    game.reset('ski')
    calls = []
    ports = SimpleNamespace(owner=owner, game=game, calls=calls, before_status=None, before_command=None)

    async def transport(incoming):
        path = incoming.url.path
        data = json.loads(incoming.content) if incoming.content else None
        calls.append((path, data))
        if path == '/api/status':
            snapshot = dict(status='live', camera_id=CAMERA, game=dict(ports.game.public))
            if ports.before_status:
                await ports.before_status()
            return httpx.Response(200, json=snapshot)
        if path == '/api/game':
            if ports.before_command:
                await ports.before_command()
            control, start, enable = game_handler(ports.game)
            try:
                result = await control(request(data))
            except HTTPException as error:
                return httpx.Response(error.status_code, json=dict(detail=error.detail))
            start.assert_not_awaited()
            enable.assert_not_awaited()
            return httpx.Response(200, json=result)
        if path == '/api/start':
            return httpx.Response(200, json=dict(status='running'))
        if path == '/health':
            return httpx.Response(200, json=dict(ready=True))
        raise AssertionError('unexpected worker operation ' + path)

    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    namespace = dict(service=owner, get_app=api_module.get_app, Start=api_module.Start, httpx=httpx)
    if hasattr(api_module, 'GameControl'):
        namespace['GameControl'] = api_module.GameControl
    app = FastAPI()
    app.add_api_route('/api/games', handler(
        'homeplane/embodied/api.py', 'games', service=owner, catalog=api_module.catalog,
        RECOGNIZERS=api_module.RECOGNIZERS, difficulty_options=api_module.difficulty_options,
        ski_trail_options=api_module.ski_trail_options), methods=['GET'])
    app.add_api_route('/api/embodied/start', handler('homeplane/embodied/api.py', 'start', **namespace), methods=['POST'])
    app.add_api_route('/api/embodied/game/stop', handler('homeplane/embodied/api.py', 'stop_game', **namespace), methods=['POST'])
    app.add_api_route('/api/games/{app_id}/open', handler('homeplane/embodied/api.py', 'open_app', service=owner, get_app=api_module.get_app), methods=['GET'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        ports.client = client
        yield ports
    await owner.client.aclose()


async def displayed(bridge):
    report = (await bridge.client.get('/api/games')).json()['service']
    assert isinstance(report.get('game_intent'), str) and len(report['game_intent']) == 64
    return report['game_intent']


@pytest.mark.asyncio
async def test_old_unscoped_stop_cannot_end_a_new_round(bridge):
    bridge.game.reset('tennis')
    before = dict(bridge.game.public)
    response = await bridge.client.post('/api/embodied/game/stop', json={})
    assert response.status_code == 422
    assert bridge.game.public == before
    assert not any(path == '/api/game' for path, _ in bridge.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('app', ['quest', 'home'])
async def test_old_unscoped_start_cannot_replace_a_new_round(bridge, app):
    before = dict(bridge.game.public)
    response = await bridge.client.post('/api/embodied/start', json=dict(camera=CAMERA, app=app))
    assert response.status_code == 422
    assert bridge.game.public == before
    assert not any(path in {'/api/game', '/api/start'} for path, _ in bridge.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['start', 'stop'])
@pytest.mark.parametrize('change', ['new_round', 'pause', 'new_worker'])
async def test_displayed_intent_cannot_be_rebound_to_peer_state(bridge, operation, change):
    intent = await displayed(bridge)
    if change == 'new_round':
        bridge.game.reset('tennis')
    elif change == 'pause':
        bridge.game.set_paused(True)
    else:
        bridge.game = MotionGames()
        bridge.game.reset('quest')
    before = dict(bridge.game.public)
    payload = dict(expected_intent=intent)
    if operation == 'start':
        payload.update(camera=CAMERA, app='quest')
    response = await bridge.client.post('/api/embodied/' + ('start' if operation == 'start' else 'game/stop'), json=payload)
    assert response.status_code == 409 and response.json()['detail'] == 'game_state_changed'
    assert bridge.game.public == before
    assert not any(path in {'/api/game', '/api/start'} for path, _ in bridge.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['start', 'stop'])
async def test_peer_change_after_root_validation_is_rejected_at_worker_without_retry(bridge, operation):
    intent = await displayed(bridge)

    async def peer_change():
        bridge.game.reset('tennis')

    bridge.before_command = peer_change
    payload = dict(expected_intent=intent)
    if operation == 'start':
        payload.update(camera=CAMERA, app='quest')
    response = await bridge.client.post('/api/embodied/' + ('start' if operation == 'start' else 'game/stop'), json=payload)
    assert response.status_code == 409 and response.json()['detail'] == 'game_state_changed'
    assert bridge.game.mode == 'tennis'
    assert sum(path == '/api/game' for path, _ in bridge.calls) == 1
    assert not any(path == '/api/start' for path, _ in bridge.calls)


@pytest.mark.asyncio
async def test_current_stop_preserves_perception_and_duplicate_intent_is_retired(bridge):
    intent = await displayed(bridge)
    first = await bridge.client.post('/api/embodied/game/stop', json=dict(expected_intent=intent))
    assert first.status_code == 200 and first.json()['mode'] == 'off'
    stopped = dict(bridge.game.public)
    second = await bridge.client.post('/api/embodied/game/stop', json=dict(expected_intent=intent))
    assert second.status_code == 409 and bridge.game.public == stopped
    assert bridge.owner.process.returncode is None
    bridge.owner._stop.assert_not_awaited()
    bridge.owner._spawn_guarded_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_start_selects_once_then_starts_original_source(bridge):
    intent = await displayed(bridge)
    result = await bridge.client.post('/api/embodied/start', json=dict(camera=CAMERA, app='quest', expected_intent=intent))
    assert result.status_code == 200
    assert bridge.game.mode == 'off' and bridge.game.selected_mode == 'quest'
    writes = [(path, data) for path, data in bridge.calls if path != '/api/status']
    assert [path for path, _ in writes] == ['/api/game', '/api/start']
    assert writes[0][1]['expected_revision'] == bridge.game.control_revision - 1


@pytest.mark.asyncio
async def test_ordinary_observation_frames_do_not_expire_controls(bridge):
    from tests.embodied.test_motion_games import LOCK, human

    intent = await displayed(bridge)
    for i in range(25):
        bridge.game.update([human()], LOCK, i * .05)
    assert await displayed(bridge) == intent


@pytest.mark.asyncio
async def test_late_status_from_replaced_process_cannot_publish_an_actionable_intent(bridge):
    async def replace_process():
        bridge.owner.process = SimpleNamespace(returncode=None, pid=99999998)

    bridge.before_status = replace_process
    report = await bridge.owner.status()
    assert report.get('game_intent') is None and report.get('game') is None


@pytest.mark.asyncio
async def test_concurrent_starts_share_one_displayed_intent_not_two_resets(bridge):
    intent = await displayed(bridge)
    payload = dict(camera=CAMERA, app='quest', expected_intent=intent)
    replies = await asyncio.gather(*(bridge.client.post('/api/embodied/start', json=payload) for _ in range(2)))
    assert sorted(r.status_code for r in replies) == [200, 409]
    assert sum(path == '/api/game' for path, _ in bridge.calls) == 1


def cold_worker(bridge, monkeypatch, *, claimed=False):
    bridge.owner.process = None
    bridge.owner._recover_pending = AsyncMock()
    bridge.game = MotionGames()
    probe = MagicMock()
    probe.__enter__.return_value = probe
    probe.connect_ex.return_value = 1
    monkeypatch.setattr(module.socket, 'socket', lambda *_args, **_kwargs: probe)

    async def spawn(_environment):
        bridge.owner.process = SimpleNamespace(returncode=None, pid=99999997)
        if claimed:
            bridge.game.reset('tennis')

    bridge.owner._spawn_guarded_worker = AsyncMock(side_effect=spawn)
    return probe


@pytest.mark.asyncio
async def test_cold_start_uses_displayed_idle_scope_and_retires_it(bridge, monkeypatch):
    cold_worker(bridge, monkeypatch)
    intent = await displayed(bridge)
    payload = dict(camera=CAMERA, app='ski', expected_intent=intent)
    result = await bridge.client.post('/api/embodied/start', json=payload)
    assert result.status_code == 200 and result.json()['game']['selected_mode'] == 'ski'
    assert result.json()['game_intent'] != intent
    again = await bridge.client.post('/api/embodied/start', json=payload)
    assert again.status_code == 409
    environment = bridge.owner._spawn_guarded_worker.await_args.args[0]
    assert environment['FRIDAY_START_IDLE'] == '1' and environment['FRIDAY_CAMERA_OPEN'] == '0'
    bridge.owner._spawn_guarded_worker.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_worker_already_claimed_by_a_viewer_is_not_reset_or_started_again(bridge, monkeypatch):
    cold_worker(bridge, monkeypatch, claimed=True)
    intent = await displayed(bridge)
    result = await bridge.client.post('/api/embodied/start', json=dict(camera=CAMERA, app='quest', expected_intent=intent))
    assert result.status_code == 409 and result.json()['detail'] == 'game_state_changed'
    assert bridge.game.mode == 'tennis'
    assert not any(path in {'/api/game', '/api/start'} for path, _ in bridge.calls)
    bridge.owner._stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_background_start_still_works_without_a_browser_scope(bridge, monkeypatch):
    cold_worker(bridge, monkeypatch)
    result = await bridge.owner.start()
    assert result['owned'] is True
    assert [path for path, _ in bridge.calls if path != '/api/status'] == ['/health', '/api/start']


@pytest.mark.asyncio
async def test_stopping_an_idle_service_retires_its_old_scope_even_when_it_returns_idle(bridge):
    bridge.owner.process = None
    bridge.owner.recovery_enabled = True
    bridge.owner._recover_pending = AsyncMock()
    intent = await displayed(bridge)
    await module.EmbodiedService._stop(bridge.owner)
    assert await displayed(bridge) != intent
    result = await bridge.client.post('/api/embodied/start', json=dict(camera=CAMERA, app='quest', expected_intent=intent))
    assert result.status_code == 409
    bridge.owner._spawn_guarded_worker.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, True, 1, '', 'a' * 63, 'z' * 64, [], {}])
@pytest.mark.parametrize('operation', ['start', 'game/stop'])
async def test_invalid_scope_fails_schema_before_worker_io(bridge, bad, operation):
    payload = dict(expected_intent=bad)
    if operation == 'start':
        payload.update(camera=CAMERA, app='ski')
    result = await bridge.client.post('/api/embodied/' + operation, json=payload)
    assert result.status_code == 422 and not bridge.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['paused', 'recovery', 'closed'])
async def test_unavailable_capture_owner_publishes_no_action_scope(bridge, changed):
    if changed == 'paused':
        bridge.owner.capture_enabled = False
    elif changed == 'recovery':
        bridge.owner.recovery_pending = True
    else:
        bridge.owner.closed = True
    result = await bridge.owner.status()
    assert result['game_intent'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field,bad', [('control_context', 'broken'), ('control_revision', True),
                                     ('round_id', True), ('mode', 'unknown'), ('mode', []), ('selected_mode', None)])
async def test_malformed_worker_game_has_no_action_scope(bridge, field, bad):
    bridge.game.public[field] = bad
    report = await bridge.owner.status()
    assert report['game_intent'] is None
