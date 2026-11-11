"""Actual private Room process and immutable editor frames; synthetic input only."""

import asyncio
import base64
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest

from homeplane.embodied.calibration_frames import CalibrationFrames
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.room_session import RoomSession


def room_port(*, registration=None, layout=None):
    return SimpleNamespace(
        command_until=0,
        registration=registration,
        reg_info={"status": "locked"},
        register=AsyncMock(return_value=True),
        update=AsyncMock(return_value=layout or {"registration": {"status": "locked"}}),
        scene_visible=True,
        draft=None,
        status=lambda: dict(state="ready"),
        retire=Mock(),
        close=AsyncMock(return_value=True),
        expect_motion=Mock(),
        reset_seats=Mock(),
    )


def test_viewer_entry_asset_version_changes_with_bytes_not_only_the_filename(tmp_path):
    from homeplane.embodied.static_page import static_page

    path = tmp_path / "index.html"
    path.write_text('<link href="/assets/index.css"><script src="/assets/index.js"></script>')
    css, script = tmp_path / "index.css", tmp_path / "index.js"
    css.write_text("original")
    script.write_text("unchanged")
    first = static_page(path)
    css.write_text("updated")
    second = static_page(path)
    assert first.split('"')[1] != second.split('"')[1]
    assert first.split('"')[3] == second.split('"')[3]


def test_static_assets_revalidate_but_reuse_unchanged_bytes(tmp_path):
    from starlette.datastructures import Headers

    from homeplane.embodied.static_page import static_asset

    path = tmp_path / "index.css"
    path.write_text("first")
    first = static_asset(path, Headers())
    assert first.headers["cache-control"] == "no-cache"
    headers = Headers({"if-none-match": first.headers["etag"]})
    assert static_asset(path, headers).status_code == 304
    path.write_text("second")
    assert static_asset(path, headers).status_code == 200


@pytest.fixture
def image():
    pixels = np.random.default_rng(13).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    return cv2.imencode(".jpg", pixels)[1].tobytes()


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("calibrated", [False, True])
async def test_real_room_child_preserves_registration_draft_and_unconfigured_layout(
    tmp_path, image, calibrated
):
    from homeplane.embodied.calibration_store import CalibrationStore, manual_config
    from homeplane.embodied.room_engine import DEFAULT, calibration_draft

    if calibrated:
        config = manual_config(dict(calibration_draft(DEFAULT), mode="relative_calibrated"))
        CalibrationStore(tmp_path).save(config, image)
    files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    session = RoomSession(tmp_path)
    try:
        now = time.monotonic()
        assert await session.register(image, [], now)
        assert session.reg_info["status"] == "locked" and np.allclose(
            session.registration, np.eye(3), atol=0.001
        )
        people = [dict(id=1, box=dict(x=0.3, y=0.1, w=0.2, h=0.7), joints={})]
        hands = []
        layout = await session.update(image, people, hands, now, 640, 360, [])
        assert (
            layout["mode"] == ("relative_calibrated" if calibrated else "unconfigured")
            and layout["outline"] == []
        )
        assert people[0]["position"] is None and people[0]["seat"] is None
        assert session.draft is not None and len(session.draft["anchors"]) == 4
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == files, (
            "Ordinary registration must not persist or overwrite a reference"
        )
        process = session.owner.process
    finally:
        assert await session.close()
    assert process.returncode is not None and not session.owner.slot


@pytest.mark.asyncio
async def test_real_room_child_confirms_patrol_with_two_fresh_translated_views(tmp_path, image):
    session = RoomSession(tmp_path)
    director = CameraDirector()
    director.apply_mode("survey", "synthetic-intent")
    now = time.monotonic()
    pixels = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    moved = cv2.warpAffine(pixels, np.float32([[1, 0, -32], [0, 1, 0]]), (640, 360))
    new_view = cv2.imencode(".jpg", moved)[1].tobytes()

    async def observe(at, picture):
        assert await session.register(picture, [], now + at)
        return director.choose(dict(status="live", session_id="synthetic-capture", people=[],
            _local_received=now + at, scene_visible=session.scene_visible,
            _patrol_view=session.patrol_view), now + at)

    try:
        assert await observe(0, image) is None
        assert await observe(.5, image) is None
        assert await observe(16, image) == "right"
        before = session.patrol_view
        session.expect_motion(now + 16.1, 2)
        director.sent(now + 16.2)
        assert session.patrol_view is None
        assert await observe(16.5, new_view) is None and session.patrol_view is None
        assert await observe(18.5, new_view) is None
        assert session.patrol_view[0] == before[0]
        assert session.patrol_view[1] != before[1]
        assert director.survey_confirmed_moves == 0
        assert await observe(19, new_view) is None
        assert director.survey_confirmed_moves == 1 and director.position == (1, 0)
        dx, dy = -32, 0
        for step, (direction, shift_x, shift_y) in enumerate([
            ('right', -32, 0), ('down', 0, -18),
            ('left', 32, 0), ('left', 32, 0), ('left', 32, 0), ('left', 32, 0),
            ('up', 0, 18), ('right', -32, 0), ('right', -32, 0),
        ], 1):
            at = 16 + 12.5 * step
            assert await observe(at, new_view) == direction
            session.expect_motion(now + at + .1, 2)
            director.sent(now + at + .2)
            dx, dy = dx + shift_x, dy + shift_y
            moved = cv2.warpAffine(pixels, np.float32([[1, 0, dx], [0, 1, dy]]), (640, 360))
            new_view = cv2.imencode('.jpg', moved)[1].tobytes()
            assert await observe(at + 2.5, new_view) is None
            assert director.survey_confirmed_moves == step
            assert await observe(at + 3, new_view) is None
            assert director.survey_confirmed_moves == step + 1
        assert director.public()['survey_result'] == 'complete' and director.position == (0, 0)
        assert director.public()["physical_motion_verified"] is False
        assert not list(tmp_path.iterdir())
    finally:
        assert await session.close()
    assert session.patrol_view is None


CHILD = r"""
import ctypes,signal,sys
from types import SimpleNamespace
from pathlib import Path
from homeplane.embodied.room_worker import serve
from homeplane.embodied.room_protocol import unavailable_layout
mode=sys.argv[2]
def stall():
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    ctypes.PyDLL(None).sleep(60)
class Room:
    def __init__(self,*_):
        if mode=='open':stall()
        if mode=='slow_open':ctypes.PyDLL(None).usleep(250000)
        self.players={};self.pending={};self.holds={};self.next_seat=1;self.command_until=0
        self.reg_info={'status':'locked'}
        self.registration=SimpleNamespace(tolist=lambda:[[1,0,0],[0,1,0],[0,0,1]])
    def expect_motion(self,now,seconds):self.command_until=now+seconds
    def register(self,*_):
        if mode=='register':stall()
    def update(self,*_):
        if mode=='layout':stall()
        return dict(unavailable_layout(),registration={'status':'locked'})
def visible(_):
    if mode=='visible':stall()
    return True
serve(sys.stdin.buffer,sys.stdout.buffer,Path(sys.argv[1]),factory=Room,visibility=visible,draft_factory=lambda _:None)
"""


def fake(folder, mode="ok", **kwargs):
    return RoomSession(folder, command=(sys.executable, "-B", "-c", CHILD, str(folder), mode), **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["open", "register", "visible", "layout"])
async def test_native_room_stalls_are_actually_killed_and_reaped(tmp_path, phase):
    session = fake(tmp_path, phase, startup_timeout=0.2, timeout=0.12)
    job = asyncio.create_task(session.register(b"SYNTHETIC", [], time.monotonic()))
    try:
        await until(lambda: session.owner is not None and session.owner.process is not None)
        process = session.owner.process
        registered = await job
        if phase == "layout":
            assert registered
            assert await session.update(b"SYNTHETIC", [], [], time.monotonic(), 640, 360, []) is None
        else:
            assert not registered
        assert session.reason == "room_timeout"
    finally:
        assert await session.close()
    assert process.returncode is not None and not session.owner.slot


@pytest.mark.asyncio
async def test_retired_frame_cannot_mutate_people_or_publish_a_draft(tmp_path):
    session = fake(tmp_path, "layout", timeout=20)
    current = [True]
    assert await session.register(b"SYNTHETIC", [], time.monotonic())
    people = [dict(id=1, joints={}, box=dict(x=0.1, y=0.1, w=0.1, h=0.5), position=None)]
    original = [dict(people[0])]
    job = asyncio.create_task(
        session.update(b"SYNTHETIC", people, [], time.monotonic(), 640, 360, [], current=lambda: current[0])
    )
    try:
        await asyncio.sleep(0.04)
        current[0] = False
        assert await asyncio.wait_for(job, 0.3) is None
        assert people == original and session.draft is None
    finally:
        assert await session.close()


@pytest.mark.asyncio
async def test_retiring_the_native_job_does_not_cancel_its_camera_owner(tmp_path):
    session = fake(tmp_path, "register", timeout=20)
    job = asyncio.create_task(session.register(b"SYNTHETIC", [], time.monotonic()))
    try:
        await until(lambda: session.ready)
        session.retire()
        assert await asyncio.wait_for(job, 0.5) is False
        assert await session.close()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_cancelled_room_spawn_and_repeated_close_keep_real_child_ownership(tmp_path, monkeypatch):
    import homeplane.embodied.local_child as module

    original = module.asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", delayed)
    session = fake(tmp_path)
    job = asyncio.create_task(session.register(b"SYNTHETIC", [], time.monotonic()))
    try:
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert session.owner.slot
        release.set()
        assert await session.close()
        assert len(processes) == 1 and processes[0].returncode is not None
    finally:
        release.set()
        await session.close()


def snapshot():
    clock = [10.0]
    frames = CalibrationFrames(clock=lambda: clock[0], wall=lambda: 1000.0 + clock[0] - 10)
    source = dict(worker="synthetic", generation=1, motion_generation=2)
    return frames, clock, source


def test_editor_freezes_one_published_image_draft_and_meta_while_new_frames_advance():
    frames, clock, source = snapshot()
    draft = dict(anchors=[dict(image=[0.1, 0.2])], seats=[])
    meta = dict(width=640, height=360, room={"registration": {"status": "locked"}})
    frames.publish(draft, b"FIRST", meta, source, 1000.0)
    frozen = frames.open(source)
    draft["anchors"][0]["image"][0] = 0.9
    meta["width"] = 1
    frames.publish(dict(anchors=[], seats=[]), b"SECOND", {}, source, 1000.0)
    assert frozen["_reference"]["jpeg"] == base64.b64encode(b"FIRST").decode()
    assert frozen["_reference"]["meta"]["width"] == 640 and frozen["anchors"][0]["image"] == [0.1, 0.2]
    assert frames.resolve(frozen["_source"], b"FIRST", source) == b"FIRST"
    with pytest.raises(ValueError):
        frames.resolve(frozen["_source"], b"SECOND", source)


@pytest.mark.parametrize("change", ["motion", "generation", "expired", "evicted", "clear", "reference"])
def test_editor_lease_rejects_retired_or_different_references(change):
    frames, clock, source = snapshot()
    frames.publish({}, b"FRAME", {}, source, 1000.0)
    frozen = frames.open(source)
    if change == "motion":
        source = dict(source, motion_generation=3)
    if change == "generation":
        source = dict(source, generation=2)
    if change == "expired":
        clock[0] += 901
    if change == "evicted":
        for _ in range(4):
            frames.open(source)
    if change == "clear":
        frames.clear()
    reference = b"OTHER" if change == "reference" else b"FRAME"
    with pytest.raises(ValueError):
        frames.resolve(frozen["_source"], reference, source)


def test_editor_cannot_open_a_stale_or_unregistered_published_frame():
    frames, clock, source = snapshot()
    frames.publish(None, b"FRAME", {}, source, 1000.0)
    with pytest.raises(ValueError):
        frames.open(source)
    frames.publish({}, b"FRAME", {}, source, 1000.0)
    clock[0] += 2
    with pytest.raises(ValueError):
        frames.open(source)


@pytest.mark.asyncio
async def test_room_warmup_waits_for_a_fresh_frame_without_restarting_the_child(tmp_path):
    session = fake(tmp_path, "slow_open")
    began = time.monotonic()
    try:
        assert not await session.register(
            b"SYNTHETIC", [], began, current=lambda: time.monotonic() - began < 0.05, active=lambda: True
        )
        process = session.owner.process
        assert session.ready and process.returncode is None
        assert await session.register(b"FRESH", [], time.monotonic())
        assert session.owner.process is process
    finally:
        assert await session.close()


@pytest.mark.asyncio
async def test_actual_primary_keeps_video_but_retires_game_input_when_room_fails(tmp_path):
    from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope
    from tests.embodied.test_game_source_recovery import partial_game

    scope, published = primary_frame_scope(tmp_path)
    scope["games"] = partial_game("ski")
    distance = scope["games"].distance
    scope["room"].register = AsyncMock(return_value=False)
    scope["room"].scene_visible = False
    scope["tracks"].invalidate.side_effect = lambda: setattr(scope["tracks"], "input_valid", False)
    await frame_continuation(scope)()
    assert published == ["live"] and scope["state"]["frame"] == scope["item"]["jpeg"]
    assert scope["state"]["room"]["registration"]["status"] == "unavailable"
    assert scope["state"]["people"] == [] and scope["state"]["hands"] == []
    assert scope["games"].distance == distance and scope["games"].source_resume_required
    assert scope["state"]["game"]["status"] == "paused"
