"""以合成房间和受控云台读回验证覆盖、发送和回位后的确认。"""

import asyncio
import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest

from homeplane.embodied.camera_director import CameraDirector, visible_scene
from homeplane.embodied.camera_view_reference import REFERENCE_FILE, save_reference
from homeplane.embodied.camera_view_recovery import ViewRecall
from homeplane.embodied.ptz_control import CameraLease, FLAGS
from homeplane.embodied.room_session import RoomSession
from tests.embodied.test_motor_recovery import primary_scope
from tests.embodied.test_review_motion import source_function


def encode(pixels):
    return cv2.imencode(".jpg", pixels)[1].tobytes()


def scene():
    image = np.full((360, 640), 118, np.uint8)
    for x, y, light, size in np.random.default_rng(748).integers(
            low=[10, 10, 25, 3], high=[630, 350, 230, 13], size=(440, 4)):
        cv2.circle(image, (int(x), int(y)), int(size), int(light), -1)
    return image


@pytest.mark.asyncio
async def test_native_room_child_distinguishes_textured_crop_and_recovers_real_room_overlap(tmp_path):
    whole = scene()
    # 当前画面保留足够纹理，却只覆盖参考画面的中央四分之一。
    crop = encode(cv2.resize(whole[90:270, 160:480], (640, 360)))
    wall = encode(np.full_like(whole, 130))
    partly_masked = np.roll(whole, (86, 225), axis=(0, 1))
    partly_masked[:134] = whole[:134]
    dark = encode(np.uint8(whole.astype(float) * .64))
    original = encode(whole)
    assert visible_scene(crop) and visible_scene(original) and not visible_scene(wall)
    revision = save_reference(tmp_path, 7, original)
    path = tmp_path / REFERENCE_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    anchor = path.read_bytes()
    assert b'\xff\xd8' not in anchor and set(json.loads(anchor)) == {
        'version', 'revision', 'preset', 'points', 'descriptors'}
    room = RoomSession(tmp_path)
    at = 100.
    try:
        async def feed(picture):
            nonlocal at
            for _ in range(4):
                at += .8
                assert await room.register(picture, [], at)
            evidence = room.room_coverage
            assert evidence["revision"] == revision and evidence["preset"] == 7
            return evidence

        assert (await feed(original))["state"] == "covered"
        assert (await feed(dark))["overlap"] == pytest.approx(1, abs=.02)
        partial = await feed(crop)
        assert partial["state"] == "partial" and partial["overlap"] == pytest.approx(.25, abs=.08)
        assert room.scene_visible is True
        assert (await feed(encode(partly_masked)))["state"] != "covered"
        assert (await feed(wall))["state"] == "unknown"
        room.expect_motion(at, 2)
        assert room.room_coverage is None
        assert await room.register(original, [], at + .8)
        assert room.room_coverage is None
        at += 2
        assert (await feed(original))["state"] == "covered"
        assert path.read_bytes() == anchor
        path.unlink()
        for _ in range(2):
            at += .8
            assert await room.register(original, [], at)
        assert room.room_coverage is None
    finally:
        assert await room.close()


def sample(at, *, quality="partial", session="primary-room", revision="e" * 24, observed=None, **updates):
    value = dict(status="live", _local_received=at, session_id=session, tracking_current=True,
                 scene_visible=True, people=[], room_coverage=dict(at=at if observed is None else observed,
                 revision=revision, preset=7, state=quality, overlap=1 if quality == "covered" else .25))
    value.update(updates)
    return value


def ready_recall(director, *, end=14., quality="partial"):
    director.apply_mode("auto", "original")
    for at in np.arange(end - 3, end, .6).tolist():
        assert director.choose(sample(at, quality=quality), at) is None
    request = director.choose(sample(end, quality=quality), end)
    assert isinstance(request, ViewRecall)
    return request


def test_camera_uses_new_matching_frames_to_confirm_recall_and_preserves_recent_view():
    director = CameraDirector(presets=[dict(idx=7)])
    request = ready_recall(director)
    assert director.decision_current(sample(14.1), 14.1, request)
    director.reference_dispatched(14.1)
    assert director.public()["room_view"] == "verifying"
    for at, seen in [(15., 14.), (16.2, 15.5), (16.8, 16.7), (17.2, 16.7)]:
        assert director.choose(sample(at, quality="covered", observed=seen), at) is None
        assert director.public()["room_view"] == "verifying"
    director.choose(sample(17.4, quality="covered", observed=17.3), 17.4)
    assert director.public()["room_view"] == "covered"
    assert director.view_recovery.holding(36)


def test_view_recall_drops_stale_sources_and_yields_to_current_intent():
    director = CameraDirector(presets=[dict(idx=7)])
    request = ready_recall(director, quality="unknown")
    at = 14.1
    for row, context in [
        (sample(at, observed=12.), {}),
        (sample(at, session="replacement"), {}),
        (sample(at, revision="f" * 24), {}),
        (sample(at, people=[{"id": 1}]), {}),
        (sample(at, game={"status": "playing"}), {}),
        (sample(at, tracking_current=False), {}),
        (sample(at), {"fall_pending": True}),
        (sample(at), {"sound_cue": object()}),
        (sample(at), {"review_pending": True}),
    ]:
        assert not director.decision_current(row, at, request, **context)
    director.apply_mode("hold", "new")
    assert not director.decision_current(sample(at), at, request)
    assert director.choose(sample(at), at) is None


def test_reference_from_an_unavailable_preset_does_not_change_other_camera_behavior():
    director = CameraDirector(presets=[dict(idx=2)])
    director.apply_mode("auto", "original")
    for at in range(11, 16):
        assert director.choose(sample(at, quality="partial"), at) is None
    assert director.public()["room_view"] is None
    assert director.public()["view_usable"]


def test_preset_ack_without_room_coverage_keeps_state_unconfirmed_and_backs_off():
    director = CameraDirector(presets=[dict(idx=7)])
    ready_recall(director)
    director.reference_dispatched(14.)
    assert director.choose(sample(22.1), 22.1) is None
    assert director.public()["room_view"] == "unconfirmed"
    assert not director.public()["view_usable"]
    for at in np.arange(22.6, 44, .6).tolist():
        assert director.choose(sample(at), at) is None
    assert isinstance(director.choose(sample(44.), 44.), ViewRecall)
    director.reference_dispatched(44.)
    for at in np.arange(46.1, 90, .6).tolist():
        assert director.choose(sample(at), at) is None
    assert director.public()["room_view"] == "unconfirmed"


class PresetDevice:
    def __init__(self, change):
        self.values = dict.fromkeys(FLAGS, False)
        self.values.update(camera_power=True, presets=[dict(idx=1, name="start"), dict(idx=7, name="room")],
                           active_preset=1)
        self.change = change
        self.writes = []

    async def read(self):
        return dict(self.values)

    async def write(self, name, value, *, before_send=None):
        if before_send is not None:
            self.change()
            before_send()
        self.writes.append((name, value))
        self.values[name] = value


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["primary", "secondary"])
@pytest.mark.parametrize("interruption", [None, "hold", "person", "privacy", "presets_deleted"])
async def test_actual_cloud_dispatch_rechecks_current_intent_and_never_counts_device_ack_as_view(
        tmp_path, owner, interruption):
    director = CameraDirector(presets=[dict(idx=7)])
    request = ready_recall(director)
    current = sample(14.)
    closed = Mock()
    motion = Mock()

    def changed():
        if interruption == "hold":
            director.apply_mode("hold", "changed")
        elif interruption == "person":
            current["people"] = [{"id": 1}]

    device = PresetDevice(changed)
    lease = CameraLease(device, tmp_path / "ptz-lease.json")
    await lease.acquire()
    if interruption == "privacy":
        device.values["camera_power"] = False
    if interruption == "presets_deleted":
        device.values["presets"] = [dict(idx=1, name="start")]
    clock = SimpleNamespace(monotonic=lambda: 14.1, time=lambda: 14.1)

    if owner == "primary":
        scope, commands = primary_scope()
        scope.update(time=clock, state=current, last_frame=14., camera_lease=lease, ViewRecall=ViewRecall,
                     close_primary_view=closed, room=SimpleNamespace(expect_motion=motion))
        move = scope["motor_step"](request, 14., expected_intent="original",
            decision_current=lambda: director.decision_current(current, 14.1, request),
            on_reference_sent=director.reference_dispatched)
    else:
        pipe = Mock()
        commands = pipe.write.call_args_list
        scope = dict(asyncio=asyncio, json=json, time=clock, secrets=secrets, waiters={},
                     ViewRecall=ViewRecall, motion_lock=asyncio.Lock(), lease=lease,
                     current_sample=lambda: current, camera_is_open=AsyncMock(return_value=True),
                     privacy_closed=closed, process=SimpleNamespace(returncode=None, stdin=pipe),
                     selected={"id": "synthetic"}, director=director, patrol=None, recognizer=None,
                     item=dict(source_active=True, privacy_closed=False, control_epoch=1),
                     self=SimpleNamespace(focus=Mock(return_value=None), on_motion=motion,
                        occupied_elsewhere=Mock(return_value=False), review_pending=Mock(return_value=False),
                        fall_pending=Mock(return_value=False), sound_cue=Mock(return_value=None),
                        runtime_log=SimpleNamespace(record=Mock())))
        send = source_function("homeplane/embodied/previews.py", "send_motion", scope)
        move = send(request, 14., 1)
    try:
        if interruption:
            with pytest.raises((RuntimeError, ValueError)):
                await move
            assert not device.writes and not motion.called
            assert closed.called == (interruption == "privacy")
        else:
            assert await move == {"accepted": True}
            assert device.writes == [("active_preset", 7)]
            assert motion.called and not closed.called
            assert director.public()["room_view"] == "verifying"
        assert not commands and not scope.get("motor_waiters", scope.get("waiters"))
    finally:
        lease.unclaim()
