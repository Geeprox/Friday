import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.embodied import safety_api
from homeplane.embodied.safety import FallDetector, SafetyPolicy, SafetyRuntime, SafetyStore


def person(down=False, identity=None):
    joints = {}
    for side, offset in (("left", -0.015), ("right", 0.015)):
        for part, x, y in (
            ("Shoulder", 0.30 if down else 0.46, 0.64 if down else 0.2),
            ("Hip", 0.60 if down else 0.46, 0.65 if down else 0.45),
        ):
            joints[side + part] = dict(x=x + offset, y=y, confidence=0.95)
    return dict(
        id=7,
        box=dict(
            x=0.25 if down else 0.4,
            y=0.58 if down else 0.1,
            w=0.5 if down else 0.15,
            h=0.18 if down else 0.55,
            confidence=0.95,
        ),
        joints=joints,
        identity=identity or {},
    )


def feed(detector, *, mode="fall", identity=False):
    events = []
    for index in range(25):
        now = 100 + index * 0.25
        down = mode == "lying" or (mode == "fall" and index >= 7)
        p = person(down)
        if identity:
            p["identity"] = dict(
                person_id="registered-person",
                state="tracked",
                observed_monotonic=now,
                face_observed_monotonic=now - 2,
            )
        events += detector.observe("camera-a", "session-a", [p], now, now, stable=True)
    return events


def test_fall_requires_transition_and_dwell_and_only_one_event_per_episode():
    assert len(feed(FallDetector())) == 1
    assert feed(FallDetector(), mode="lying") == []
    assert feed(FallDetector(), mode="standing") == []
    assert feed(FallDetector(), identity=True)[0]["person_id"] == "registered-person"


@pytest.mark.parametrize(
    "fence", ["motion", "stale", "gap", "session", "missing_joints", "missing_person", "bad_coordinate"]
)
def test_invalid_observation_breaks_fall_continuity(fence):
    detector = FallDetector()
    events = []
    for index in range(18):
        now = 100 + index * 0.25
        p, session, stable, captured = person(index >= 7), "a", True, now
        if index == 8:
            if fence == "motion":
                stable = False
            if fence == "stale":
                captured = now - 2
            if fence == "gap":
                now += 3
            if fence == "session":
                session = "b"
            if fence == "missing_joints":
                p["joints"] = {}
            if fence == "bad_coordinate":
                p["joints"]["leftHip"]["x"] = float("nan")
        events += detector.observe(
            "camera-a",
            session,
            [] if fence == "missing_person" and index == 8 else [p],
            now,
            captured,
            stable=stable,
        )
    assert events == []


def test_camera_and_track_cannot_borrow_upright_baseline_or_stale_identity():
    detector = FallDetector()
    for i in range(7):
        detector.observe("a", "s", [person()], 100 + i * 0.25, 100 + i * 0.25, stable=True)
    for i in range(7, 25):
        now = 100 + i * 0.25
        p = person(
            True,
            dict(state="tracked", person_id="wrong", observed_monotonic=now, face_observed_monotonic=now - 6),
        )
        assert detector.observe("b", "s", [p], now, now, stable=True) == []
        out = detector.observe("a", "s", [p], now, now, stable=True)
        if out:
            assert out[0]["person_id"] is None


def test_short_horizontal_pose_and_slow_descent_do_not_trigger():
    for mode in ("brief", "slow"):
        d = FallDetector()
        events = []
        for i in range(28):
            now = 100 + i * 0.25
            p = person(i >= 7 if mode == "slow" else 7 <= i < 10)
            if mode == "slow" and 7 <= i < 15:
                p = person()
                p["box"]["h"] = 0.3  # neither standing nor horizontal during descent
            events += d.observe("a", "s", [p], now, now, stable=True)
        assert events == []


def test_store_restart_review_retention_and_disk_budget(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "private/safety.sqlite")
    assert not store.policy().enabled
    policy = store.configure(SafetyPolicy(enabled=True))
    assert policy.revision == 1
    with pytest.raises(ValueError, match="changed"):
        store.configure(SafetyPolicy(enabled=False))
    monkeypatch.setattr(store, "MAX_EVENTS", 2)
    first = store.create(dict(kind="fall"), [(1, b"jpeg1")])
    second = store.create(dict(kind="fall"), [(2, b"jpeg2")])
    store.update(second["id"], review="false_alarm")
    store.create(dict(kind="fall"))
    assert len(store.events()) == 2
    with pytest.raises(ValueError):
        store.frame(first["id"], 0)
    assert SafetyStore(store.path).events()[1]["review"] == "false_alarm"
    monkeypatch.setattr(store, "MAX_BYTES", 1)
    assert all(not e["frames"] for e in store.events())
    assert store.path.stat().st_mode & 0o077 == 0
    with store.connect() as db:
        db.execute("UPDATE safety_events SET at=0")
    assert store.events() == []


@pytest.mark.asyncio
async def test_runtime_persists_before_named_speech_and_keeps_evidence_local(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    store.configure(SafetyPolicy(enabled=True, announce=True))
    clock = [100.0]
    monkeypatch.setattr("homeplane.embodied.safety.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("homeplane.embodied.safety.time.time", lambda: 1800000000 + clock[0])

    async def say(text):
        assert store.events()[0]["person_name"] == "Example Person"
        assert store.events()[0]["frames"]
        assert text == "检测到 Example Person 疑似跌倒，请立即查看。"
        return dict(status="accepted", physical_audio_verified=True)

    speaker = SimpleNamespace(say=AsyncMock(side_effect=say))
    runtime = SafetyRuntime(store, labels=lambda: {"synthetic-person": "Example Person"}, speaker=speaker)
    for i in range(25):
        clock[0] = now = 100 + i * 0.25
        identity = dict(
            state="matched", person_id="synthetic-person", observed_monotonic=now, face_observed_monotonic=now
        )
        await runtime.observe("a", "s", [person(i >= 7, identity)], now, now, stable=True, jpeg=b"local-jpeg")
    await asyncio.gather(*runtime.tasks)
    event = store.events()[0]
    assert event["announcement"] == dict(status="accepted", physical_audio_confirmed=False)
    assert not event["clip_available"] and event["simulated"] is False
    assert 8 < len(event["frames"]) <= 22
    assert store.frame(event["id"], event["frames"][0]["ordinal"]) == b"local-jpeg"
    speaker.say.assert_awaited_once()
    await runtime.close()


@pytest.mark.asyncio
async def test_revocation_and_quiet_are_recorded_without_fabricating_physical_success(tmp_path):
    store = SafetyStore(tmp_path / "safety.sqlite")
    policy = store.configure(SafetyPolicy(enabled=True, announce=True))
    speaker = SimpleNamespace(say=AsyncMock(return_value=dict(status="paused")))
    runtime = SafetyRuntime(store, speaker=speaker)
    event = store.create(dict(message="test"))
    store.configure(policy.model_copy(update={"enabled": False}))
    await runtime.announce(event, revision=policy.revision)
    speaker.say.assert_not_awaited()
    assert store.events()[0]["announcement"]["status"] == "cancelled"
    await runtime.announce(event)
    assert store.events()[0]["announcement"] == dict(status="paused", physical_audio_confirmed=False)


@pytest.mark.asyncio
async def test_api_reading_is_silent_no_evidence_injection_and_test_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, "PRIVATE", tmp_path)
    speaker = AsyncMock(return_value=dict(status="accepted"))
    monkeypatch.setattr("homeplane.embodied.speech.Speaker.say", speaker)
    service = SimpleNamespace(process=None)
    router = APIRouter()
    runtime = safety_api.install(router, service)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        report = (await client.get("/api/embodied/safety")).json()
        assert report["policy"]["enabled"] is False
        assert report["worker"]["status"] == "not_running"
        assert all(c["status"] == "unavailable" for c in report["capabilities"][1:])
        speaker.assert_not_awaited()
        assert (await client.post("/api/embodied/safety/events", json={"kind": "fall"})).status_code == 405
        assert (
            await client.post("/api/embodied/safety/policy", json={"enabled": True, "cloud": True})
        ).status_code == 422
        event = (await client.post("/api/embodied/safety/test", json={})).json()
        await asyncio.gather(*runtime.tasks)
        assert event["simulated"] and event["severity"] == "test" and event["message"].startswith("测试播报")
        result = await client.post(
            "/api/embodied/safety/events/" + event["id"] + "/review", json={"review": "false_alarm"}
        )
        assert result.json()["review"] == "false_alarm"
        assert (await client.get("/api/embodied/safety/events/no/frames/0")).status_code == 404
        assert len((await client.get("/api/embodied/safety/events")).json()["events"]) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_disabled_monitor_has_no_image_buffer_and_revocation_clears_post_capture(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    runtime = SafetyRuntime(store)
    clock = [100.0]
    monkeypatch.setattr("homeplane.embodied.safety.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("homeplane.embodied.safety.time.time", lambda: 1800000000 + clock[0])
    await runtime.observe("a", "s", [person()], 100, 100, stable=True, jpeg=b"private")
    assert not runtime.frames and not store.events()
    policy = store.configure(SafetyPolicy(enabled=True))
    for i in range(17):
        clock[0] = now = 100 + i * 0.25
        await runtime.observe("a", "s", [person(i >= 7)], now, now, stable=True, jpeg=b"private")
    assert runtime.post_frames["a"] and store.events()
    count = len(store.events()[0]["frames"])
    store.configure(policy.model_copy(update={"enabled": False}))
    clock[0] += 0.5
    await runtime.observe("a", "s", [person(True)], clock[0], clock[0], stable=True, jpeg=b"new-private")
    assert not runtime.frames and not runtime.post_frames
    assert len(store.events()[0]["frames"]) == count


@pytest.mark.asyncio
async def test_storage_failure_does_not_crash_camera_observer(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    runtime = SafetyRuntime(store)

    def failed():
        raise OSError("synthetic disk full")

    monkeypatch.setattr(store, "policy", failed)
    await runtime.observe("a", "s", [person()], 100, 100, stable=True)
    assert runtime.status()["error"] == "safety_storage_unavailable"


@pytest.mark.asyncio
async def test_two_process_output_lease_and_false_alarm_cancel(tmp_path):
    store = SafetyStore(tmp_path / "safety.sqlite")
    speaker = SimpleNamespace(say=AsyncMock(return_value=dict(status="accepted")))
    runtime = SafetyRuntime(store, speaker=speaker)
    event = store.create(dict(message="test"))
    assert store.claim_output("other-owner")
    await runtime.announce(event)
    assert store.event(event["id"])["announcement"]["status"] == "busy"
    speaker.say.assert_not_awaited()
    store.release_output("incorrect-owner")
    assert not SafetyStore(store.path).claim_output("another-owner")
    store.release_output("other-owner")
    store.update(event["id"], review="false_alarm")
    await runtime.announce(event)
    assert store.event(event["id"])["announcement"]["status"] == "cancelled"
    speaker.say.assert_not_awaited()


@pytest.mark.parametrize("target", ["joint", "box"])
@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), 1.1, -0.1, None, "0.9", True])
def test_invalid_confidence_never_establishes_fall(target, confidence):
    detector = FallDetector()
    events = []
    for i in range(25):
        now = 100 + i * 0.25
        p = person(i >= 7)
        evidence = p["joints"]["leftShoulder"] if target == "joint" else p["box"]
        evidence["confidence"] = confidence
        events += detector.observe("a", "s", [p], now, now, stable=True)
    assert events == []


@pytest.mark.asyncio
async def test_delayed_pipeline_cannot_name_expired_identity(tmp_path, monkeypatch):
    import homeplane.embodied.safety as module

    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 1800000000 + clock[0]))
    store = SafetyStore(tmp_path / "private/safety.sqlite")
    store.configure(SafetyPolicy(enabled=True))
    runtime = SafetyRuntime(store, labels=lambda: {"registered-person": "Example"})
    for i in range(25):
        source = 100 + i * 0.25
        clock[0] = source + 1.25
        identity = dict(state="tracked", person_id="registered-person",
                        observed_monotonic=source, face_observed_monotonic=source)
        await runtime.observe("a", "s", [person(i >= 7, identity)], source, source, stable=True)
    event = store.events()[0]
    assert event["person_id"] is None and event["person_name"] is None
    assert event["message"] == "有人疑似跌倒，请立即查看。"
    await runtime.close()
