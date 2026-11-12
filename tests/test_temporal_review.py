"""Scheduling, evidence freshness and process ownership; no real models/devices."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.embodied import paths, safety, safety_api
from homeplane.embodied import temporal_model as model
from homeplane.embodied.rapid_review import RapidReviewWindow
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from homeplane.embodied.temporal_model import GIB, HostBudget, MlxWindowProcess, VisualResult
from homeplane.embodied.temporal_review import TemporalReviewer


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class Engine:
    def __init__(self, event="face_obscured", block=False):
        self.event = event
        self.calls = []
        self.closed = False
        self.ready = asyncio.Event()
        if not block:
            self.ready.set()

    async def analyze(self, frames):
        self.calls.append(frames)
        await self.ready.wait()
        return VisualResult(event=self.event, observation="合成可见变化")

    async def close(self):
        self.closed = True


class PostureEngine(Engine):
    def __init__(self):
        super().__init__(event='prone_posture')
        self.scopes = []

    async def analyze_scoped(self, select):
        frames, posture = select()
        self.scopes.append(posture)
        return await self.analyze(frames)


def rig(engine=None):
    clock, events = Clock(), []
    policy = SimpleNamespace(enabled=True, visual_review=True, revision=1)
    reviewer = TemporalReviewer(
        policy=lambda: policy,
        deliver=lambda *args: events.append(args),
        engine=engine or Engine(),
        clock=clock,
        wall=lambda: 1800000000,
    )
    return reviewer, clock, policy, events


async def feed(reviewer, clock, start, end, session="s", settle=True):
    for index in range(start, end):
        clock.value = 100 + index
        reviewer.offer("camera", session, 1, clock.value, b"synthetic-jpeg", stable=True)
        if settle and reviewer.task:
            await reviewer.task
            await asyncio.sleep(0)


def paired_poses(offset, *, anchor_offset=0, near=True, track=2):
    def joint(x, y):
        return dict(x=x, y=y, confidence=.96)

    def torso(x, y, length):
        return {side+part: joint(x+dx, y+dy*length) for side, dx in (('left', -.024), ('right', .024))
                for part, dy in (('Shoulder', -.5), ('Hip', .5))}

    y = .59+offset
    anchor_joints = torso(.36, .49+anchor_offset, .20)
    anchor_joints.update(leftWrist=joint(.50 if near else .25, y), rightWrist=joint(.55 if near else .29, y))
    return [
        dict(id=1, box=dict(x=.19, y=.24+anchor_offset, w=.40, h=.52, confidence=.97), joints=anchor_joints),
        dict(id=track, box=dict(x=.44, y=y-.14, w=.20, h=.28, confidence=.96), joints=torso(.53, y, .12)),
    ]


@pytest.mark.parametrize('scenario', ['no_contact', 'shared_motion', 'new_track', 'occluded', 'three_people',
                                      'jumped_sample', 'slow', 'new_aspect', 'valid'])
def test_rapid_window_requires_visible_pair_and_two_continuous_motion_steps(scenario):
    detector = RapidReviewWindow()
    offsets = (0, 0, -.15, -.15) if scenario == 'jumped_sample' else (0, 0, -.07, -.15)
    interval = .6 if scenario == 'slow' else .25
    for index, offset in enumerate(offsets):
        now = 100 + index*interval
        people = paired_poses(offset, anchor_offset=offset if scenario == 'shared_motion' else 0,
                              near=scenario != 'no_contact', track=3 if scenario == 'new_track' and index >= 2 else 2)
        if scenario == 'occluded' and index == 2:
            del people[1]['joints']['leftShoulder']
        if scenario == 'three_people' and index == 2:
            people.append({'id': 4})
        aspect = 4/3 if scenario == 'new_aspect' and index >= 2 else 16/9
        detector.offer(people, aspect, now, bytes([index]), now)
    if scenario == 'valid':
        assert detector.pending(now) == tuple((100 + i*.25, bytes([i])) for i in range(4))
        assert not detector.samples and not detector.pending(now+6.01)
    else:
        assert not detector.pending(now)


@pytest.mark.asyncio
@pytest.mark.parametrize(('event_kind','capacity','should_save'), [
    ('rapid_movement', None, True), ('none', None, False), ('possible_fall', None, False),
    ('rapid_movement', 3, False),
])
async def test_single_rapid_window_reaches_real_private_event_and_frames(tmp_path, monkeypatch,
                                                                       event_kind, capacity, should_save):
    clock = Clock()

    def wall():
        return 1800000000+clock()

    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=clock, time=wall))
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    engine = Engine(event=event_kind)
    runtime.visual.engine, runtime.visual.clock, runtime.visual.wall = engine, clock, wall
    runtime.recording.clock = clock
    if capacity is not None:
        runtime.recording.MAX_REVIEW_BYTES = capacity
    app = FastAPI()
    app.include_router(router)
    base = '/api/embodied/safety'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            configured = await client.post(base+'/policy', json=dict(enabled=True, visual_review=True, announce=True))
            assert configured.status_code == 200
            for index, offset in enumerate((0, 0, -.07, -.15)):
                clock.value = 100 + index*.25
                assert runtime.offer('cam_'+'a'*10, 'session', paired_poses(offset), clock(), clock(), stable=True,
                                     jpeg=bytes([index]), source_context='b'*64, source_name='合成镜头')
                await runtime.drain()
                if runtime.visual.task is not None:
                    await runtime.visual.task
                    await asyncio.sleep(0)
            await runtime.drain()
            assert engine.calls == [tuple((100+i*.25, bytes([i])) for i in range(4))]
            events = (await client.get(base+'/events')).json()['events']
            assert bool(events) == should_save
            assert not runtime.tasks
            if not should_save:
                assert runtime.recording.dropped == (1 if capacity is not None else 0)
                return
            saved, = events
            assert saved['basis'] == 'same_pair_rapid_window' and saved['visual_kind'] == 'rapid_movement'
            assert saved['captured_at'] == wall() and saved['observation'] is None
            assert saved['person_name'] is None and saved['person_id'] is None and saved['track'] is None
            assert saved['announcement']['status'] == 'not_requested'
            event_url = base+'/events/'+saved['id']
            for ordinal in range(4):
                response = await client.get(event_url+'/frames/'+str(ordinal))
                assert response.content == bytes([ordinal])
            assert (await client.post(event_url+'/review', json=dict(review='false_alarm'))).json()['review'] == 'false_alarm'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_rapid_window_hands_the_following_slot_back_to_ready_ordinary_camera():
    engine = Engine(event='none')
    reviewer, clock, _, _ = rig(engine)
    try:
        for index, offset in enumerate((0, 0, -.07, -.15)):
            clock.value = 100+index*.25
            reviewer.offer('first', 's', 1, clock(), b'first', stable=True, rapid_people=paired_poses(offset))
        await reviewer.task
        await asyncio.sleep(0)
        assert reviewer.last_rapid and len(engine.calls) == 1
        for value in (103.5,104.5,105.5,106.5):
            clock.value = value
            reviewer.offer('ordinary', 's', 1, value, b'ordinary', stable=True)
        for index, offset in enumerate((0, 0, -.07, -.15)):
            clock.value = 106.5+index*.25
            reviewer.offer('later', 's', 1, clock(), b'later', stable=True, rapid_people=paired_poses(offset))
        assert reviewer.task is None and reviewer.windows['later'].rapid.pending(clock())
        clock.value = 107.3
        reviewer.offer('ordinary', 's', 1, clock(), b'ordinary', stable=True)
        assert reviewer.active_camera == 'ordinary'
        await reviewer.task
        assert len(engine.calls) == 2 and not reviewer.last_rapid
    finally:
        await reviewer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('wait_frames', [2, 8])
async def test_rapid_window_is_frozen_during_model_startup_and_expires(wait_frames):
    class StartingEngine(Engine):
        async def analyze_latest(self, select):
            await self.ready.wait()
            return await self.analyze(select())

    engine = StartingEngine(event='none', block=True)
    reviewer, clock, _, _ = rig(engine)
    try:
        for index, offset in enumerate((0, 0, -.07, -.15)):
            clock.value = 100+index*.25
            reviewer.offer('camera', 's', 1, clock(), bytes([index]), stable=True, rapid_people=paired_poses(offset))
        task = reviewer.task
        assert task and reviewer.stability_pending('camera', 's', 1)
        await asyncio.sleep(0)
        for index in range(wait_frames):
            clock.value = 101.5 + index*.85
            reviewer.offer('camera', 's', 1, clock(), b'later', stable=True, rapid_people=[])
        engine.ready.set()
        await task
        expected = [tuple((100+i*.25, bytes([i])) for i in range(4))] if wait_frames == 2 else []
        assert engine.calls == expected
    finally:
        await reviewer.close()


@pytest.mark.asyncio
async def test_retiring_a_previous_source_does_not_cancel_the_current_camera():
    engine = Engine()
    reviewer, clock, _, _ = rig(engine)
    try:
        await feed(reviewer, clock, 0, 4)
        assert reviewer.last_camera == 'camera'
        engine.ready.clear()
        for value in (106,107,108,109):
            clock.value = value
            reviewer.offer('other', 's', 1, value, b'current', stable=True)
        await asyncio.sleep(0)
        task = reviewer.task
        assert reviewer.active_camera == 'other' and task
        reviewer.clear('camera')
        assert not task.cancelling()
        engine.ready.set()
        await task
        assert reviewer.last_camera == 'other'
    finally:
        await reviewer.close()


@pytest.mark.asyncio
async def test_two_disjoint_windows_persist_both_evidence_sequences_and_deduplicate():
    r, clock, policy, events = rig()
    await feed(r, clock, 0, 4)
    assert not events
    await feed(r, clock, 4, 10)
    assert len(events) == 1 and len(events[0][2]) == 8
    stamps = [f[0] for f in events[0][2]]
    assert stamps == sorted(set(stamps))
    await feed(r, clock, 10, 25)
    assert len(events) == 1
    await r.close()


@pytest.mark.asyncio
async def test_jitter_does_not_send_a_window_the_model_worker_will_reject():
    engine = Engine()
    r, clock, _, _ = rig(engine)
    for captured in (100, 101.7, 103.4, 105.1, 106.2):
        clock.value = captured
        r.offer('camera', 'session', 1, captured, b'frame', stable=True)
        if r.task:
            await r.task
            await asyncio.sleep(0)
    assert len(engine.calls) == 1
    assert [stamp for stamp, _ in engine.calls[0]] == [101.7, 103.4, 105.1, 106.2]
    await r.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [None, 2])
async def test_prone_corroboration_cannot_bridge_lost_or_replaced_subjects(replacement):
    engine = PostureEngine()
    r, clock, _, events = rig(engine)
    for index in range(16):
        clock.value = 100 + index
        track = replacement if index == 4 else (2 if replacement == 2 and index > 4 else 1)
        r.offer('camera', 'session', 1, clock(), b'frame', stable=True, posture_track=track)
        if r.task:
            await r.task
            await asyncio.sleep(0)
        if index == 9:
            assert not events
    assert len(events) == 1 and engine.scopes == [True, True, True]
    assert [stamp for stamp, _ in events[0][2]] == list(range(106, 110)) + list(range(112, 116))
    await r.close()


@pytest.mark.asyncio
async def test_unscoped_model_suggestions_cannot_create_posture_reviews():
    engine = PostureEngine()
    r, clock, _, events = rig(engine)
    await feed(r, clock, 0, 16)
    assert engine.scopes == [False, False, False] and not events
    await r.close()


@pytest.mark.asyncio
async def test_bound_posture_policy_routes_same_subject_frames_into_the_actual_review_api(tmp_path, monkeypatch):
    clock = Clock()

    def wall():
        return 1800000000 + clock()

    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=clock, time=wall))
    monkeypatch.setattr(paths, 'PRIVATE', tmp_path)
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    monkeypatch.setattr('homeplane.embodied.temporal_review.model_readiness', lambda: dict(available=True))
    chosen, other = 'cam_' + 'a'*10, 'cam_' + 'b'*10
    config = dict(version=1, cameras=[dict(id=chosen, name='合成照护镜头', device=dict(did='synthetic-a')),
                                      dict(id=other, name='合成客厅镜头', device=dict(did='synthetic-b'))])
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    source = safety_api.configured_camera_sources()[0]['source_context']
    router = APIRouter()

    async def request(route, body=None):
        if route.endswith('/policy'):
            await runtime.sync_policy()
            return {}
        assert route.endswith('/status')
        return runtime.status()

    runtime = safety_api.install(router, SimpleNamespace(process=SimpleNamespace(returncode=None), request=request))
    engine = PostureEngine()
    runtime.visual.engine, runtime.visual.clock, runtime.visual.wall = engine, clock, wall
    app = FastAPI()
    app.include_router(router)
    person = dict(id=1, box=dict(x=.32, y=.38, w=.31, h=.18, confidence=.9), joints={})
    transport = httpx.ASGITransport(app=app)
    base = '/api/embodied/safety'
    try:
        async with httpx.AsyncClient(transport=transport, base_url='http://local') as client:
            original = (await client.get(base)).json()
            assert original['posture_review']['state'] == 'off'
            assert original['posture_review']['cameras'] == [dict(id=chosen, name='合成照护镜头'),
                                                            dict(id=other, name='合成客厅镜头')]
            assert source not in json.dumps(original)
            invalid = await client.post(base+'/posture', json=dict(camera='cam_'+'f'*10, expected_revision=0))
            assert invalid.status_code == 409
            forged = await client.post(base+'/posture', json=dict(camera=chosen, expected_revision=0, source_context=source))
            assert forged.status_code == 422
            choice = await client.post(base+'/posture', json=dict(camera=chosen, expected_revision=0))
            assert choice.json() == dict(camera=chosen, revision=1)
            assert (await client.get(base)).json()['posture_review']['state'] == 'paused'
            response = await client.post(base+'/policy', json=dict(enabled=True, visual_review=True, announce=True, revision=1))
            assert response.json()['revision'] == 2 and 'posture_source' not in response.json()
            with runtime.store.connect() as db:
                original_row, = db.execute('SELECT value FROM safety_policy WHERE id=1').fetchone()
            assert safety.SafetyControls.model_validate_json(original_row).revision == 2
            assert runtime.store.policy().posture_source.source_context == source
            assert (await client.get(base)).json()['posture_review']['state'] == 'waiting'
            stale = await client.post(base+'/posture', json=dict(camera=None, expected_revision=1))
            assert stale.status_code == 409
            for index in range(10):
                clock.value = 100 + index
                await runtime.observe(chosen, 'session', [person], clock(), clock(), stable=True,
                                      jpeg=bytes([index]), source_context=source, source_name='合成照护镜头')
                if runtime.visual.task:
                    await runtime.visual.task
                    await asyncio.sleep(0)
                if index < 9:
                    assert not runtime.recording.jobs
            await runtime.drain()
            assert engine.scopes == [True, True]
            assert (await client.get(base)).json()['posture_review']['state'] == 'observing'
            event, = (await client.get(base+'/events')).json()['events']
            assert event['visual_kind'] == 'prone_posture' and event['camera'] == chosen
            assert event['basis'] == 'same_track_two_local_windows' and event['observation'] is None
            assert event['track'] is None and event['person_name'] is None and not runtime.tasks
            assert event['announcement']['status'] == 'not_requested'
            frames = [(await client.get(base+'/events/'+event['id']+'/frames/'+str(i))).content for i in range(8)]
            assert frames == [bytes([i]) for i in (0,1,2,3,6,7,8,9)]
            done = await client.post(base+'/events/'+event['id']+'/review', json=dict(review='acknowledged'))
            assert done.json()['review'] == 'acknowledged'
            config['cameras'][0]['device']['did'] = 'synthetic-replacement'
            path.write_text(json.dumps(config))
            assert (await client.get(base)).json()['posture_review']['state'] == 'source_changed'
            assert not runtime._cached_policy().admits_posture(chosen, safety_api.configured_camera_sources()[0]['source_context'])
            assert (await client.post(base+'/posture', json=dict(camera=None, expected_revision=2))).json()['camera'] is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_busy_drops_backlog_and_next_request_contains_latest_frames():
    engine = Engine(block=True)
    r, clock, _, events = rig(engine)
    await feed(r, clock, 0, 4, settle=False)
    await asyncio.sleep(0)
    await feed(r, clock, 4, 10, settle=False)
    await asyncio.sleep(0)
    assert len(engine.calls) == 1 and r.skipped > 0
    engine.ready.set()
    await r.task
    await asyncio.sleep(0)
    await feed(r, clock, 10, 11)
    assert len(engine.calls) == 2
    assert [f[0] for f in engine.calls[-1]] == [107, 108, 109, 110]
    await r.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["privacy", "revision", "disabled", "session", "stale", "motion"])
async def test_late_results_cannot_cross_privacy_policy_session_or_freshness(fence):
    engine = Engine(block=True)
    r, clock, policy, events = rig(engine)
    await feed(r, clock, 0, 4, settle=False)
    await asyncio.sleep(0)
    if fence == "privacy":
        r.clear("camera")
    elif fence == "revision":
        policy.revision = 2
    elif fence == "disabled":
        policy.visual_review = False
    elif fence == "session":
        r.offer("camera", "new", 1, clock.value + 0.1, b"frame", stable=True)
    elif fence == "stale":
        clock.value += 14
    else:
        r.offer("camera", "s", 1, clock.value + 0.1, b"frame", stable=False)
    engine.ready.set()
    if r.task:
        await asyncio.gather(r.task, return_exceptions=True)
    assert not events
    await r.close()


@pytest.mark.asyncio
async def test_normal_or_unclear_window_breaks_confirmation_and_gap_clears_buffer():
    engine = Engine()
    r, clock, _, events = rig(engine)
    await feed(r, clock, 0, 4)
    engine.event = "none"
    await feed(r, clock, 4, 10)
    engine.event = "face_obscured"
    await feed(r, clock, 10, 16)
    assert not events
    await feed(r, clock, 30, 34)
    assert not events
    await r.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'enabled':False},{'announce':True}])
async def test_policy_watcher_revokes_inference_without_more_frames_or_status_reads(tmp_path,change):
    from homeplane.embodied.safety import SafetyPolicy
    store=SafetyStore(tmp_path/'safety.sqlite')
    policy=store.configure(SafetyPolicy(enabled=True,visual_review=True))
    engine=Engine(block=True)
    runtime=SafetyRuntime(store,visual_engine=engine)
    await runtime.start(interval=.01)
    engine.closed=False
    clock=Clock()
    runtime.visual.clock=clock
    try:
        await feed(runtime.visual,clock,0,4,settle=False)
        for _ in range(100):
            if engine.calls:
                break
            await asyncio.sleep(.001)
        assert engine.calls and runtime.visual.task is not None
        store.configure(policy.model_copy(update=change))
        for _ in range(100):
            if engine.closed and not runtime.visual.windows and runtime.visual.task is None:
                break
            await asyncio.sleep(.005)
        assert engine.closed and not runtime.visual.windows and runtime.visual.task is None
        assert not store.events()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unreadable_optional_safety_policy_does_not_abort_perception_startup(tmp_path):
    from unittest.mock import Mock
    store=SafetyStore(tmp_path/'safety.sqlite')
    store.read_policy=Mock(side_effect=OSError('synthetic_unreadable_policy'))
    runtime=SafetyRuntime(store,visual_engine=Engine())
    try:
        await runtime.start(interval=.01)
        await asyncio.sleep(.02)
        assert runtime.policy_task is not None and not runtime.policy_task.done()
        assert runtime.error=='safety_storage_unavailable' and not runtime.visual.windows
    finally:
        await runtime.close()


def test_model_pin_is_checked_without_loading_or_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "WEIGHT_BYTES", 3)
    monkeypatch.setattr(model.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(model.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(model.importlib.util, "find_spec", lambda name: object())
    for name in ("config.json", "tokenizer.json"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"abc")
    manifest = dict(repository=model.MODEL_ID, revision=model.REVISION, weight_sha256=model.WEIGHT_SHA256)
    (tmp_path / "friday-source.json").write_text(json.dumps(manifest))
    assert model.model_readiness(tmp_path)["available"]
    manifest["revision"] = "wrong"
    (tmp_path / "friday-source.json").write_text(json.dumps(manifest))
    assert model.model_readiness(tmp_path)["reason"] == "model_pin_mismatch"
    (tmp_path / "friday-source.json").write_text("[]")
    assert not model.model_readiness(tmp_path)["available"]


def test_mac_budget_reserves_headroom_and_defers_unknown_or_busy_host():
    assert HostBudget(16 * GIB, 40, 2, 10).reason() is None
    assert HostBudget(16 * GIB, 25).reason() == "memory_pressure"
    assert HostBudget(16 * GIB, 25).reason(resident=True) is None
    assert HostBudget(16 * GIB, None).reason() == "memory_unknown"
    assert HostBudget(8 * GIB, 80).reason() == "memory_capacity"
    assert HostBudget(16 * GIB, 50, 20, 10).reason() == "cpu_busy"


@pytest.mark.asyncio
async def test_model_text_cannot_supply_identity_severity_or_actions(tmp_path):
    with pytest.raises(ValueError):
        VisualResult(event="face_obscured", observation="sample", action="broadcast")
    store = SafetyStore(tmp_path / "safety.sqlite")
    store.configure(SafetyPolicy(enabled=True, visual_review=True))
    runtime = SafetyRuntime(store)
    now = time.monotonic()
    await runtime.observe('camera', 'session', [], now, now, stable=True, jpeg=b'jpeg')
    assert runtime._visual_event(
        "camera",
        "session",
        [(now - 1, b"jpeg"), (now, b"jpeg")],
        VisualResult(event="face_obscured", observation="模型观察待复核"),
        1800000000,
    )
    await runtime.drain()
    event = runtime.store.events()[0]
    assert event["kind"] == "visual_review" and event["severity"] == "review"
    assert event["person_id"] is None and event["announcement"]["status"] == "not_requested"
    assert len(event["frames"]) == 2 and not runtime.tasks
    await runtime.close()


@pytest.mark.asyncio
async def test_corroborated_visual_review_saves_original_frames_after_write_failure(tmp_path, monkeypatch):
    from homeplane.embodied import safety

    clock = Clock()

    def wall():
        return 1800000000 + clock()

    monkeypatch.setattr(safety, 'time', SimpleNamespace(monotonic=clock, time=wall))
    store = SafetyStore(tmp_path / 'safety.sqlite')
    store.configure(SafetyPolicy(enabled=True, visual_review=True))
    engine = Engine()
    runtime = SafetyRuntime(store, visual_engine=engine)
    runtime.visual.clock = clock
    runtime.visual.wall = wall
    create = store.create
    attempts = []

    def interrupted_write(event, frames, **options):
        attempts.append((options.get('event_id'), event['captured_at'], tuple(frames)))
        if len(attempts) == 1:
            raise sqlite3.OperationalError('synthetic interrupted write')
        return create(event, frames, **options)

    monkeypatch.setattr(store, 'create', interrupted_write)
    try:
        for index in range(13):
            clock.value = 100 + index
            await runtime.observe('camera', 'session', [], clock(), clock(), stable=True,
                                  jpeg=bytes([index]), source_context='a' * 64)
            if runtime.visual.task:
                await runtime.visual.task
                await asyncio.sleep(0)
        await asyncio.wait_for(runtime.drain(), 1)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        saved, = store.events()
        assert saved['id'] == attempts[0][0] and saved['captured_at'] == 1800000109
        assert [store.frame(saved['id'], index) for index in range(8)] == [bytes([i]) for i in (0, 1, 2, 3, 6, 7, 8, 9)]
        assert len(engine.calls) == 2 and runtime.visual.error is None
        assert not runtime.tasks
    finally:
        await runtime.close()


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdin = self.stdout = self
        self.reads = 0

    def write(self, data):
        self.request = data

    async def drain(self):
        pass

    async def readline(self):
        self.reads += 1
        if self.reads == 1:
            return b'{"version":1,"ready":true}\n'
        return b'{"version":1,"result":{"event":"none","observation":"unchanged"},"peak_mlx_bytes":123}\n'

    async def read(self, limit):
        return b""

    def close(self):
        pass

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_process_lease_allows_only_one_owner_and_releases_on_close(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "model_readiness", lambda path: {"available": True})
    created = []

    async def spawn(*args, **kwargs):
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert kwargs["env"]["OMP_NUM_THREADS"] == "2"
        process = FakeProcess()
        created.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    def budget():
        return HostBudget(16 * GIB, 50)

    a = MlxWindowProcess(tmp_path, tmp_path, budget=budget)
    b = MlxWindowProcess(tmp_path, tmp_path, budget=budget)
    frames = [(float(i), b"frame") for i in range(4)]
    try:
        assert (await a.analyze(frames)).event == "none"
        assert set(json.loads(created[0].request)) == {'frames'}
        assert (await a.analyze_scoped(lambda: (frames, True))).event == 'none'
        assert json.loads(created[0].request)['review_prone'] is True
        with pytest.raises(ValueError):
            await b.analyze(frames)
        assert len(created) == 1 and a.lease is not None
        await a.close()
        assert (await b.analyze(frames)).event == "none"
        assert len(created) == 2
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_privacy_clear_unloads_idle_model_and_discards_confirmation():
    engine = Engine()
    reviewer, clock, _, _ = rig(engine)
    await feed(reviewer, clock, 0, 4)
    assert reviewer.last_camera == "camera" and not engine.closed
    reviewer.clear("camera")
    await reviewer.cleanup_task
    assert engine.closed and not reviewer.windows
    await reviewer.close()


@pytest.mark.asyncio
async def test_cancelled_request_and_memory_pressure_release_the_child(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "model_readiness", lambda path: {"available": True})
    spawned, read = asyncio.Event(), asyncio.Event()
    processes = []

    class Blocked(FakeProcess):
        async def readline(self):
            if self.reads == 0:
                return await super().readline()
            read.set()
            await asyncio.Event().wait()

    async def spawn(*args, **kwargs):
        process = Blocked() if not processes else FakeProcess()
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    host = [HostBudget(16 * GIB, 50)]
    engine = MlxWindowProcess(tmp_path, tmp_path, budget=lambda: host[0])
    frames = [(float(i), b"frame") for i in range(4)]
    task = asyncio.create_task(engine.analyze(frames))
    await spawned.wait()
    await read.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert engine.process is engine.lease is None and processes[0].returncode == 0
    await engine.analyze(frames)
    host[0] = HostBudget(16 * GIB, 5)
    with pytest.raises(ValueError, match="memory_pressure"):
        await engine.analyze(frames)
    assert engine.process is engine.lease is None and processes[1].returncode == 0
    await engine.close()


@pytest.mark.asyncio
async def test_global_cadence_rotates_cameras_instead_of_multiplying_gpu_load():
    engine = Engine(event="none")
    reviewer, clock, _, _ = rig(engine)
    for tick in range(20):
        clock.value = 100 + tick
        for camera in ("a", "b"):
            reviewer.offer(camera, "s", 1, clock.value, camera.encode(), stable=True)
            if reviewer.task:
                await reviewer.task
                await asyncio.sleep(0)
    assert len(engine.calls) == 3
    assert [frames[0][1] for frames in engine.calls] == [b"a", b"b", b"a"]
    await reviewer.close()


@pytest.mark.asyncio
async def test_resource_failures_back_off_instead_of_reloading_on_every_frame():
    class Busy(Engine):
        async def analyze(self, frames):
            self.calls.append(frames)
            raise ValueError("memory_pressure")

    engine = Busy()
    reviewer, clock, _, events = rig(engine)
    await feed(reviewer, clock, 0, 17)
    assert len(engine.calls) == 1 and reviewer.error == "memory_pressure" and not events
    await feed(reviewer, clock, 17, 21)
    assert len(engine.calls) == 2
    await reviewer.close()
