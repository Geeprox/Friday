"""Synthetic fall evidence -> metadata reference -> real archive resolver contract."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.archive_contracts import ArchiveCameraEdit, ArchiveResolveRequest
from homeplane.archive_index import ArchiveIndex
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from homeplane.embodied.safety_archive import SafetyArchive
from tests.test_embodied_safety import person
from tests.test_homeplane_archive_index import setup as setup

CAMERA = {'id': 'cam_1111111111', 'name': 'SYNTHETIC camera', 'device': {'did': 'SYNTHETIC-device'}}
SOURCE = camera_source_context(CAMERA)
CAPTURED = datetime(2026, 10, 1, 0, 0, 30, tzinfo=UTC).timestamp()


async def mapped(index, *, timing=True, capture=CAMERA['id'], camera_id=None):
    context = await index.scan(index.context()['fingerprint'])
    return index.save_camera(ArchiveCameraEdit(
        expected_context=context['fingerprint'], folder_ref=context['folders'][0]['folder_ref'],
        camera_id=camera_id, capture_camera_id=capture, name='SYNTHETIC recording',
        timing_reviewed=timing, clock_offset_seconds=0, clock_uncertainty_seconds=0,
        completion_convention='closed_files_operator_reviewed'))


def catalog(index):
    current = [dict(id=CAMERA['id'], name=CAMERA['name'], source_context=SOURCE)]
    index.capture_cameras = lambda: current
    return current


def test_simulator_never_consults_the_real_camera_registry(setup):
    index, _, _, _, _, _, _ = setup
    consulted = []
    def forbidden():
        consulted.append(True)
        raise AssertionError('SYNTHETIC callback must not run in Simulator')
    index.capture_cameras = forbidden
    index.simulated = lambda: True
    context = index.context()
    assert context['simulated'] is True and context['capture_cameras'] == []
    assert consulted == []


def event(store, **changes):
    return store.create(dict(kind='fall', simulated=False, camera=CAMERA['id'],
                             camera_source_context=SOURCE, captured_at=CAPTURED) | changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['fall', 'visual_review', 'sound_review'])
async def test_reviewed_physical_source_survives_restart_and_resolves_the_actual_time_window(setup, tmp_path, kind):
    index, _, _, traces, settings, clock, calls = setup
    catalog(index)
    first = await mapped(index)
    assert first['capture_cameras'] == [{'id': CAMERA['id'], 'name': CAMERA['name']}]
    assert first['cameras'][0]['capture_binding_current']
    original = traces.connection.execute('SELECT body FROM archive_profiles').fetchone()[0]
    reopened = ArchiveIndex(traces, settings, lambda: False, runner=index.runner, clock=lambda: clock[0],
                            capture_cameras=index.capture_cameras)
    try:
        assert reopened.context()['cameras'][0]['capture_binding_current']
        assert traces.connection.execute('SELECT body FROM archive_profiles').fetchone()[0] == original
    finally:
        await reopened.close()
    store = SafetyStore(tmp_path / 'private/safety.sqlite')
    saved = event(store, kind=kind)
    before = list(calls)
    found = SafetyArchive(store, index).references(saved['id'])
    assert calls == before, 'Listing event references must not scan/read NAS or invoke a model'
    assert found['code'] == 'configured_camera_time_lookup' and len(found['recordings']) == 1
    request = ArchiveResolveRequest.model_validate(found['recordings'][0]['request'])
    assert request.time_range.start == datetime(2026, 10, 1, 0, 0, 15, tzinfo=UTC)
    assert request.time_range.end == datetime(2026, 10, 1, 0, 0, 45, tzinfo=UTC)
    clock[0] = clock[0].replace(second=3)
    resolved = await index.resolve(request)
    assert resolved.status == 'ready' and resolved.camera_id == first['cameras'][0]['id']
    assert not resolved.media_copied and not resolved.device_write_attempted


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'missing', 'legacy', 'forged_flag', 'nas', 'unreviewed_clock'])
@pytest.mark.parametrize('kind', ['fall', 'visual_review', 'sound_review'])
async def test_stale_or_unreviewed_mapping_cannot_attach_a_recording_to_an_event(setup, tmp_path, change, kind):
    index, _, _, traces, settings, _, calls = setup
    registry = catalog(index)
    await mapped(index, timing=change != 'unreviewed_clock')
    store = SafetyStore(tmp_path / 'private/safety.sqlite')
    saved = event(store, kind=kind)
    if change == 'source':
        registry[0]['source_context'] = 'b' * 64
    elif change == 'missing':
        registry.clear()
    elif change in {'legacy', 'forged_flag'}:
        identifier, raw = traces.connection.execute('SELECT id,body FROM archive_profiles').fetchone()
        body = json.loads(raw)
        body.pop('capture_source_context')
        body['capture_binding_current'] = change == 'forged_flag'
        with traces.connection:
            traces.connection.execute('UPDATE archive_profiles SET body=? WHERE id=?', (json.dumps(body), identifier))
    elif change == 'nas':
        settings.update({'nas_path': str(tmp_path / 'SYNTHETIC-other-NAS')})
    before = list(calls)
    found = SafetyArchive(store, index).references(saved['id'])
    assert not found['recordings'] and calls == before
    assert found['code'] in {'archive_capture_mapping_changed', 'archive_recording_clock_unreviewed'}


@pytest.mark.asyncio
async def test_unknown_capture_is_rejected_and_clear_is_an_explicit_edit(setup):
    index, _, _, traces, _, _, _ = setup
    catalog(index)
    with pytest.raises(ValueError, match='archive_capture_camera_unavailable'):
        await mapped(index, capture='cam_2222222222')
    assert traces.connection.execute('SELECT count(*) FROM archive_profiles').fetchone()[0] == 0
    first = await mapped(index)
    updated = await mapped(index, camera_id=first['cameras'][0]['id'], capture=None)
    assert updated['cameras'][0]['capture_camera_id'] is None
    assert not updated['cameras'][0]['capture_binding_current']


@pytest.mark.asyncio
async def test_real_route_does_not_backfill_legacy_event_sources_or_accept_request_injection(setup, tmp_path, monkeypatch):
    from homeplane.embodied import safety_api
    index, _, _, _, _, _, calls = setup
    catalog(index)
    await mapped(index)
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path / 'private')
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None), archive=index)
    app = FastAPI()
    app.include_router(router)
    good = event(runtime.store)
    legacy = runtime.store.create(dict(kind='fall', simulated=False, camera=CAMERA['id'], captured_at=CAPTURED))
    before = list(calls)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/embodied/safety/events/'+good['id']+'/recordings')
        assert response.status_code == 200 and len(response.json()['recordings']) == 1
        response = await client.get('/api/embodied/safety/events/'+legacy['id']+'/recordings?camera_source_context='+SOURCE)
        assert response.json()['code'] == 'event_source_unverified' and not response.json()['recordings']
        assert (await client.get('/api/embodied/safety/events/missing/recordings')).status_code == 404
    assert calls == before
    await runtime.close()


@pytest.mark.asyncio
async def test_sound_search_recordings_use_the_selected_saved_view_on_the_original_source(setup, tmp_path, monkeypatch):
    from homeplane.embodied import safety_api
    index, _, _, _, _, clock, calls = setup
    registry = catalog(index)
    await mapped(index)
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path / 'private')
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None), archive=index)
    app = FastAPI()
    app.include_router(router)
    try:
        policy = runtime.store.configure(SafetyPolicy(enabled=True, sound_review=True))
        policy = runtime.store.configure_sound_search(policy.revision, True)
        saved = runtime.store.create(dict(kind='sound_review', simulated=False, camera=CAMERA['id'],
            session='synthetic-session', camera_source_context=SOURCE, captured_at=CAPTURED),
            frames=[(CAPTURED-1,b'synthetic-original')])
        unrelated = event(runtime.store, kind='sound_review')
        for step, delta in enumerate((6,18),start=1):
            runtime.store.append_sound_search_frame(saved['id'], (CAMERA['id'],'synthetic-session',SOURCE),
                step, CAPTURED+delta, bytes([step]), policy_revision=policy.revision, guard=lambda:True)
        before = list(calls)
        path = '/api/embodied/safety/events/'+saved['id']+'/recordings'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            original = (await client.get(path)).json()
            view = (await client.get(path, params={'ordinal':2,'captured_at':CAPTURED-600})).json()
            request = ArchiveResolveRequest.model_validate(view['recordings'][0]['request'])
            assert view['event_id'] == saved['id'] and view['frame_ordinal'] == 2
            assert request.time_range.start.timestamp() == CAPTURED+3
            assert request.time_range.end.timestamp() == CAPTURED+33
            assert ArchiveResolveRequest.model_validate(original['recordings'][0]['request']).time_range.end.timestamp() == CAPTURED+15
            assert view['source_scan_attempted'] is False and view['media_read_attempted'] is False
            for endpoint, ordinal in [(path,0),(path,9),(path.replace(saved['id'],unrelated['id']),2)]:
                reply = (await client.get(endpoint,params={'ordinal':ordinal})).json()
                assert reply['code'] == 'event_frame_unavailable' and not reply['recordings']
            assert (await client.get(path,params={'ordinal':22})).status_code == 422
            registry[0]['source_context'] = 'b'*64
            assert (await client.get(path,params={'ordinal':2})).json()['code'] == 'archive_capture_mapping_changed'
            registry[0]['source_context'] = SOURCE
            assert calls == before
            clock[0] = clock[0].replace(second=3)
            resolved = await index.resolve(request)
            assert resolved.camera_id == request.camera_id and resolved.time_range == request.time_range
            assert not resolved.media_copied and not resolved.device_write_attempted
            with runtime.store.connect() as db:
                db.execute('UPDATE safety_events SET at=0 WHERE id=?',(saved['id'],))
            assert (await client.get(path,params={'ordinal':2})).status_code == 404
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_recorded_sound_review_reaches_original_http_lookup_and_archive_resolver(setup, tmp_path, monkeypatch):
    """Synthetic corroborated result, actual recorder/API/metadata/resolve; no audio/model."""
    from homeplane.embodied import safety, safety_api
    index, _, _, _, _, clock, calls = setup
    catalog(index)
    await mapped(index)
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path / 'private')
    monkeypatch.setattr(safety.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(safety.time, 'time', lambda: CAPTURED)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None), archive=index)
    app = FastAPI()
    app.include_router(router)
    try:
        runtime.store.configure(SafetyPolicy(enabled=True, sound_review=True))
        await runtime.sync_policy()
        await runtime.observe(CAMERA['id'], 'synthetic-session', [], 100.0, 100.0,
                              stable=True, source_context=SOURCE, source_name=CAMERA['name'])
        assert runtime._sound_event(CAMERA['id'], 'synthetic-session', SOURCE, dict(
            kind='crying', confidence=.94, captured_at=CAPTURED, captured_mono=100.0, corroborating_windows=2,
            audio_seconds=4, age_basis='local_sdk_receive', policy_revision=runtime.policy_cache.revision))
        await runtime.drain()
        saved = runtime.store.events()[0]
        assert saved['kind'] == 'sound_review' and saved['raw_audio_stored'] is False
        assert saved['directional_audio'] is False and saved['camera_source_context'] == SOURCE
        before = list(calls)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/embodied/safety/events/'+saved['id']+'/recordings')
            assert response.status_code == 200 and response.json()['code'] == 'configured_camera_time_lookup'
        assert calls == before, 'Reading an event reference never scans or reads NAS'
        request = ArchiveResolveRequest.model_validate(response.json()['recordings'][0]['request'])
        clock[0] = clock[0].replace(second=3)
        resolved = await index.resolve(request)
        assert resolved.status == 'ready' and not resolved.media_copied and not resolved.device_write_attempted
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_records_capture_identity_and_breaks_continuity_on_source_replacement(tmp_path, monkeypatch):
    from homeplane.embodied import safety
    clock = [100.0]
    monkeypatch.setattr(safety.time, 'monotonic', lambda: clock[0])
    store = SafetyStore(tmp_path / 'private/safety.sqlite')
    store.configure(SafetyPolicy(enabled=True))
    runtime = SafetyRuntime(store)
    try:
        for i in range(25):
            clock[0] = 100 + i * .25
            await runtime.observe(CAMERA['id'], 'same-session', [person(i >= 7)], clock[0], clock[0],
                                  stable=True, source_context=SOURCE if i < 8 else 'b' * 64)
        assert store.events() == [], 'A replacement camera cannot inherit the old upright baseline'
        for i in range(25):
            clock[0] = 110 + i * .25
            await runtime.observe(CAMERA['id'], 'same-session', [person(i >= 7)], clock[0], clock[0],
                                  stable=True, source_context='b' * 64, source_name=CAMERA['name'])
        saved = store.events()
        assert len(saved) == 1 and saved[0]['camera_source_context'] == 'b' * 64
        assert saved[0]['camera_name'] == CAMERA['name']
        assert abs(saved[0]['captured_at']-safety.time.time()) < 5
    finally:
        await runtime.close()
