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
    "fence", ["motion", "stale", "gap", "captured_gap", "replay", "session",
              "missing_joints", "missing_person", "bad_coordinate"]
)
def test_invalid_observation_breaks_fall_continuity(fence):
    detector = FallDetector()
    events = []
    for index in range(18):
        now = 100 + index * 0.25
        p, session, stable, captured = person(index >= 7), "a", True, now
        if fence == "captured_gap" and index >= 8:
            now = captured = now + 0.8
        if index == 8:
            if fence == "motion":
                stable = False
            if fence == "stale":
                captured = now - 2
            if fence == "gap":
                now += 3
            if fence == "replay":
                captured -= .25
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


def test_short_horizontal_pose_and_stationary_transition_do_not_trigger():
    for mode in ("brief", "stationary"):
        d = FallDetector()
        events = []
        for i in range(28):
            now = 100 + i * 0.25
            p = person(i >= 7 if mode == "stationary" else 7 <= i < 10)
            if mode == "stationary" and 7 <= i < 15:
                p = person()
                p["box"]["h"] = 0.3  # neither standing nor horizontal during descent
            events += d.observe("a", "s", [p], now, now, stable=True)
        assert events == []


def slide_person(progress, *, raised=False, feet=True):
    """从同一站位分步降低躯干并转向倒卧，纵向坐标随镜头比例变化。"""
    start, end = person(), person(True)
    if raised:
        end["box"]["y"] -= .18
        for point in end["joints"].values():
            point["y"] -= .18
    for part in start["joints"]:
        for axis in ("x", "y"):
            first, final = start["joints"][part][axis], end["joints"][part][axis]
            start["joints"][part][axis] = first + (final - first) * progress
    for axis in ("x", "y", "w", "h"):
        first, final = start["box"][axis], end["box"][axis]
        start["box"][axis] = first + (final - first) * progress
    if feet:
        for side, x in (("left", .44), ("right", .48)):
            start["joints"][side + "Ankle"] = dict(x=x + progress * .18, y=.64, confidence=.94)
    return start


def slide_episode(detector, *, scenario="slide", camera="a", start=100.0, room_aspect=16 / 9):
    results = []
    for index in range(40):
        captured = start + index * .25
        progress = max(0, min(1, (index - 6) / 16))
        if scenario == "sit_then_lie" and 13 <= index <= 17:
            progress = 7 / 16
        if scenario == "recovery" and 14 <= index <= 16:
            progress = 3 / 16
        if scenario == "sit":
            progress = min(.4, progress)
        raised = scenario in {"bed", "sofa"} or scenario == "rise_while_horizontal" and index >= 22
        actor = slide_person(progress, raised=raised, feet=scenario != "no_feet")
        if scenario == "weak_feet":
            actor["joints"]["leftAnkle"]["confidence"] = .52
        if scenario == "discontinuous" and index == 15:
            actor["box"]["x"] = 0
            actor["box"]["w"] = .07
        if scenario == "wrong_track" and index >= 15:
            actor["id"] = 8
        if scenario == "occluded" and index == 15:
            actor["joints"].pop("rightShoulder")
        if scenario == "changed_person":
            actor["identity"] = dict(state="matched", person_id="actor-a" if index < 15 else "actor-b",
                                     observed_monotonic=captured, face_observed_monotonic=captured)
        processed = captured + .3 if scenario == "jitter" and index >= 15 else captured
        events = detector.observe(camera, "next" if scenario == "changed_session" and index >= 15 else "s",
                                  [actor], processed, captured, stable=not (scenario == "motor" and index == 15),
                                  aspect=room_aspect, coalesced=scenario == "coalesced" and index == 15)
        results.extend((captured, event) for event in events)
    return results


@pytest.mark.parametrize("scenario,aspect", [("slide", 16/9), ("slide", 4/3), ("jitter", 16/9)])
def test_progressive_slide_reaches_one_suspected_fall_without_changing_rapid_timing(scenario, aspect):
    tracker = FallDetector()
    [(captured, event)] = slide_episode(tracker, scenario=scenario, room_aspect=aspect)
    assert event["kind"] == "fall" and event["status"] == "suspected"
    assert event["detector"] == "geometry-fall/4"
    assert event["basis"] == "upright_progressive_slide_sustained_low_horizontal"
    assert event["descent_mono"] - event["started_mono"] > FallDetector.RAPID_DESCENT_SECONDS
    assert captured - event["descent_mono"] == 2.0
    assert tracker.confirmed_down("a", "s", "7", started=event["started_mono"], evaluated=109.75, max_age=1.5)


@pytest.mark.parametrize("scenario", [
    "bed", "sofa", "sit", "sit_then_lie", "no_feet", "weak_feet", "discontinuous", "wrong_track",
    "occluded", "changed_person", "changed_session", "motor", "coalesced", "recovery", "rise_while_horizontal",
])
def test_slow_motion_without_continuous_floor_evidence_stays_unconfirmed(scenario):
    assert slide_episode(FallDetector(), scenario=scenario) == []


def test_queued_low_slide_holds_only_the_confirming_view():
    tracker = FallDetector()
    for index in range(29):
        captured = 100 + index * .25
        actor = slide_person(max(0, min(1, (index - 6) / 16)))
        candidate = ([actor], captured, 16 / 9, False)
        pending = tracker.stable_view_tracks("a", "s", captured, candidate)
        if pending:
            assert not tracker.stable_view_tracks("b", "s", captured, candidate)
            assert not tracker.stable_view_tracks("a", "other", captured, candidate)
            assert not tracker.stable_view_tracks("a", "s", captured + 1.6, candidate)
        tracker.observe("a", "s", [actor], captured, captured, stable=True)
        if pending:
            assert tracker.stable_view_tracks("a", "s", captured)
            break
    else:
        pytest.fail("完整的慢滑倒卧没有请求稳定视角")


@pytest.mark.parametrize("latency", ["growing", "jump"])
def test_fall_confirmation_uses_capture_intervals_under_processing_jitter(latency):
    detector = FallDetector()
    events = []
    for index in range(24):
        captured = 100 + index * .25
        delay = min(1.38, max(0, index-7)*.23) if latency == "growing" else (.6 if index >= 10 else 0)
        now = captured + delay
        events.extend((captured, event) for event in detector.observe(
            "a", "s", [person(index >= 7)], now, captured, stable=True))
    assert len(events) == 1
    confirmed_at, event = events[0]
    assert confirmed_at == 103.75
    assert event["started_mono"] == 101.5 and event["detector"] == "geometry-fall/3"


@pytest.mark.parametrize("missing", ["absent", "weak"])
def test_one_missing_joint_holds_same_track_without_counting_the_gap(missing):
    detector = FallDetector()
    confirmations = []
    for index in range(25):
        captured = 100 + index * .25
        actor = person(index >= 7)
        if index == 10:
            if missing == "absent":
                actor["joints"].pop("leftShoulder")
            else:
                actor["joints"]["leftShoulder"]["confidence"] = .4
            assert detector.stable_view_tracks("a", "s", captured, ([actor], captured, 16/9, False))
        events = detector.observe("a", "s", [actor], captured, captured, stable=True)
        confirmations.extend([captured]*len(events))
        if index == 10:
            assert detector.stable_view_tracks("a", "s", captured + .8)
            assert not detector.stable_view_tracks("a", "s", captured + 1.51)
    assert confirmations == [104.25]


@pytest.mark.parametrize("break_in_pose", ["too_long", "different_box", "reappears_elsewhere", "before_descent"])
def test_partial_pose_never_borrows_a_different_track_or_an_unseen_descent(break_in_pose):
    detector = FallDetector()
    events = []
    for index in range(25):
        captured = 100 + index * .25
        actor = person(index >= 7)
        if (index == (7 if break_in_pose == "before_descent" else 10)
                or break_in_pose == "too_long" and 11 <= index <= 12):
            actor["joints"].pop("leftShoulder")
        if (break_in_pose == "different_box" and index == 10
                or break_in_pose == "reappears_elsewhere" and index == 11):
            actor["box"]["x"] = 0
            actor["box"]["w"] = .23
        events.extend(detector.observe("a", "s", [actor], captured, captured, stable=True))
    assert not events


def occlusion_observations(fence=None, *, down_gap=False):
    tracking = dict(backend="ByteTrack", space="image", session_id=True)
    times = [100, 100.25, 100.375, 100.5, 100.75, 101, 101.25, 101.5]
    if fence == "two_missing":
        times.append(101.625)
    times += [101.75 + i * .25 for i in range(12)]
    for captured in times:
        if captured == 100 and fence == "too_little_upright":
            continue
        down = captured >= 101.75
        actor = person(down)
        actor["tracking"] = tracking.copy()
        actor["box"].update(
            dict(x=.33, y=.48, w=.35, h=.22) if down else dict(y=.20, h=.56)
        )
        if down and fence == "unproven":
            actor["tracking"].clear()
        if down and fence == "different_space":
            actor["tracking"]["space"] = "background_reference"
        if down and fence == "different_track":
            actor["id"] = 8
        if down and fence == "disjoint_box":
            actor["box"].update(x=.66, w=.33)
        missing = captured in {100.375, 101.5, 101.625} or down_gap and captured == 102.5
        people = [] if missing else [actor]
        if down and fence == "another_person":
            people.append(dict(person(), id=12))
        yield captured, people, "other" if down and fence == "source_changed" else "s", (
            captured == 101.75 and fence == "coalesced"
        )


@pytest.mark.parametrize("down_gap, confirmation", [(False, 103.75), (True, 104.25)])
def test_one_missed_track_frame_retains_only_observed_posture_time(down_gap, confirmation):
    detector = FallDetector()
    confirmations = []
    for captured, people, session, coalesced in occlusion_observations(down_gap=down_gap):
        if captured == 101.75:
            assert detector.stable_view_tracks("a", session, captured, (people, captured, 16/9, coalesced))
        found = detector.observe("a", session, people, captured, captured, stable=True, coalesced=coalesced)
        confirmations.extend((event["started_mono"], event["descent_mono"], captured) for event in found)
    assert confirmations == [(101.25, 101.75, confirmation)]


@pytest.mark.parametrize("fence", ["two_missing", "unproven", "different_space", "different_track",
                                 "disjoint_box", "another_person", "source_changed", "coalesced",
                                 "too_little_upright"])
def test_missed_track_frame_cannot_substitute_for_upright_time_or_continuity(fence):
    detector = FallDetector()
    for captured, people, session, coalesced in occlusion_observations(fence):
        if captured == 101.75:
            assert not detector.stable_view_tracks("a", session, captured, (people, captured, 16/9, coalesced))
        assert not detector.observe("a", session, people, captured, captured, stable=True, coalesced=coalesced)


def test_consecutive_misses_during_down_confirmation_end_original_episode():
    detector = FallDetector()
    for captured, people, session, coalesced in occlusion_observations(down_gap=True):
        if captured == 102.75:
            assert not detector.observe("a", session, [], 102.625, 102.625, stable=True)
            assert not detector.stable_view_tracks("a", session, captured)
        assert not detector.observe("a", session, people, captured, captured, stable=True, coalesced=coalesced)


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
async def test_runtime_persists_event_before_named_speech_and_keeps_evidence_local(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    store.configure(SafetyPolicy(enabled=True, announce=True))
    clock = [100.0]
    monkeypatch.setattr("homeplane.embodied.safety.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("homeplane.embodied.safety.time.time", lambda: 1800000000 + clock[0])

    async def say(text, *, current, revalidate):
        assert current() is True and await revalidate() is True
        assert store.events()[0]["person_name"] == "Example Person"
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
    await runtime.drain()
    await asyncio.gather(*runtime.tasks)
    event = store.events()[0]
    assert event["announcement"] == dict(status="accepted", physical_audio_confirmed=False)
    assert not event["clip_available"] and event["simulated"] is False
    assert 8 < len(event["frames"]) <= 22
    assert store.frame(event["id"], event["frames"][0]["ordinal"]) == b"local-jpeg"
    speaker.say.assert_awaited_once()
    await runtime.close()


@pytest.mark.asyncio
async def test_live_fall_queue_keeps_tracking_source_through_one_missed_frame(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    store.configure(SafetyPolicy(enabled=True))
    clock = [100.0]
    monkeypatch.setattr("homeplane.embodied.safety.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("homeplane.embodied.safety.time.time", lambda: 1800000000 + clock[0])
    runtime = SafetyRuntime(store)
    try:
        for captured, people, session, _ in occlusion_observations(down_gap=True):
            clock[0] = captured
            await runtime.sync_policy()
            assert runtime.offer("a", session, people, captured, captured, stable=True, jpeg=b"synthetic")
            for actor in people:
                actor["tracking"].clear()
            await runtime.drain()
        saved, = store.events()
        assert saved["kind"] == "fall" and saved["status"] == "suspected"
        assert saved["track"] == "7" and saved["captured_at"] == 1800000104.25
        assert saved["announcement"]["status"] == "not_requested"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_fall_observation_quality_tracks_geometry_and_recovery_for_the_owned_capture(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    store.configure(SafetyPolicy(enabled=True))
    clock = [100.]
    monkeypatch.setattr("homeplane.embodied.safety.time.monotonic", lambda: clock[0])
    runtime = SafetyRuntime(store)
    camera, stream, context = "camera", "a" * 32, "b" * 64
    complete = person()
    incomplete = dict(person(), id=8)
    del incomplete["joints"]["rightShoulder"]

    def quality():
        return runtime.status(include_fall_inputs=True)["fall_inputs"][0]

    async def feed(at, people):
        clock[0] = at
        await runtime.sync_policy()
        runtime.capture_frame(camera, stream, context, at)
        assert runtime.offer(camera, "view", people, at, at, stable=True,
                             source_context=context, capture_session=stream)
        await runtime.drain()
        return quality()

    try:
        await runtime.sync_policy()
        runtime.capture_frame(camera, stream, context, 100.)
        assert quality()["state"] == "waiting"
        assert "fall_inputs" not in runtime.status()
        assert (await feed(100., []))["state"] == "no_person_reported"
        assert (await feed(100.25, [complete]))["state"] == "pose_available"
        mixed = await feed(100.5, [complete, incomplete])
        assert (mixed["state"], mixed["reported_people"], mixed["usable_poses"]) == ("pose_limited", 2, 1)
        limited = await feed(100.75, [incomplete])
        assert limited["state"] == "pose_limited" and limited["usable_poses"] == 0
        assert limited["pose_gap_ms"] == limited["last_pose_age_ms"] == 250

        clock[0] = 102.
        runtime.capture_frame(camera, stream, context, 102.)
        gap = quality()
        assert gap["state"] == "observation_gap" and gap["capture_age_ms"] == 0
        assert gap["observed_age_ms"] == 1250 and gap["observation_gap_ms"] == 500
        assert gap["reported_people"] is None and gap["usable_poses"] is None
        runtime.clear_view(camera)
        assert quality()["last_pose_age_ms"] is None
        recovered = await feed(102.25, [])
        assert recovered["state"] == "no_person_reported" and recovered["longest_gap_ms"] == 750
        assert recovered["pose_gap_ms"] is None
        people = [dict(complete, id=i) for i in range(1, 12)]
        crowded = await feed(102.5, people)
        assert (crowded["state"], crowded["reported_people"], crowded["usable_poses"]) == ("pose_limited", 11, 10)

        clock[0] = 102.75
        runtime.capture_frame(camera, "c" * 32, "d" * 64, 102.75)
        assert quality()["state"] == "waiting" and quality()["longest_gap_ms"] == 0
        assert not runtime.offer(camera, "view", [complete], 102.75, 102.75, stable=True,
                                 source_context=context, capture_session=stream)
        assert not store.events()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_api_fall_quality_follows_current_source_privacy_and_policy_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(safety_api, "PRIVATE", tmp_path)
    facing = dict(id="cam_1111111111", name="合成客厅", source_context="a" * 64)
    shutter = dict(id="cam_2222222222", name="合成主卧", source_context="b" * 64)
    replaced = dict(id="cam_3333333333", name="合成书房", source_context="c" * 64)
    sources = [facing, shutter, replaced]
    monkeypatch.setattr(safety_api, "configured_camera_sources", lambda: sources)
    router = APIRouter()
    service = SimpleNamespace(process=SimpleNamespace(returncode=None))
    runtime = safety_api.install(router, service)
    policy = runtime.store.configure(SafetyPolicy(enabled=True))
    clock = [100.]
    monkeypatch.setattr("homeplane.embodied.safety.time.monotonic", lambda: clock[0])
    stream = "d" * 32
    revision = policy.revision

    async def request(path):
        assert path == "/api/safety/status"
        return dict(runtime.status(include_fall_inputs=True), fall_policy_revision=revision,
                    closed_sources={shutter["id"]: shutter["source_context"]})

    service.request = request
    app = FastAPI()
    app.include_router(router)
    try:
        await runtime.sync_policy()
        for source, context in ((facing, facing["source_context"]), (replaced, "e" * 64)):
            runtime.capture_frame(source["id"], stream, context, 100.)
            assert runtime.offer(source["id"], "view", [person()], 100., 100., stable=True,
                                 source_context=context, capture_session=stream)
        await runtime.drain()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.get("/api/embodied/safety")
            assert response.status_code == 200
            report = response.json()
            good, closed, waiting = report["fall_observations"]
            assert good["state"] == "pose_available" and good["usable_poses"] == 1
            assert closed == dict(camera=shutter["id"], name=shutter["name"], state="shutter_closed")
            assert waiting == dict(camera=replaced["id"], name=replaced["name"], state="waiting")
            assert all(key not in response.text for key in ("source_context", "fall_policy_revision", "closed_sources"))
            revision -= 1
            report = (await client.get("/api/embodied/safety")).json()
            assert [row["state"] for row in report["fall_observations"]] == [
                "unavailable", "shutter_closed", "unavailable"]
            runtime.store.configure(policy.model_copy(update={"enabled": False}))
            report = (await client.get("/api/embodied/safety")).json()
            assert [row["state"] for row in report["fall_observations"]] == ["paused", "shutter_closed", "paused"]
        assert not runtime.store.events()
    finally:
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
    await runtime.close()


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
        assert all(c["status"] == "unavailable" for c in report["capabilities"]
                   if c["id"] in {"crying", "infant_face_covered", "rough_handling"})
        assert report["visual_review"]["enabled"] is False
        assert runtime.visual.engine.process is None
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
async def test_sound_search_is_separately_opted_in_revision_bound_and_legacy_policy_readable(tmp_path, monkeypatch):
    from homeplane.embodied.safety import SafetyControls

    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router = APIRouter()
    runtime = safety_api.install(router, SimpleNamespace(process=None))
    app = FastAPI()
    app.include_router(router)
    base = '/api/embodied/safety'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            assert (await client.get(base)).json()['sound_search'] is False
            denied = await client.post(base+'/sound-search', json=dict(enabled=True, expected_revision=0))
            assert denied.status_code == 409 and denied.json()['detail'] == 'sound_search_unavailable'
            policy = (await client.post(base+'/policy', json=dict(enabled=True, sound_review=True, revision=0))).json()
            assert (await client.get(base)).json()['sound_search'] is False
            selection = dict(enabled=True, expected_revision=policy['revision'])
            saved = (await client.post(base+'/sound-search', json=selection)).json()
            assert saved == dict(enabled=True, revision=policy['revision']+1)
            assert (await client.post(base+'/sound-search', json=selection)).status_code == 409
            status = (await client.get(base)).json()
            assert status['sound_search'] is True and status['policy']['revision'] == saved['revision']
            with runtime.store.connect() as db:
                old_schema = db.execute('SELECT value FROM safety_policy WHERE id=1').fetchone()[0]
                assert SafetyControls.model_validate_json(old_schema).sound_review is True
            disabled = dict(status['policy'], sound_review=False)
            assert (await client.post(base+'/policy', json=disabled)).status_code == 200
            assert (await client.get(base)).json()['sound_search'] is False
    finally:
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
    await runtime.drain()
    assert runtime.post_frames["a"] and store.events()
    count = len(store.events()[0]["frames"])
    store.configure(policy.model_copy(update={"enabled": False}))
    clock[0] += 0.5
    await runtime.observe("a", "s", [person(True)], clock[0], clock[0], stable=True, jpeg=b"new-private")
    assert not runtime.frames and not runtime.post_frames
    assert len(store.events()[0]["frames"]) == count
    await runtime.close()


@pytest.mark.asyncio
async def test_storage_failure_does_not_crash_camera_observer(tmp_path, monkeypatch):
    store = SafetyStore(tmp_path / "safety.sqlite")
    runtime = SafetyRuntime(store)

    def failed():
        raise OSError("synthetic disk full")

    monkeypatch.setattr(store, "read_policy", failed)
    await runtime.observe("a", "s", [person()], 100, 100, stable=True)
    assert runtime.status()["error"] == "safety_storage_unavailable"
    await runtime.close()


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
    await runtime.close()


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
