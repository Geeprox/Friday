"""Actual safety owners against delayed storage; no perception or device I/O."""

import asyncio
import sqlite3
import threading
import time
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
    read = store.policy
    release = threading.Event()

    def slow_policy():
        release.wait(.3)
        return read()

    monkeypatch.setattr(store, 'policy', slow_policy)
    runtime = SafetyRuntime(store, speaker=SimpleNamespace(say=AsyncMock()))
    now = time.monotonic()
    observation = asyncio.create_task(runtime.observe('synthetic', 'session', [], now, now, stable=True))
    try:
        started = time.perf_counter()
        await asyncio.sleep(.01)
        assert time.perf_counter() - started < .15, 'Storage blocked the shared camera/game event loop'
        assert not observation.done(), 'The deliberately held policy read already ran on the event loop'
    finally:
        release.set()
        await observation
        await runtime.close()


def synthetic_runtime(tmp_path):
    store = SafetyStore(tmp_path / 'safety.sqlite')
    store.configure(SafetyPolicy(enabled=True))
    runtime = SafetyRuntime(store, speaker=SimpleNamespace(say=AsyncMock()))

    def detect(camera, session, people, *_args, **_kwargs):
        if not people or not people[0].get('trigger'):
            return []
        return [dict(kind='fall', severity='urgent', camera=camera, session=session,
                     track='synthetic-track', person_id=None, simulated=False, status='suspected')]

    runtime.detector.observe = Mock(side_effect=detect)
    return runtime


def offer(runtime, camera='primary', *, session='session', context='a' * 64, trigger=False):
    now = time.monotonic()
    return runtime.offer(camera, session, [dict(trigger=trigger)], now, now, stable=True,
                         jpeg=b'SYNTHETIC-NOT-CAMERA-PIXELS', source_context=context)


@pytest.mark.asyncio
async def test_latest_mailboxes_keep_actual_secondary_callback_live_and_revoke_old_writes(tmp_path, monkeypatch):
    runtime = synthetic_runtime(tmp_path)
    await runtime.sync_policy()
    entered, release = threading.Event(), threading.Event()
    create = runtime.store.create

    def held_create(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError('测试写入闸门未释放')
        return create(*args, **kwargs)

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
        started = time.thread_time()
        async with asyncio.timeout(2):
            for index in range(100):
                now = time.monotonic()
                people = [dict(id=index)]
                await callback(dict(camera=camera, session='secondary', people=people, hands=[], faces=[],
                                    registration={'status': 'locked'}, now=now, captured_mono=now,
                                    scene_visible=True, camera_source_context='c' * 64), object())
                people[0]['id'] = 'changed-after-admission'
        # 独立验证回调的 CPU 预算与存储等待隔离，避免把宿主调度计为回调计算。
        assert time.thread_time() - started < .15
        assert runtime.io.active is not None and not runtime.io.active.done()
        assert home.observe.await_count == scope['wall'].observe.call_count == 100
        assert runtime.pending[camera][0][2][0]['id'] == 99
        for index in range(10):
            offer(runtime, f'extra-{index}')
        assert len(runtime.sources) == 8 and len(runtime.pending) <= 8
        assert runtime.coalesced == 99 and runtime.skipped >= 4
        runtime.clear(camera)
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
        threads = tuple(runtime.io.executor._threads)
        release.set()
        if cancel_close:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        assert runtime.store.events() == [] and not runtime.io.pending
        assert all(not thread.is_alive() for thread in threads)
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await runtime.close()


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
        assert runtime.offer('held', 'session', [dict(trigger=True)], 10, 10, stable=True, source_context='a' * 64)
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
async def test_console_stays_responsive_during_a_real_private_sqlite_write_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    runtime.store.policy()
    runtime.store.last_prune = -100
    app = FastAPI()
    app.include_router(router)

    @app.get('/synthetic-probe')
    async def probe():
        return {'ready': True}

    lock = sqlite3.connect(runtime.store.path, check_same_thread=False)
    lock.execute('BEGIN IMMEDIATE')
    release = threading.Timer(.3, lock.rollback)
    release.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            reading = asyncio.create_task(client.get('/api/embodied/safety'))
            started = time.perf_counter()
            await asyncio.sleep(.01)
            response = await client.get('/synthetic-probe')
            assert response.json() == {'ready': True}
            assert time.perf_counter() - started < .15, 'A safety SQLite lock froze unrelated Console requests'
            assert (await reading).status_code == 200
    finally:
        await asyncio.to_thread(release.join)
        lock.close()
        await runtime.close()
