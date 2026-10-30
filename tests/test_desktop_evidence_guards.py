"""Desktop request guard with typed synthetic domains; no service/device startup."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from homeplane.event_evidence import EvidenceDetail, EvidenceQuery
from homeplane.archive_contracts import ArchivePlaybackRequest, ArchiveResolveRequest


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://127.0.0.1:8740'


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.setenv('FRIDAY_HOME', str(tmp_path.resolve()))
    monkeypatch.setenv('FRIDAY_EMBODIED_DATA', str((tmp_path / 'embodied').resolve()))
    monkeypatch.setenv('FRIDAY_SETTINGS', str(tmp_path / 'settings.json'))
    spec = importlib.util.spec_from_file_location(
        'evidence_guard_backend', ROOT / 'native/FridayDesktop/backend/desktop_backend.py')
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    inner = FastAPI()
    seen = []
    qualified = set()

    @inner.post('/api/home/evidence/query')
    async def query(request: EvidenceQuery):
        seen.append('query')
        return {'expected_context': request.expected_context}

    @inner.post('/api/home/evidence/detail')
    async def detail(request: EvidenceDetail):
        seen.append('detail')
        return {'event_id': request.event_id}

    @inner.post('/api/archive/resolve')
    async def resolve(request: ArchiveResolveRequest):
        seen.append('resolve')
        return {'camera_id': request.camera_id}

    @inner.post('/api/archive/playback')
    async def playback(request: ArchivePlaybackRequest):
        seen.append('playback')
        return {'media_ref': request.media_ref}

    @inner.post('/api/home/recordings/stop')
    async def stop():
        seen.append('stop')
        return {'stopped': True}

    @inner.post('/api/home/unreviewed-operation')
    async def unreviewed():
        raise AssertionError('Unreviewed home mutation must remain held')

    @inner.get('/api/archive/media/{identifier}')
    @inner.get('/api/home/recordings/media/{identifier}')
    async def media(identifier: str):
        seen.append('media')
        return {'protected': True, 'identifier': identifier}

    inner.state.core = SimpleNamespace(actions=SimpleNamespace(policy_report=lambda: {'enabled': False, 'configured': False}))
    inner.state.archive_player = SimpleNamespace(protected_preview=lambda identifier: ('archive', identifier) in qualified)
    inner.state.home_experience = SimpleNamespace(recordings=SimpleNamespace(
        protected_preview=lambda identifier: ('recording', identifier) in qualified))
    wrapper = backend.DesktopService()
    wrapper.inner = inner
    return wrapper, seen, qualified


@pytest.mark.asyncio
async def test_new_reads_are_typed_bounded_and_require_exact_origin(guard):
    wrapper, seen, _ = guard
    window = {'start': '2026-01-01T00:00:00Z', 'end': '2026-01-01T00:01:00Z'}
    requests = {
        '/api/home/evidence/query': {'expected_context': 'a' * 64, 'room': 'SYNTHETIC-ROOM'},
        '/api/home/evidence/detail': {'expected_context': 'a' * 64, 'projection_id': 'b' * 64, 'event_id': 'synthetic-event'},
        '/api/archive/resolve': {'expected_context': 'a' * 64, 'camera_id': 'synthetic-camera', 'time_range': window},
        '/api/archive/playback': {'expected_context': 'a' * 64, 'camera_id': 'synthetic-camera', 'time_range': window, 'media_ref': 'b' * 64},
        '/api/home/recordings/stop': {'token': 'a' * 48},
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for path, body in requests.items():
            accepted = await client.post(path, json=body, headers={'Origin': ORIGIN})
            assert accepted.status_code == 200, (path, accepted.text)
            before = len(seen)
            for origin in (None, 'https://invalid.example'):
                headers = {} if origin is None else {'Origin': origin}
                assert (await client.post(path, json=body, headers=headers)).status_code == 403
            invalid = await client.post(path, json={**body, 'execute': True}, headers={'Origin': ORIGIN})
            assert invalid.status_code == 422, (path, invalid.text)
            oversized = await client.post(path, content=b'x' * 8193, headers={'Origin': ORIGIN})
            assert oversized.status_code == 413
            assert len(seen) == before
        assert (await client.post('/api/home/unreviewed-operation', json={}, headers={'Origin': ORIGIN})).status_code == 409
        wrapper.inner = None
        for path, body in requests.items():
            assert (await client.post(path, json=body, headers={'Origin': ORIGIN})).status_code == 503


@pytest.mark.asyncio
async def test_only_owned_protected_preview_media_passes_the_desktop_guard(guard):
    wrapper, seen, qualified = guard
    cases = [('archive', 'a' * 64, '/api/archive/media/'),
             ('recording', 'b' * 48, '/api/home/recordings/media/')]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for kind, identifier, prefix in cases:
            path = prefix + identifier
            assert (await client.get(path, headers={'Origin': ORIGIN})).status_code == 409
            qualified.add((kind, identifier))
            for headers in ({'Origin': ORIGIN}, {'Sec-Fetch-Site': 'same-origin'}):
                accepted = await client.get(path, headers=headers)
                assert accepted.status_code == 200
                assert accepted.json()['protected'] is True
            before = len(seen)
            for bad in (prefix + identifier.upper(), path + '/extra', prefix + 'unknown'):
                assert (await client.get(bad, headers={'Origin': ORIGIN})).status_code == 409
            for headers in ({}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
                assert (await client.get(path, headers=headers)).status_code == 409
            assert (await client.get(path, headers={'Origin': 'https://invalid.example'})).status_code == 403
            qualified.remove((kind, identifier))
            assert (await client.get(path, headers={'Origin': ORIGIN})).status_code == 409
            assert len(seen) == before
        assert (await client.get('/api/family-media/originals/' + 'a' * 64, headers={'Origin': ORIGIN})).status_code == 409
        del wrapper.inner.state.archive_player
        assert (await client.get('/api/archive/media/' + 'a' * 64, headers={'Origin': ORIGIN})).status_code == 409
