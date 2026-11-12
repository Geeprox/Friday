"""Actual safety owners against delayed storage; no perception or device I/O."""

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.embodied import safety_api
from homeplane.embodied.safety import SafetyAdmissionChanged, SafetyPolicy, SafetyRuntime, SafetyStore
from homeplane.embodied.safety_storage import SafetyIO, SafetyStorageBusy
from tests.embodied.test_camera_audio import actual


@pytest.mark.asyncio
async def test_camera_policy_storage_wait_does_not_block_the_event_loop(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / 'safety.sqlite')
    store.configure(SafetyPolicy(enabled=True))
    read = store.read_policy
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_policy():
        entered.set()
        try:
            assert release.wait(5), '测试策略读回闸门未释放'
            return read()
        finally:
            finished.set()

    monkeypatch.setattr(store, 'read_policy', slow_policy)
    runtime = SafetyRuntime(store, speaker=SimpleNamespace(say=AsyncMock()))
    now = time.monotonic()
    observation = asyncio.create_task(runtime.observe('synthetic', 'session', [], now, now, stable=True))
    shutdown = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not finished.is_set() and not observation.done()
        shutdown = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert runtime.closed and not shutdown.done()
        assert not finished.is_set()
    finally:
        release.set()
        await observation
        if shutdown is None:
            await runtime.close()
        else:
            await shutdown
    assert runtime.policy_revision is None and not runtime.source_tokens


def test_readonly_policy_uses_legacy_database_without_upgrading_it(tmp_path):
    store = SafetyStore(tmp_path / 'legacy #?.sqlite')
    assert not store.read_policy().enabled and not store.path.exists()
    legacy = SafetyPolicy(enabled=True, announce=True, revision=3)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute('CREATE TABLE safety_policy (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('INSERT INTO safety_policy VALUES (1, ?)', (legacy.model_dump_json(),))
    assert store.read_policy() == legacy
    with closing(sqlite3.connect(store.path)) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == [('safety_policy',)]
    link = tmp_path / 'redirected.sqlite'
    link.symlink_to(store.path)
    with pytest.raises(ValueError, match='safety_private_path_required'):
        SafetyStore(link).read_policy()


@pytest.mark.asyncio
async def test_worker_prunes_expired_events_and_frames_without_a_console_request(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / 'safety.sqlite')
    policy = store.configure(SafetyPolicy(enabled=True))
    store.create(dict(kind='fall'), [(1, b'SYNTHETIC-FRAME')])
    with store.connect() as db:
        db.execute('UPDATE safety_events SET at=0')
    prune = store.prune_expired
    finished = threading.Event()

    def measured_prune():
        prune()
        finished.set()

    monkeypatch.setattr(store, 'prune_expired', measured_prune)
    runtime = SafetyRuntime(store)
    monkeypatch.setattr(runtime, '_sound_input_open', lambda: False)
    try:
        await runtime.start()
        assert await asyncio.to_thread(finished.wait, 2)
        assert runtime.recording_status()['policy_current'] and runtime.policy_revision == policy.revision
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute('SELECT COUNT(*) FROM safety_events').fetchone() == (0,)
            assert db.execute('SELECT COUNT(*) FROM safety_frames').fetchone() == (0,)
    finally:
        await runtime.close()
    assert runtime.retention_task is None and runtime.policy_task is None


def synthetic_runtime(tmp_path):
    store = SafetyStore(tmp_path / 'safety.sqlite')
    store.configure(SafetyPolicy(enabled=True))
    runtime = SafetyRuntime(store, speaker=SimpleNamespace(say=AsyncMock()))

    def detect(camera, session, people, *_args, **_kwargs):
        if not people or people[0].get('id') != 'synthetic-fall':
            return []
        return [dict(kind='fall', severity='urgent', camera=camera, session=session,
                     track='synthetic-track', person_id=None, simulated=False, status='suspected')]

    runtime.detector.observe = Mock(side_effect=detect)
    return runtime


def offer(runtime, camera='primary', *, session='session', context='a' * 64, trigger=False):
    now = time.monotonic()
    person = dict(id='synthetic-fall' if trigger else 'synthetic-standing')
    return runtime.offer(camera, session, [person], now, now, stable=True,
                         jpeg=b'SYNTHETIC-NOT-CAMERA-PIXELS', source_context=context)


@pytest.mark.asyncio
async def test_latest_mailboxes_keep_actual_secondary_callback_live_and_revoke_old_writes(tmp_path, monkeypatch):
    runtime = synthetic_runtime(tmp_path)
    await runtime.sync_policy()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    create = runtime.store.create

    def held_create(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5), '测试写入闸门未释放'
            return create(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.store, 'create', held_create)
    camera = 'cam_1111111111'
    home = SimpleNamespace(enabled=True, armed=True, speaking=False, last_audio=None,
                           attention=SimpleNamespace(candidate=lambda: None), observe=AsyncMock())
    scope = dict(safety=runtime, secondary_frame_current=lambda *_: True,
                 practice=SimpleNamespace(observe=Mock()), wall=SimpleNamespace(observe=Mock()),
                 fleet=SimpleNamespace(update=Mock(return_value=[]), cameras={}, voice=Mock(return_value=False)),
                 dispatch_interactions=AsyncMock(), secondary_homes={camera: home},
                 interaction_enabled=True, PRIVATE=tmp_path, time=time)
    callback = actual('homeplane/embodied/worker.py', 'secondary_observation', scope)
    try:
        assert offer(runtime, trigger=True)
        assert await asyncio.to_thread(entered.wait, 1)
        source_token = None
        # 在真实 SQLite 写入仍被扣住时验证帧和交互均能前进。
        async with asyncio.timeout(2):
            for index in range(100):
                now = time.monotonic()
                wrist = dict(x=.4, y=.3, confidence=.9)
                box = dict(x=.2, y=.1, w=.3, h=.5, confidence=.9)
                identity = dict(state='tracked', person_id='synthetic-person', observed_monotonic=now,
                                face_observed_monotonic=now, label='ignored')
                first = dict(id=index, box=box, identity=identity, joints=dict(rightWrist=wrist, nose=wrist),
                             motion='still', unrelated={'values': [1, 2]})
                people = [first] + [dict(id=other) for other in range(100, 111)]
                await callback(dict(camera=camera, session='secondary', people=people, hands=[], faces=[],
                                    registration={'status': 'locked'}, now=now, captured_mono=now,
                                    scene_visible=True, camera_source_context='c' * 64), object())
                if index == 0:
                    source_token = runtime.source_tokens[camera]
                first['id'] = 'changed-after-admission'
                wrist['x'] = box['x'] = .8
                identity['state'] = 'unknown'
        assert not release.is_set() and not finished.is_set()
        assert runtime.io.active is not None and not runtime.io.active.done()
        assert home.observe.await_count == scope['wall'].observe.call_count == 100
        queued_people = runtime.pending[camera][0][2]
        assert len(queued_people) == 10
        assert queued_people[0] == dict(id=99, box=dict(x=.2, y=.1, w=.3, h=.5, confidence=.9),
                                       identity=dict(state='tracked', person_id='synthetic-person',
                                                     observed_monotonic=now, face_observed_monotonic=now),
                                       joints=dict(rightWrist=dict(x=.4, y=.3, confidence=.9)), motion='still')
        assert runtime.source_tokens[camera] is source_token and not source_token.is_set()
        for index in range(10):
            offer(runtime, f'extra-{index}')
        assert len(runtime.sources) == 8 and len(runtime.pending) <= 8
        assert runtime.coalesced == 99 and runtime.skipped >= 4
        runtime.clear(camera)
        assert source_token.is_set()
        runtime.clear('primary')
        assert offer(runtime, session='replacement', context='b' * 64)
        release.set()
        await runtime.drain()
        assert runtime.store.events() == [], 'A retired writer must not persist its old event'
        assert not runtime.post_frames.get('primary')
        assert offer(runtime, session='replacement', context='b' * 64, trigger=True)
        await runtime.drain()
        events = runtime.store.events()
        assert len(events) == 1 and events[0]['camera_source_context'] == 'b' * 64
        runtime.speaker.say.assert_not_awaited()
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'append_frame'])
async def test_policy_rechecked_after_real_write_lock_before_evidence_is_committed(tmp_path, monkeypatch, operation):
    runtime = synthetic_runtime(tmp_path)
    await runtime.sync_policy()
    policy = runtime.store.policy()
    saved = runtime.store.create(dict(kind='fall'), [(1, b'initial')])
    entered = threading.Event()
    method = getattr(runtime.store, operation)

    def admitted(*args, **kwargs):
        entered.set()
        return method(*args, **kwargs)

    monkeypatch.setattr(runtime.store, operation, admitted)
    lock = sqlite3.connect(runtime.store.path)
    lock.execute('BEGIN IMMEDIATE')
    if operation == 'create':
        job = asyncio.create_task(runtime.io.call(runtime.store.create, dict(kind='fall'),
                                                  policy_revision=policy.revision))
    else:
        job = asyncio.create_task(runtime.io.call(runtime.store.append_frame, saved['id'], 2, b'new',
                                                  policy_revision=policy.revision))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        revoked = policy.model_copy(update={'enabled': False, 'revision': policy.revision + 1})
        lock.execute('UPDATE safety_policy SET value=? WHERE id=1', (revoked.model_dump_json(),))
        lock.commit()
        with pytest.raises(SafetyAdmissionChanged):
            await job
        events = runtime.store.events()
        assert len(events) == 1 and len(events[0]['frames']) == 1
    finally:
        lock.rollback()
        lock.close()
        await asyncio.gather(job, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_release_or_multiply_the_running_storage_job():
    io = SafetyIO(max_pending=2)
    entered, release = threading.Event(), threading.Event()
    second_started = threading.Event()

    def blocked():
        entered.set()
        release.wait(1)
        return 'first'

    def second():
        second_started.set()
        return 'second'

    first = asyncio.create_task(io.call(blocked))
    later = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        later = asyncio.create_task(io.call(second))
        await asyncio.sleep(.01)
        assert io.pending == 2 and not first.done() and not second_started.is_set()
        with pytest.raises(SafetyStorageBusy):
            await io.call(lambda: 'unbounded-third-job')
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await later == 'second'
        threads = tuple(io.executor._threads)
        await io.close()
        assert not io.pending and all(not thread.is_alive() for thread in threads)
    finally:
        release.set()
        await asyncio.gather(first, *([later] if later else []), return_exceptions=True)
        if not io.closed:
            await io.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_close', [False, True])
async def test_runtime_close_revokes_inputs_before_waiting_for_actual_storage_completion(tmp_path, monkeypatch, cancel_close):
    runtime = synthetic_runtime(tmp_path)
    await runtime.sync_policy()
    entered, release = threading.Event(), threading.Event()
    create = runtime.store.create

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(1)
        return create(*args, **kwargs)

    monkeypatch.setattr(runtime.store, 'create', blocked)
    closing = None
    try:
        offer(runtime, trigger=True)
        assert await asyncio.to_thread(entered.wait, 1)
        offer(runtime, 'secondary')
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(.01)
        assert runtime.closed and not runtime.pending and not runtime.frames
        assert not closing.done(), 'Close returned while the storage thread still owned private evidence'
        assert not offer(runtime, trigger=True)
        if cancel_close:
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done(), 'Repeated cancellation abandoned the actual cleanup owner'
        threads = (*runtime.io.executor._threads, *runtime.policy_io.executor._threads)
        release.set()
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        assert runtime.store.events() == [] and not runtime.io.pending and not runtime.policy_io.pending
        assert all(not thread.is_alive() for thread in threads)
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['sqlite', 'sqlite_and_console_write_queue'])
async def test_saved_alert_and_frame_remain_readable_during_evidence_write(tmp_path, monkeypatch, busy):
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    camera, source = 'cam_1111111111', 'a' * 64
    binding = dict(id='SYNTHETIC-recording', name='示例录像', capture_camera_id=camera, simulated=False,
                   source_binding_current=True, capture_binding_current=True, capture_source_context=source,
                   timing_reviewed=True, completion_convention='closed_files_operator_reviewed',
                   clock_uncertainty_seconds=0)
    archive = SimpleNamespace(context=lambda: dict(configured=True, simulated=False, fingerprint='b' * 64,
                                                    cameras=[binding]))
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None), archive=archive)
    app = FastAPI()
    app.include_router(router)
    captured = time.time() - 1
    pixels = b'SYNTHETIC-JPEG'
    event = runtime.store.create(dict(kind='fall', simulated=False, camera=camera,
        camera_source_context=source, captured_at=captured), [(captured, pixels)])
    url = '/api/embodied/safety/events'
    frame = f'{url}/{event["id"]}/frames/0'
    reference = f'{url}/{event["id"]}/recordings?ordinal=0'
    running, release = threading.Event(), threading.Event()

    def queued():
        running.set()
        assert release.wait(5)

    job = None
    try:
        with closing(sqlite3.connect(runtime.store.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('UPDATE safety_events SET at=0 WHERE id=?', (event['id'],))
            if busy == 'sqlite_and_console_write_queue':
                runtime.io.acquire_timeout = .15
                job = asyncio.create_task(runtime.io.call(queued))
                assert await asyncio.to_thread(running.wait, 1)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
                listing, jpeg, playback = await asyncio.gather(*(client.get(path) for path in (url, frame, reference)))
                assert [response.status_code for response in (listing, jpeg, playback)] == [200, 200, 200]
                [saved] = listing.json()['events']
                assert saved['id'] == event['id'] and saved['frames'] == [
                    dict(ordinal=0, at=captured, recording_anchor=True)]
                assert jpeg.content == pixels and jpeg.headers['cache-control'] == 'no-store'
                found = playback.json()
                assert found['event_id'] == event['id'] and found['frame_ordinal'] == 0
                assert found['recordings'][0]['request']['camera_id'] == binding['id']
                assert not found['source_scan_attempted'] and not found['media_read_attempted']
                if job is not None:
                    assert not job.done()
                writer.commit()
                listing, jpeg, playback = await asyncio.gather(*(client.get(path) for path in (url, frame, reference)))
                assert listing.json() == {'events': []}
                assert jpeg.status_code == playback.status_code == 404
    finally:
        release.set()
        if job is not None:
            await job
        await runtime.close()


@pytest.mark.asyncio
async def test_history_does_not_create_or_upgrade_database_and_rejects_a_link(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    monkeypatch.setattr(safety_api, 'PRIVATE', root)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    app = FastAPI()
    app.include_router(router)
    events = '/api/embodied/safety/events'
    frame, reference = f'{events}/{"c" * 32}/frames/0', f'{events}/{"c" * 32}/recordings'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            assert (await client.get(events)).json() == {'events': []}
            assert (await client.get(frame)).status_code == (await client.get(reference)).status_code == 404
            assert not root.exists()
            root.mkdir()
            legacy = root / 'legacy.sqlite'
            with closing(sqlite3.connect(legacy)) as db, db:
                db.execute('CREATE TABLE safety_events (id TEXT PRIMARY KEY, at REAL NOT NULL, value TEXT NOT NULL)')
                db.execute('INSERT INTO safety_events VALUES (?,?,?)',
                           ('c' * 32, time.time(), '{"kind":"fall","simulated":true}'))
            before = legacy.read_bytes()
            legacy.rename(runtime.store.path)
            assert (await client.get(events)).json() == {'events': [dict(kind='fall', simulated=True, frames=[])]}
            assert (await client.get(frame)).status_code == 404
            assert (await client.get(reference)).json()['code'] == 'event_recording_not_applicable'
            assert runtime.store.path.read_bytes() == before
            runtime.store.path.rename(legacy)
            runtime.store.path.symlink_to(legacy)
            for path in (events, frame, reference):
                denied = await client.get(path)
                assert denied.status_code == 503 and denied.json() == {'detail': 'safety_storage_unavailable'}
            assert legacy.read_bytes() == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_console_shutdown_waits_for_its_owned_evidence_read(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    app = FastAPI()
    app.include_router(router)
    entered, release = threading.Event(), threading.Event()
    original = runtime.store.events

    def wait_for_read():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(runtime.store, 'events', wait_for_read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
        request = asyncio.create_task(client.get('/api/embodied/safety/events'))
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            closing = asyncio.create_task(runtime.close())
            await asyncio.sleep(0)
            assert runtime.closed and not closing.done()
            threads = tuple(runtime.evidence_io.executor._threads)
        finally:
            release.set()
            await asyncio.gather(request, closing if closing is not None else runtime.close())
        assert not runtime.evidence_io.pending and all(not thread.is_alive() for thread in threads)


def test_console_prunes_old_evidence_without_a_camera_worker(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from homeplane.api import create_app

    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path / 'private')
    app = create_app(db_path=tmp_path / 'traces.sqlite', settings_path=tmp_path / 'settings.json')
    service = app.state.embodied
    store = service.safety_output.store
    store.create(dict(kind='fall'), [(1, b'SYNTHETIC-EXPIRED')])
    with store.connect() as db:
        db.execute('UPDATE safety_events SET at=0')
    pruned = threading.Event()
    prune = store.prune_expired

    def maintain():
        prune()
        pruned.set()

    monkeypatch.setattr(store, 'prune_expired', maintain)
    with TestClient(app):
        assert service.process is None
        assert pruned.wait(2)
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute('SELECT COUNT(*) FROM safety_events').fetchone() == (0,)
            assert db.execute('SELECT COUNT(*) FROM safety_frames').fetchone() == (0,)
    assert service.safety_output.retention_task is None


@pytest.mark.asyncio
async def test_api_storage_fault_is_actionable_without_exposing_private_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    app = FastAPI()
    app.include_router(router)

    def fail():
        raise OSError('PRIVATE-DATABASE-PATH-AND-DETAIL')

    monkeypatch.setattr(runtime.store, 'events', fail)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            response = await client.get('/api/embodied/safety/events')
            assert response.status_code == 503
            assert response.json() == {'detail': 'safety_storage_unavailable'}
    finally:
        await runtime.close()


def test_failed_database_initialization_always_closes_its_connection(tmp_path, monkeypatch):
    connection = SimpleNamespace(execute=Mock(side_effect=sqlite3.OperationalError('locked')), close=Mock())
    monkeypatch.setattr('homeplane.embodied.safety.sqlite3.connect', lambda *_args, **_kwargs: connection)
    store = SafetyStore(tmp_path / 'existing.sqlite')
    store.path.touch()
    with pytest.raises(sqlite3.OperationalError), store.connect():
        pass
    connection.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('expired', [False, True])
async def test_identity_is_rechecked_after_delayed_label_lookup(tmp_path, monkeypatch, expired):
    from homeplane.embodied import safety

    clock = [100.0]
    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]))
    runtime = synthetic_runtime(tmp_path)
    await runtime.sync_policy()
    entered, release = threading.Event(), threading.Event()

    def labels():
        entered.set()
        release.wait(1)
        return {'known-person': 'Example'}

    runtime.labels = labels
    runtime.detector.observe = Mock(return_value=[dict(kind='fall', severity='urgent', camera='primary',
        session='session', track='7', person_id='known-person', simulated=False, status='suspected')])
    try:
        now = clock[0]
        runtime.offer('primary', 'session', [dict(id=7, identity=dict(state='matched', person_id='known-person',
            observed_monotonic=now - .95, face_observed_monotonic=now))], now, now, stable=True,
            source_context='a' * 64)
        assert await asyncio.to_thread(entered.wait, 1)
        if expired:
            clock[0] += .1
        release.set()
        await runtime.drain()
        event = runtime.store.events()[0]
        assert event['person_id'] == (None if expired else 'known-person')
        assert event['person_name'] == (None if expired else 'Example')
        assert ('Example' in event['message']) is not expired
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_storage_processing_does_not_relabel_old_camera_input_as_newly_seen(tmp_path, monkeypatch):
    from homeplane.embodied import safety

    clock = [100.0]
    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]))
    runtime = synthetic_runtime(tmp_path)
    try:
        await runtime.observe('primary', 'session', [], 98.75, 98.75, stable=True, source_context='a' * 64)
        assert runtime.last_seen['primary'] == 98.75
        clock[0] = 100.8
        assert runtime.status()['cameras'] == []
        assert runtime.frames
        await runtime.start()
        assert not runtime.frames
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_actual_primary_frame_continues_game_and_home_input_while_safety_storage_waits(tmp_path, monkeypatch):
    from homeplane.embodied import safety
    from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope

    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=lambda: 10, time=lambda: 1000))
    runtime = synthetic_runtime(tmp_path)
    await runtime.sync_policy()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    create = runtime.store.create

    def held(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5), '合成写入闸门未在测试结束前释放'
            return create(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.store, 'create', held)
    scope, published = primary_frame_scope(tmp_path)
    scope['safety'] = runtime
    try:
        assert runtime.offer('held', 'session', [dict(id='synthetic-fall')], 10, 10, stable=True,
                             source_context='a' * 64)
        assert await asyncio.to_thread(entered.wait, 1)
        # 原帧处理必须在持久化闸门仍关闭时完成；墙钟只作死锁上限。
        async with asyncio.timeout(2):
            await frame_continuation(scope)()
        assert not release.is_set() and not finished.is_set()
        assert published == ['live'] and scope['state']['status'] == 'live'
        scope['practice'].observe.assert_called_once()
        scope['home'].observe.assert_awaited_once()
        scope['dispatch_interactions'].assert_awaited_once()
        assert len(runtime.pending) == 1
        runtime.clear('held')
        release.set()
        await runtime.drain()
        assert runtime.store.events() == []
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['manual_previews', 'safety_output', 'stop'])
async def test_original_service_always_stops_worker_and_closes_client_after_optional_cleanup_fault(fault):
    from homeplane.embodied.service import EmbodiedService

    service = EmbodiedService.__new__(EmbodiedService)
    service.closed = False
    service.manual_previews = SimpleNamespace(close=AsyncMock())
    service.safety_output = SimpleNamespace(close=AsyncMock())
    service.stop = AsyncMock()
    service.client = SimpleNamespace(aclose=AsyncMock())
    action = service.stop if fault == 'stop' else getattr(service, fault).close
    action.side_effect = RuntimeError('synthetic cleanup fault')
    with pytest.raises(RuntimeError, match='synthetic cleanup fault'):
        await service.close()
    assert service.closed
    service.manual_previews.close.assert_awaited_once()
    service.safety_output.close.assert_awaited_once()
    service.stop.assert_awaited_once()
    service.client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_console_reads_enabled_policy_while_sqlite_writer_holds_a_reserved_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    policy = runtime.store.configure(SafetyPolicy(enabled=True))
    app = FastAPI()
    app.include_router(router)

    @app.get('/synthetic-probe')
    async def probe():
        return {'ready': True}

    lock = sqlite3.connect(runtime.store.path)
    lock.execute('BEGIN IMMEDIATE')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            async with asyncio.timeout(2):
                status, probe = await asyncio.gather(client.get('/api/embodied/safety'), client.get('/synthetic-probe'))
            assert lock.in_transaction and probe.json() == {'ready': True}
            assert status.status_code == 200 and status.json()['policy'] == policy.model_dump()
    finally:
        lock.close()
        await runtime.close()
