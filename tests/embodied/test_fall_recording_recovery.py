"""Original fall geometry and private recorder; synthetic people/frames only."""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from homeplane.embodied import safety
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from tests.test_embodied_safety import person


@pytest_asyncio.fixture
async def monitor(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1800000000+clock[0]))
    store = SafetyStore(tmp_path/'safety.sqlite')
    store.configure(SafetyPolicy(enabled=True, announce=True))
    runtime = SafetyRuntime(store, speaker=SimpleNamespace(say=AsyncMock(return_value=dict(status='accepted'))))
    value = SimpleNamespace(runtime=runtime, store=store, clock=clock, context='a'*64, camera='primary')
    yield value
    await runtime.close()


async def frame(monitor, *, down=True, camera=None, context=None, session='same-session', sync=False,
                jpeg=b'EXPLICIT-SYNTHETIC-JPEG'):
    runtime = monitor.runtime
    if sync:
        await runtime.sync_policy()
    now = monitor.clock[0]
    return runtime.offer(camera or monitor.camera, session, [person(down)], now, now, stable=True,
                         jpeg=jpeg, source_context=context or monitor.context)


async def before_fall(monitor):
    for i in range(15):
        monitor.clock[0] = 100+i*.25
        await frame(monitor, down=i>=7, sync=True)
        await monitor.runtime.observation_task
    monitor.clock[0] += .25
    await monitor.runtime.sync_policy()


@pytest.mark.asyncio
async def test_qualified_fall_survives_a_storage_failure_without_a_second_fall(monitor, monkeypatch):
    create = monitor.store.create
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(dict(args[0]))
        if len(attempts) == 1:
            raise sqlite3.OperationalError('synthetic busy writer')
        return create(*args, **kwargs)

    monkeypatch.setattr(monitor.store, 'create', fail_once)
    await before_fall(monitor)
    qualified_at = 1800000000+monitor.clock[0]
    await frame(monitor)
    await asyncio.sleep(.01)
    assert monitor.store.events() == []
    monitor.runtime.speaker.say.assert_not_awaited()
    for _ in range(8):
        monitor.clock[0] += .25
        await frame(monitor, sync=True)
        await asyncio.sleep(.01)
    await monitor.runtime.drain()
    events = monitor.store.events()
    assert len(events) == 1, 'the original qualified fall was lost after a transient save failure'
    assert len(attempts) >= 2 and events[0]['captured_at'] == qualified_at
    assert events[0]['frames'] and events[0]['camera_source_context'] == monitor.context


@pytest.mark.asyncio
async def test_safety_observation_continues_and_pending_incident_survives_expired_policy_cache(monitor, monkeypatch):
    create = monitor.store.create
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def busy_once(*args, **kwargs):
        calls.append(dict(args[0]))
        if len(calls) == 1:
            entered.set()
            release.wait(2)
            failed.set()
            raise sqlite3.OperationalError('synthetic busy writer')
        return create(*args, **kwargs)

    monkeypatch.setattr(monitor.store, 'create', busy_once)
    await before_fall(monitor)
    qualified_at = 1800000000+monitor.clock[0]
    await frame(monitor)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(4):
            monitor.clock[0] += .25
            await frame(monitor)
            await frame(monitor, camera='secondary', down=False)
            await asyncio.sleep(.005)
        assert monitor.runtime.last_seen.get('secondary') == monitor.clock[0], 'one incident save blocked all safety input'
        for _ in range(8):
            monitor.clock[0] += .25
            await frame(monitor)
            await asyncio.sleep(0)
        release.set()
        assert await asyncio.to_thread(failed.wait, 1)
        await asyncio.sleep(.01)
        monitor.clock[0] += .5
        await frame(monitor)
        await asyncio.wait_for(monitor.runtime.drain(), 1)
        events = monitor.store.events()
        assert len(events) == 1 and events[0]['captured_at'] == qualified_at
        monitor.runtime.speaker.say.assert_not_awaited()  # a recovered history item is not a fresh audible warning
    finally:
        release.set()


@pytest.mark.asyncio
async def test_uncertain_commit_readback_does_not_create_a_duplicate(monitor, monkeypatch):
    create = monitor.store.create
    ids = []

    def commit_then_lose_reply(*args, **kwargs):
        value = create(*args, **kwargs)
        ids.append(value['id'])
        if len(ids) == 1:
            raise OSError('synthetic lost commit receipt')
        return value

    monkeypatch.setattr(monitor.store, 'create', commit_then_lose_reply)
    await before_fall(monitor)
    await frame(monitor)
    for _ in range(10):
        await asyncio.sleep(.01)
        monitor.clock[0] += .25
        await frame(monitor, sync=True)
    await monitor.runtime.drain()
    assert len(ids) >= 2, 'the recorder abandoned an uncertain write without reconciling it'
    assert len(set(ids)) == len(monitor.store.events()) == 1


def test_scoped_incident_create_is_idempotent_without_replacing_review_state(tmp_path):
    store = SafetyStore(tmp_path/'safety.sqlite')
    event = dict(kind='fall', camera='primary', session='same', track='7', started_mono=100,
                 captured_at=1800000000, camera_source_context='a'*64,
                 basis='upright_rapid_descent_sustained_horizontal', detector='geometry-fall/1')
    saved = store.create(event, [(1800000000, b'frame')], event_id='1'*32)
    store.update(saved['id'], review='false_alarm')
    same = store.create(event, [(1800000000, b'frame')], event_id='1'*32)
    assert same['review'] == 'false_alarm' and len(store.events()) == 1
    assert len(store.events()[0]['frames']) == 1
    with pytest.raises(ValueError):
        store.create(dict(event, session='replacement'), event_id='1'*32)


def test_retried_post_frame_is_not_duplicate_evidence(tmp_path):
    store = SafetyStore(tmp_path/'safety.sqlite')
    event = store.create(dict(kind='fall'))
    for _ in range(3):
        store.append_frame(event['id'], 1800000000, b'frame', deduplicate=True)
    assert len(store.events()[0]['frames']) == 1


@pytest.mark.asyncio
async def test_review_quota_reserves_space_and_falls_precede_waiting_reviews():
    from homeplane.embodied.safety_recording import SafetyRecorder

    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def persist(job):
        writes.append(job.event['kind'])
        if job.event['kind'] == 'sound_review':
            entered.set()
            await release.wait()
        job.saved = job.event
        recorder.recorded(job)

    recorder = SafetyRecorder(persist=persist, reconcile=AsyncMock(), current=lambda job: True, clock=lambda: 100.)
    recorder.MAX_REVIEW_PENDING, recorder.MAX_REVIEW_BYTES = 2, 4

    def submit(kind, frames):
        feature = None if kind == 'fall' else kind
        return recorder.submit(camera='camera', session='session', context='source', token=object(),
                               captured=100., revision=1, event={'kind': kind}, frames=frames, feature=feature)

    try:
        first = submit('sound_review', [(1, b'123')])
        await asyncio.wait_for(entered.wait(), 1)
        waiting = submit('visual_review', [(2, b'12'), (3, b'3')])
        assert waiting.initial == ((3, b'3'),)
        assert submit('visual_review', [(4, b'1')]) is None
        assert submit('fall', [(4, b'12345')]).initial == ((4, b'12345'),)
        assert recorder.status()['retained_bytes'] == 9
        release.set()
        await asyncio.wait_for(recorder.drain(), 1)
        assert writes == ['sound_review', 'fall', 'visual_review']
        assert first.receipt.saved_at == waiting.receipt.saved_at == 100.
    finally:
        release.set()
        await recorder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fence', ['session', 'context', 'clear', 'policy_off', 'policy_revision', 'input_gap', 'close'])
async def test_pending_incident_cannot_cross_revocation_or_source_replacement(monitor, monkeypatch, fence):
    create = monitor.store.create
    entered, release = threading.Event(), threading.Event()

    def held_create(*args, **kwargs):
        entered.set()
        release.wait(2)
        return create(*args, **kwargs)

    monkeypatch.setattr(monitor.store, 'create', held_create)
    await before_fall(monitor)
    await frame(monitor)
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        monitor.clock[0] += .25
        if fence in {'session', 'context'}:
            await frame(monitor, session='replacement' if fence == 'session' else 'same-session',
                        context='b'*64 if fence == 'context' else monitor.context)
        elif fence == 'clear':
            monitor.runtime.clear(monitor.camera)
        elif fence.startswith('policy'):
            policy = monitor.store.policy()
            monitor.store.configure(policy.model_copy(update={'enabled': fence != 'policy_off'}))
        elif fence == 'input_gap':
            monitor.clock[0] += 2
            await frame(monitor)
        else:
            closing = asyncio.create_task(monitor.runtime.close())
            await asyncio.sleep(.01)
            assert not closing.done()
        release.set()
        if closing:
            await closing
        else:
            await monitor.runtime.drain()
        assert monitor.store.events() == []
        monitor.runtime.speaker.say.assert_not_awaited()
    finally:
        release.set()
        if closing:
            await closing


@pytest.mark.asyncio
async def test_exhausted_recording_is_bounded_and_not_hidden_by_another_camera(monitor, monkeypatch):
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(monitor.clock[0])
        raise OSError('synthetic full storage')

    monkeypatch.setattr(monitor.store, 'create', unavailable)
    await before_fall(monitor)
    await frame(monitor)
    for _ in range(44):
        await asyncio.sleep(.005)
        monitor.clock[0] += .5
        await frame(monitor, sync=True)
    await monitor.runtime.drain()
    status = monitor.runtime.status()
    assert not monitor.runtime.recording.jobs and status['recording']['pending'] == 0
    assert status['recording']['dropped'] == 1 and status['error'] == 'safety_recording_lost'
    assert len(calls) < 16 and not monitor.store.events()
    await frame(monitor, camera='healthy-camera', down=False, sync=True)
    await monitor.runtime.drain()
    assert monitor.runtime.status()['error'] == 'safety_recording_lost'
    monitor.runtime.speaker.say.assert_not_awaited()
    await verify_public_recording_health(monitor, monkeypatch)


@pytest.mark.asyncio
async def test_delayed_labels_do_not_stop_observation_or_name_an_expired_identity(monitor, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def labels():
        entered.set()
        release.wait(2)
        return {'known-person': 'Example'}

    monitor.runtime.labels = labels
    await before_fall(monitor)
    now = monitor.clock[0]
    tracked = person(True, dict(state='matched', person_id='known-person', observed_monotonic=now,
                                face_observed_monotonic=now))
    monitor.runtime.offer(monitor.camera, 'same-session', [tracked], now, now, stable=True,
                          source_context=monitor.context, jpeg=b'SYNTHETIC')
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(6):
            monitor.clock[0] += .25
            await frame(monitor)
            await frame(monitor, camera='secondary', down=False)
            await asyncio.sleep(.005)
        assert monitor.runtime.last_seen['secondary'] == monitor.clock[0]
        release.set()
        await monitor.runtime.drain()
        event = monitor.store.events()[0]
        assert event['person_id'] is None and event['person_name'] is None and 'Example' not in event['message']
    finally:
        release.set()


@pytest.mark.asyncio
async def test_uncertain_post_frame_commit_is_reconciled_without_duplicate_frames(monitor, monkeypatch):
    append = monitor.store.append_frame
    calls = []

    def uncertain(*args, **kwargs):
        value = append(*args, **kwargs)
        calls.append(args[1])
        if len(calls) == 1:
            raise OSError('synthetic lost frame receipt')
        return value

    monkeypatch.setattr(monitor.store, 'append_frame', uncertain)
    await before_fall(monitor)
    await frame(monitor)
    await monitor.runtime.drain()
    for _ in range(12):
        monitor.clock[0] += .25
        await frame(monitor, sync=True)
        await asyncio.sleep(.01)
    await monitor.runtime.drain()
    frames = monitor.store.events()[0]['frames']
    assert len(calls) > len(set(calls)) and len({f['at'] for f in frames}) == len(frames)
    assert len(frames) <= 22


@pytest.mark.asyncio
async def test_recording_queue_and_private_frame_memory_are_bounded():
    from homeplane.embodied.safety_recording import SafetyRecorder

    entered = asyncio.Event()
    release = asyncio.Event()

    async def held(_job):
        entered.set()
        await release.wait()

    recorder = SafetyRecorder(persist=held, reconcile=AsyncMock(return_value=None), current=lambda _: True, clock=lambda: 100)
    recorder.MAX_PENDING, recorder.MAX_BYTES = 3, 32
    try:
        for _ in range(4):
            recorder.submit(camera='same', session='same', context='a'*64, token=None, captured=100,
                            revision=1, event={}, person={}, frames=[(1, b'12345678')]*4, announce=False)
        await entered.wait()
        assert len(recorder.jobs) == 3 and recorder.bytes <= 32
        for i in range(12):
            recorder.frame('same', 100+i*.25, 2+i*.25, b'12345678')
        assert recorder.bytes <= 32 and all(len(job.following) <= 6 for job in recorder.jobs.values())
        assert recorder.status()['dropped'] == 1 and recorder.status()['skipped_frames'] > 0
    finally:
        await recorder.close()
    assert not recorder.jobs and recorder.bytes == 0 and recorder.task is None


@pytest.mark.asyncio
async def test_retired_inflight_recording_keeps_its_memory_budget_until_actual_release():
    from homeplane.embodied.safety_recording import SafetyRecorder

    entered, release = asyncio.Event(), asyncio.Event()

    async def held(_job):
        entered.set()
        await release.wait()

    recorder = SafetyRecorder(persist=held, reconcile=AsyncMock(return_value=None), current=lambda _: True, clock=lambda: 100)
    recorder.MAX_BYTES = 8
    try:
        first = recorder.submit(camera='first', session='s', context=None, token=None, captured=100,
            revision=1, event={}, person={}, frames=[(1, b'12345678')], announce=False)
        await entered.wait()
        recorder.clear('first')
        assert recorder.active is first and recorder.bytes == 8
        second = recorder.submit(camera='second', session='s', context=None, token=None, captured=100,
            revision=1, event={}, person={}, frames=[(1, b'12345678')], announce=False)
        assert second.initial == () and recorder.bytes == 8
    finally:
        await recorder.close()
    assert recorder.bytes == 0 and recorder.active is None


@pytest.mark.asyncio
async def test_deadline_readback_can_confirm_a_committed_incident_without_replaying_audio(monitor, monkeypatch):
    create = monitor.store.create

    def always_lose_receipt(*args, **kwargs):
        create(*args, **kwargs)
        raise OSError('synthetic missing receipt')

    monkeypatch.setattr(monitor.store, 'create', always_lose_receipt)
    await before_fall(monitor)
    await frame(monitor)
    for _ in range(44):
        await asyncio.sleep(.005)
        monitor.clock[0] += .5
        await frame(monitor, sync=True, jpeg=b'')
    await monitor.runtime.drain()
    report = monitor.runtime.recording_status()
    assert len(monitor.store.events()) == 1 and report['completed'] == 1
    assert report['dropped'] == report['partial'] == report['unconfirmed'] == 0
    assert report['state'] == 'ready'
    monitor.runtime.speaker.say.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_final_readback_does_not_fabricate_a_missing_or_successful_record(monitor, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError('synthetic storage failure')

    monkeypatch.setattr(monitor.store, 'create', unavailable)
    monkeypatch.setattr(monitor.store, 'recording_receipt', unavailable)
    await before_fall(monitor)
    await frame(monitor)
    for _ in range(44):
        await asyncio.sleep(.005)
        monitor.clock[0] += .5
        await frame(monitor, sync=True)
    await monitor.runtime.drain()
    report = monitor.runtime.recording_status()
    assert report['dropped'] == report['completed'] == 0 and report['unconfirmed'] == 1
    assert report['state'] == 'unavailable'
    monitor.runtime.speaker.say.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sqlite_writer_wait_does_not_age_out_a_qualified_incident(monitor, monkeypatch):
    await before_fall(monitor)
    qualified_at = 1800000000+monitor.clock[0]
    busy = threading.Event()
    create = monitor.store.create

    def observe_busy(*args, **kwargs):
        try:
            return create(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                busy.set()
            raise

    monkeypatch.setattr(monitor.store, 'create', observe_busy)
    blocker = sqlite3.connect(monitor.store.path)
    blocker.execute('BEGIN IMMEDIATE')
    try:
        await frame(monitor)

        async def until_retry_pending():
            while not (busy.is_set() and any(job.failed for job in monitor.runtime.recording.jobs.values())):
                await asyncio.sleep(.05)
                monitor.clock[0] += .05
                await frame(monitor)
                await frame(monitor, camera='secondary', down=False)

        # 等真实 SQLite 竞争进入原记录器的重试状态，不以调度耗时推测它已发生。
        await asyncio.wait_for(until_retry_pending(), 10)
        for _ in range(16):
            monitor.clock[0] += .25
            await frame(monitor)
            await frame(monitor, camera='secondary', down=False)
            await asyncio.sleep(0)
        blocker.commit()
        await asyncio.wait_for(monitor.runtime.drain(), 2)
        events = monitor.store.events()
        assert len(events) == 1 and events[0]['captured_at'] == qualified_at
        assert events[0]['frames'] and monitor.runtime.recording.retried >= 1
        monitor.runtime.speaker.say.assert_not_awaited()
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.asyncio
async def test_recording_health_recovers_without_probing_native_models(monitor, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(monitor.runtime.visual, 'status', Mock(side_effect=AssertionError('no visual model probe')))
    monkeypatch.setattr(monitor.runtime.sound, 'status', Mock(side_effect=AssertionError('no sound model probe')))
    await monitor.runtime.sync_policy()
    assert monitor.runtime.recording_status()['state'] == 'ready'
    original = monitor.store.policy
    monkeypatch.setattr(monitor.store, 'policy', lambda: (_ for _ in ()).throw(OSError('synthetic unreadable store')))
    with pytest.raises(OSError):
        await monitor.runtime.sync_policy()
    assert monitor.runtime.recording_status()['state'] == 'recovering'
    monitor.clock[0] += monitor.runtime.recording.RETRY_WINDOW
    assert monitor.runtime.recording_status()['state'] == 'unavailable'
    monkeypatch.setattr(monitor.store, 'policy', original)
    await monitor.runtime.sync_policy()
    assert monitor.runtime.recording_status()['state'] == 'ready'


async def verify_public_recording_health(monitor, monkeypatch):
    """A real exhausted recorder -> original view/service/HTTP -> original UI projection."""
    import json
    import subprocess
    from pathlib import Path

    import httpx
    from fastapi import FastAPI

    from homeplane.embodied import service as service_module
    from homeplane.embodied.motion_games import MotionGames
    from tests.embodied.test_camera_audio import actual

    camera = 'cam_1111111111'
    from homeplane.embodied.party_camera import PartyCamera
    scope = dict(state=dict(status='live', camera_id=camera, processed_frames=1),party_camera=PartyCamera(),
        games=MotionGames(clock=lambda: monitor.clock[0]),
        recorder=SimpleNamespace(public=lambda: {}), safety=monitor.runtime,
        home=SimpleNamespace(public=lambda: {}), secondary_homes={}, fleet=SimpleNamespace(public=lambda _: {}),
        PRIVATE=monitor.store.path.parent, time=safety.time, last_frame=0, camera_planner=None,
        SELECTED={'id': camera}, configuration=lambda: {'cameras': [{'id': camera}]}, previews=None)
    public = actual('homeplane/embodied/worker.py', 'view', scope)()
    owner = service_module.EmbodiedService()
    owner.configured = lambda: True
    owner.process = SimpleNamespace(returncode=None)
    owner.request = AsyncMock(return_value=public)
    monkeypatch.setattr(service_module, 'PRIVATE', monitor.store.path.parent)
    monkeypatch.setattr(service_module, 'configuration', lambda: {'cameras': [{'id': camera}]})
    app = FastAPI()
    app.add_api_route('/api/embodied/status', actual('homeplane/embodied/api.py', 'status', {'service': owner}))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            response = await client.get('/api/embodied/status')
        assert response.status_code == 200 and response.json()['safety_recording']['dropped'] == 1
        script = """
import assert from 'node:assert/strict';
import {homeServiceSummary,homeAttention} from './homeplane/static/home-status.js';
let data='';for await(const chunk of process.stdin)data+=chunk;
const worker=JSON.parse(data),recording=worker.safety_recording;
assert.equal(homeServiceSummary({worker}).rows.find(r=>r.label==='异常记录').state,'warning');
const [notice]=homeAttention([],[],[],recording);
assert.equal(notice.title,'有提醒未能保存');assert.equal(notice.page,'Safety');assert.equal(notice.eventId,undefined);
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script],
            input=json.dumps(response.json()), cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        await owner.client.aclose()
