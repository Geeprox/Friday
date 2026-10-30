"""Scheduling, evidence freshness and process ownership; no real models/devices."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from homeplane.embodied import temporal_model as model
from homeplane.embodied.safety import SafetyRuntime, SafetyStore
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
async def test_busy_drops_backlog_and_next_request_contains_latest_frames():
    engine = Engine(block=True)
    r, clock, _, events = rig(engine)
    await feed(r, clock, 0, 10, settle=False)
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
    clock=Clock();runtime.visual.clock=clock
    try:
        await feed(runtime.visual,clock,0,4,settle=False)
        for _ in range(100):
            if engine.calls:break
            await asyncio.sleep(.001)
        assert engine.calls and runtime.visual.task is not None
        store.configure(policy.model_copy(update=change))
        for _ in range(100):
            if engine.closed and not runtime.visual.windows and runtime.visual.task is None:break
            await asyncio.sleep(.005)
        assert engine.closed and not runtime.visual.windows and runtime.visual.task is None
        assert not store.events()
    finally:await runtime.close()


@pytest.mark.asyncio
async def test_unreadable_optional_safety_policy_does_not_abort_perception_startup(tmp_path):
    from unittest.mock import Mock
    store=SafetyStore(tmp_path/'safety.sqlite')
    store.policy=Mock(side_effect=OSError('synthetic_unreadable_policy'))
    runtime=SafetyRuntime(store,visual_engine=Engine())
    try:
        await runtime.start(interval=.01)
        await asyncio.sleep(.02)
        assert runtime.policy_task is not None and not runtime.policy_task.done()
        assert runtime.error=='safety_storage_unavailable' and not runtime.visual.windows
    finally:await runtime.close()


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


def test_model_text_cannot_supply_identity_severity_or_actions(tmp_path):
    with pytest.raises(ValueError):
        VisualResult(event="face_obscured", observation="sample", action="broadcast")
    runtime = SafetyRuntime(SafetyStore(tmp_path / "safety.sqlite"))
    runtime._visual_event(
        "camera",
        "session",
        [(1.0, b"jpeg"), (2.0, b"jpeg")],
        VisualResult(event="face_obscured", observation="模型观察待复核"),
        1800000000,
    )
    event = runtime.store.events()[0]
    assert event["kind"] == "visual_review" and event["severity"] == "review"
    assert event["person_id"] is None and event["announcement"]["status"] == "not_requested"
    assert len(event["frames"]) == 2 and not runtime.tasks


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdin = self.stdout = self

    def write(self, data):
        self.request = data

    async def drain(self):
        pass

    async def readline(self):
        return b'{"version":1,"result":{"event":"none","observation":"unchanged"},"peak_mlx_bytes":123}\n'

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
