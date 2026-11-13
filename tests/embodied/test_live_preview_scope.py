"""用原 Console 和识别路由验证查看权限，画面与设备均为合成。"""

import asyncio
import base64
import json
import secrets
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from homeplane.embodied import service as module
from homeplane.embodied.api import create_router
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.live_preview import VIEW_IDLE_SECONDS
from tests.embodied.test_review_motion import source_function

PRIMARY = 'cam_1111111111'
SECONDARY = 'cam_2222222222'
JPEG = b'\xff\xd8SYNTHETIC-LIVE\xff\xd9'
VIEWER = 'SYNTHETIC-TEST-VIEWER'
CONTROL = 'SYNTHETIC-TEST-CONTROL-KEY-0000000000'
WORKER = 'homeplane/embodied/worker.py'


@asynccontextmanager
async def environment(tmp_path, monkeypatch):
    cameras = {cid: dict(id=cid, device=dict(did='SYNTHETIC-' + cid))
               for cid in (PRIMARY, SECONDARY)}
    configured = cameras.copy()
    settings = lambda: dict(viewer_port=31973, speaker_hosts=[], main_origin='http://console',
                            cameras=list(configured.values()))
    monkeypatch.setattr(module, 'PRIVATE', tmp_path)
    monkeypatch.setattr(module, 'configuration', settings)
    monkeypatch.setattr(module, 'camera', configured.__getitem__)
    (tmp_path / 'viewer-key').write_text(VIEWER)
    (tmp_path / 'control-key').write_text(CONTROL)
    local = dict(state='live')
    previews = SimpleNamespace(sessions={SECONDARY: {'selected': cameras[SECONDARY]}},
                               start=AsyncMock(return_value=dict(state='live')),
                               status=lambda _: dict(state=local['state']),
                               frame=lambda _: JPEG)
    scope = dict(SELECTED=cameras[PRIMARY], previews=previews, HTTPException=HTTPException,
                 Request=Request, JSONResponse=JSONResponse, Response=Response,
                 PRIVATE=tmp_path, secrets=secrets, time=time, KEY=VIEWER,
                 wall=SimpleNamespace(epochs={PRIMARY: 1, SECONDARY: 2}),
                 ALLOWED_IPS={'127.0.0.1', '192.0.2.1'}, configuration=settings,
                 camera=configured.__getitem__, camera_source_context=camera_source_context,
                 view=lambda: dict(status=local['state'], camera_sessions=[{'recognition': 'running'}]),
                 primary_recovery_frame=lambda: None, state={'processed_frames': 7},
                 camera_planner=object(), last_preview=JPEG, last_preview_at=time.time())
    for name in ('authorized', 'require_console_worker', 'require_preview_source', 'preview_selection'):
        source_function(WORKER, name, scope)
    worker = FastAPI()
    worker.middleware('http')(source_function(WORKER, 'privacy', scope))
    for path, name, method in (
        ('/api/previews/{camera_id}', 'start_preview', 'POST'),
        ('/api/previews/{camera_id}', 'preview_status', 'GET'),
        ('/api/previews/{camera_id}/frame', 'preview_frame', 'GET'),
        ('/api/frame', 'frame', 'GET'),
    ):
        worker.add_api_route(path, source_function(WORKER, name, scope), methods=[method])
    owner = module.EmbodiedService()
    await owner.client.aclose()
    owner.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=worker))
    owner.configured = lambda: True
    owner.process = SimpleNamespace(returncode=None)
    owner.selected = PRIMARY
    owner.stop = AsyncMock()
    clock = [30.0]
    owner.live_previews.clock = lambda: clock[0]
    console = FastAPI()
    console.include_router(create_router(owner, None, None))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=console),
                                     base_url='http://console') as browser:
            yield SimpleNamespace(owner=owner, worker=worker, browser=browser, scope=scope,
                                  clock=clock, local=local, configured=configured)
    finally:
        await owner.close()


def path(camera=PRIMARY):
    return '/api/embodied/cameras/' + camera


async def open_view(browser, camera=PRIMARY, *, purpose='live'):
    result = await browser.post(path(camera) + '/preview', json={'purpose': purpose})
    assert result.status_code == 200
    assert result.headers['cache-control'] == 'no-store'
    return result.json()['preview_token']


def view_headers(token):
    return {'X-Friday-Live-View': token}


@pytest.mark.asyncio
@pytest.mark.parametrize('camera', [PRIMARY, SECONDARY])
async def test_only_explicit_local_view_opens_its_own_jpeg(tmp_path, monkeypatch, camera):
    async with environment(tmp_path, monkeypatch) as app:
        console = app.browser
        metadata = await console.get(path(camera) + '/preview')
        assert metadata.json()['state'] == 'live' and 'preview_token' not in metadata.json()
        assert (await console.get(path(camera) + '/frame')).status_code == 409
        assert (await console.post(path(camera) + '/preview', json={},
                                  headers={'Sec-Fetch-Site': 'same-site'})).status_code == 403
        token = await open_view(console, camera)
        first = await console.get(path(camera) + '/frame', params={'view': token})
        assert first.content == JPEG and first.headers['cache-control'] == 'no-store'
        assert first.headers['cross-origin-resource-policy'] == 'same-origin'
        assert (await console.get(path(camera) + '/frame', params={'view': token},
                                  headers={'Sec-Fetch-Site': 'cross-site'})).status_code == 403
        other = SECONDARY if camera == PRIMARY else PRIMARY
        assert (await console.get(path(other) + '/frame', params={'view': token})).status_code == 409
        worker_path = '/api/previews/' + camera + '/frame'
        assert (await app.owner.client.get(app.owner._url(worker_path))).status_code == 403
        assert (await app.owner.client.get(app.owner._url('/api/frame'))).status_code == 403
        await console.post(path(camera) + '/preview/stop', json={'token': token})
        assert (await console.get(path(camera) + '/frame', params={'view': token})).status_code == 409
        assert (await console.get(path(camera) + '/preview')).json()['state'] == 'live'
        app.owner.stop.assert_not_called()


@pytest.mark.asyncio
async def test_only_current_status_renews_and_views_end_independently(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as app:
        console = app.browser
        live = await open_view(console)
        pointing = await open_view(console, purpose='pointing')
        assert live != pointing
        assert (await console.get(path() + '/follow-target', headers=view_headers(pointing))).status_code == 409
        app.clock[0] += VIEW_IDLE_SECONDS - 1
        assert (await console.get(path() + '/frame', params={'view': live})).content == JPEG
        assert (await console.get(path() + '/preview', headers=view_headers(pointing))).status_code == 200
        app.clock[0] += 2
        assert (await console.get(path() + '/frame', params={'view': live})).status_code == 409
        assert (await console.get(path() + '/frame', params={'view': pointing})).content == JPEG
        await console.post(path() + '/preview/stop', json={'token': live})
        assert (await console.get(path() + '/frame', params={'view': pointing})).content == JPEG
        app.local['state'] = 'shutter_closed'
        assert (await console.get(path() + '/preview', headers=view_headers(pointing))).json()['state'] == 'shutter_closed'
        app.local['state'] = 'live'
        assert (await console.get(path() + '/frame', params={'view': pointing})).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stop', 'new_worker', 'pause', 'source', 'expiry'])
async def test_late_jpeg_cannot_restore_ended_view(tmp_path, monkeypatch, change):
    async with environment(tmp_path, monkeypatch) as app:
        token = await open_view(app.browser)
        began, resume = asyncio.Event(), asyncio.Event()
        private = app.owner._private_worker_request

        async def delayed(*args, **kwargs):
            report = await private(*args, **kwargs)
            if kwargs.get('jpeg'):
                began.set()
                await resume.wait()
            return report

        app.owner._private_worker_request = delayed
        waiting = asyncio.create_task(app.browser.get(path() + '/frame', params={'view': token}))
        await asyncio.wait_for(began.wait(), 1)
        if change == 'stop':
            await app.browser.post(path() + '/preview/stop', json={'token': token})
        elif change == 'new_worker':
            app.owner.process = SimpleNamespace(returncode=None)
        elif change == 'pause':
            app.owner.capture_enabled = False
            await app.owner.live_previews.close()
            app.owner.capture_enabled = True
        elif change == 'source':
            app.configured[PRIMARY] = dict(id=PRIMARY, device=dict(did='SYNTHETIC-REPLACEMENT'))
        else:
            app.clock[0] += VIEW_IDLE_SECONDS
        resume.set()
        result = await waiting
        assert result.status_code == 409 and JPEG not in result.content


@pytest.mark.asyncio
async def test_reconnected_capture_requires_new_view_for_the_same_camera(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as app:
        token = await open_view(app.browser)
        app.scope['wall'].epochs[PRIMARY] += 1
        assert (await app.browser.get(path() + '/frame', params={'view': token})).status_code == 409
        result = await app.browser.get(path() + '/preview', headers=view_headers(token))
        assert result.json()['detail'] == 'camera_preview_ended'
        fresh = await open_view(app.browser)
        assert fresh != token
        assert (await app.browser.get(path() + '/frame', params={'view': fresh})).content == JPEG


@pytest.mark.asyncio
async def test_video_only_owner_is_released_after_last_view(tmp_path, monkeypatch):
    async with environment(tmp_path, monkeypatch) as app:
        service = app.owner
        service.capture_enabled = False
        service.process = None
        cameras = set()
        manual = SimpleNamespace(process=None, cameras=cameras, has=cameras.__contains__)

        async def command(action, camera, **payload):
            if action == 'start':
                manual.process = manual.process or SimpleNamespace(returncode=None)
                cameras.add(camera)
            if action == 'stop':
                cameras.discard(camera)
            if action == 'frame':
                return {'frame': base64.b64encode(JPEG).decode()}
            return {'state': 'live'}

        manual.command = AsyncMock(side_effect=command)
        manual.close = AsyncMock(side_effect=lambda: cameras.clear())
        service.manual_previews = manual
        console = app.browser
        assert (await console.post(path() + '/preview', json={'purpose': 'identity'})).status_code == 409
        assert not manual.command.called
        live = await open_view(console)
        pointing = await open_view(console, purpose='pointing')
        await console.post(path() + '/preview/stop', json={'token': live})
        assert manual.has(PRIMARY)
        assert (await console.get(path() + '/frame', params={'view': pointing})).content == JPEG
        app.clock[0] += VIEW_IDLE_SECONDS + 1
        assert (await console.get(path() + '/frame', params={'view': pointing})).status_code == 409
        await asyncio.wait_for(service.live_previews.expiry_task, 3)
        assert not manual.has(PRIMARY)
        manual.close.assert_awaited_once()
        assert service.capture_enabled is False
