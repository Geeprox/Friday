"""Observed image geometry is usable without inventing a household floor plan."""

import ast
import asyncio
import base64
import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest
from tests.embodied.test_camera_audio import actual

from homeplane.embodied import camera_recognition
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.room_engine import DEFAULT, Room, calibration_draft, load_room
from tests.embodied.test_motion_games import human
from tests.embodied.test_tracking_continuity import frame


@pytest.fixture
def jpeg():
    return cv2.imencode(".jpg", np.zeros((360, 640, 3), np.uint8))[1].tobytes()


def observed_room(jpeg, config=None):
    room = Room(copy.deepcopy(DEFAULT if config is None else config), jpeg)
    room.registration = np.eye(3)
    room.reg_info = {"status": "locked"}
    return room


def person(track=1, shift=0, arms="down"):
    value = human(track, arms=arms)
    for point in value["joints"].values():
        point["x"] += shift
    value["joints"]["root"] = dict(x=0.5 + shift, y=0.55, confidence=0.9)
    value["box"] = dict(x=0.35 + shift, y=0.2, w=0.3, h=0.75, confidence=0.9)
    return value


def test_default_contains_no_household_geometry():
    assert DEFAULT["mode"] == "unconfigured"
    assert DEFAULT["camera"] is None
    assert all(DEFAULT[key] == [] for key in ("anchors", "seats", "furniture", "outline"))


@pytest.mark.parametrize("mode", ["unconfigured", "approximate"])
def test_unreviewed_calibration_never_creates_floor_positions(jpeg, mode):
    # A valid homography is not evidence that these points belong to this home.
    candidate = calibration_draft(DEFAULT)
    candidate.update(mode=mode, furniture=[dict(name="Synthetic", rect=[0.2, 0.2, 0.3, 0.3])],
                     camera=dict(map=[0.2, 0.8]), outline=[[0, 0], [1, 0], [1, 1]])
    room = observed_room(jpeg, candidate)
    people = [person()]
    result = room.update(jpeg, people, [], 10, registered=True, objects=[
        dict(**{"class": "chair"}, confidence=0.9, box=dict(x=0.1, y=0.1, w=0.8, h=0.8))
    ])
    assert room.floor is None and people[0]["position"] is None
    assert people[0]["motion_anchor"]["reference"] is not None
    assert people[0]["motion_anchor"]["floor"] is None
    assert result["position_status"] == "unconfigured"
    assert result["camera"] is None and result["outline"] == [] and result["furniture"] == []
    assert result["observed_furniture"][0]["ground_edge"] is None
    assert room.calibration_candidate == candidate  # Private editing candidate survives.


@pytest.mark.parametrize("mode", ["relative_calibrated", "approximate"])
def test_explicit_manual_calibration_keeps_projection(jpeg, mode):
    config = calibration_draft(DEFAULT)
    config.update(mode=mode, calibration_source="manual")
    room = observed_room(jpeg, config)
    people = [person()]
    room.update(jpeg, people, [], 10, registered=True)
    assert room.floor is not None
    assert people[0]["position"]["method"] == "visible_ankle"
    assert people[0]["motion_anchor"]["floor"] is not None


def test_live_player_slots_and_pointing_do_not_require_a_floor_plan(jpeg):
    room = observed_room(jpeg)
    hand = dict(side="right", confidence=0.9, joints={
        "wrist": dict(x=0.2, y=0.5, confidence=0.9),
        "indexMCP": dict(x=0.25, y=0.5, confidence=0.9),
        "indexPIP": dict(x=0.28, y=0.5, confidence=0.9),
        "indexTip": dict(x=0.34, y=0.5, confidence=0.9),
        "middleMCP": dict(x=0.20, y=0.53, confidence=0.9),
        "littleMCP": dict(x=0.18, y=0.52, confidence=0.9),
        "middlePIP": dict(x=0.22, y=0.52, confidence=0.9),
        "middleTip": dict(x=0.21, y=0.51, confidence=0.9),
        "ringMCP": dict(x=0.19, y=0.54, confidence=0.9),
        "ringPIP": dict(x=0.22, y=0.54, confidence=0.9),
        "ringTip": dict(x=0.20, y=0.52, confidence=0.9),
        "littlePIP": dict(x=0.21, y=0.54, confidence=0.9),
        "littleTip": dict(x=0.20, y=0.52, confidence=0.9),
    })
    for i in range(26):
        people = [person(1, -0.3), person(2, 0.3)]
        people[0]["joints"]["rightWrist"] = dict(hand["joints"]["wrist"])
        for item, x in zip(people, (0.2, 0.8)):
            item["joints"]["root"].update(x=x, y=0.5)
        result = room.update(jpeg, people, [copy.deepcopy(hand)], 10 + i * 0.1, registered=True)
    assert len(result["seats"]) == 2
    assert all(s["active"] and not s["located"] and s["map"] is None for s in result["seats"])
    assert all(p["position"] is None for p in people)
    assert result["pointing"][0]["confirmed"] and result["pointing"][0]["target"] == 2
    from homeplane.werewolf_camera import hints, observation
    camera, session = "cam_1111111111", "synthetic-session"
    sample = observation(dict(status="live", tracking_current=True, scene_visible=True,
                              frame_age_ms=50, camera_id=camera, session_id=session,
                              room=result, people=people))
    assert sample is not None
    suggestions = hints(sample, {1: (camera, session, 1), 2: (camera, session, 2)},
                        {1: SimpleNamespace(alive=True), 2: SimpleNamespace(alive=True)})
    assert suggestions[0]["pointing_at"] == 2
    assert suggestions[0]["advisory"] and not suggestions[0]["vote_submitted"]


@pytest.mark.parametrize("mode", ["quest", "ski", "tennis", "hiit"])
def test_original_games_accept_observed_ankles_without_fabricated_floor(jpeg, mode):
    room = observed_room(jpeg)
    game = MotionGames()
    game.reset(mode)
    for i in range(100):
        people = [person(1, -0.18 if mode == "tennis" else 0,
                         arms="horizontal" if mode == "quest" else "down")]
        if mode == "tennis":
            people.append(person(2, 0.18))
        layout = room.update(jpeg, people, [], 10 + i * 0.05, registered=True)
        result = game.update(people, layout["registration"], 10 + i * 0.05)
    assert result["status"] in {"playing", "complete"}
    assert result["players"] and all(p["floor_x"] is None for p in result["players"])
    assert all(p["motion_anchor"]["reference"] and p["motion_anchor"]["floor"] is None for p in people)
    assert all(p["position"] is None for p in people)


def test_unconfigured_tracking_still_revokes_motion_on_registration_loss(jpeg):
    room = observed_room(jpeg)
    people = [person()]
    room.update(jpeg, people, [], 10, registered=True)
    assert people[0]["motion_anchor"] is not None
    room.registration = None
    room.reg_info = {"status": "moving"}
    result = room.update(jpeg, people, [], 10.1, registered=True)
    assert people[0]["motion_anchor"] is None and people[0]["position"] is None
    assert result["pointing"] == []


def test_first_observation_never_saves_a_reference_or_calibration(tmp_path, jpeg):
    room = load_room(tmp_path, jpeg)
    assert room.floor is None and room.config["mode"] == "unconfigured"
    assert list(tmp_path.iterdir()) == []
    draft = calibration_draft(room.calibration_candidate)
    assert len(draft["anchors"]) == 4 and len(draft["seats"]) == 2
    assert room.config["anchors"] == [] and room.config["seats"] == []


def test_saved_projection_without_its_reference_is_not_applied(tmp_path, jpeg):
    config = calibration_draft(DEFAULT)
    config.update(mode="relative_calibrated", calibration_source="manual")
    path = tmp_path / "room-calibration.json"
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    room = load_room(tmp_path, jpeg)
    assert room.floor is None and room.config["mode"] == "unconfigured"
    assert path.read_bytes() == before and not (tmp_path / "room-reference.jpg").exists()


@pytest.mark.parametrize("fault", ["json", "not_object", "bad_homography", "reference", "oversized"])
def test_damaged_optional_calibration_keeps_real_body_input_without_overwriting_files(tmp_path, jpeg, fault):
    config = calibration_draft(DEFAULT)
    config.update(mode="relative_calibrated", calibration_source="manual")
    if fault == "bad_homography":
        for point in config["anchors"]:
            point["image"] = [0.5, 0.5]
    config_path, reference_path = tmp_path / "room-calibration.json", tmp_path / "room-reference.jpg"
    config_path.write_text("{" if fault == "json" else "[]" if fault == "not_object" else json.dumps(config))
    if fault == "oversized":
        config_path.write_bytes(b" " * (64 * 1024 + 1))
    reference_path.write_bytes(b"not-an-image" if fault == "reference" else jpeg)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    room = load_room(tmp_path, jpeg)
    assert room.floor is None and room.calibration_error == "calibration_unavailable"
    assert room.reference == jpeg
    room.registration = np.eye(3)
    room.reg_info = {"status": "locked"}
    people = [person()]
    room.update(jpeg, people, [], 10, registered=True)
    assert people[0]["motion_anchor"]["reference"] is not None and people[0]["position"] is None
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [False, True])
async def test_original_secondary_first_room_loading_does_not_block_or_publish_after_retire(tmp_path, jpeg, monkeypatch, retired):
    from tests.embodied.test_room_process import fake, until
    current=[True]
    identities=SimpleNamespace(discard=Mock(),process=Mock(return_value=[]),close=Mock())
    monkeypatch.setattr(camera_recognition,'IdentitySession',lambda *_:identities)
    monkeypatch.setattr(camera_recognition,'camera_storage',lambda _:tmp_path)
    monkeypatch.setattr(camera_recognition,'VisionProcess',lambda _:SimpleNamespace(
        detect=AsyncMock(return_value=frame()),close=AsyncMock()))
    monkeypatch.setattr(camera_recognition,'RoomSession',lambda folder:fake(folder,'open' if retired else 'ok',startup_timeout=20))
    recognizer=camera_recognition.CameraRecognition('synthetic_camera',Mock())
    packet=dict(jpeg=base64.b64encode(jpeg).decode(),width=640,height=360,received_at=time.time())
    work=asyncio.create_task(recognizer.observe(packet,source_current=lambda:current[0]))
    try:
        await until(lambda:recognizer.room is not None and recognizer.room.owner is not None and recognizer.room.owner.process is not None)
        if retired:current[0]=False
        result=await asyncio.wait_for(work,2)
        if retired:assert result is None and recognizer.last is None
        else:assert result is not None
        assert list(tmp_path.iterdir())==[]
    finally:
        await asyncio.gather(work,return_exceptions=True)
        await recognizer.close()



@pytest.mark.asyncio
async def test_original_calibration_get_uses_private_draft_instead_of_live_map(jpeg):
    from homeplane.embodied.calibration_frames import CalibrationFrames
    from fastapi import HTTPException
    room=observed_room(jpeg)
    source=dict(worker='synthetic',generation=1,motion_generation=2)
    frames=CalibrationFrames(clock=lambda:10,wall=lambda:1000)
    frames.publish(calibration_draft(room.calibration_candidate),jpeg,dict(width=640,height=360),source,1000)
    scope=dict(HTTPException=HTTPException,state={'status':'live'},calibration_frames=frames,calibration_source=lambda:source)
    draft=await actual('homeplane/embodied/worker.py','calibration',scope)()
    assert len(draft['anchors'])==4 and len(draft['seats'])==2
    assert base64.b64decode(draft['_reference']['jpeg'])==jpeg
    assert room.config['anchors']==[] and room.config['seats']==[]



@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unconfigured", "relative_calibrated"])
async def test_original_save_endpoint_marks_manual_calibration_and_loads_same_reference(tmp_path, jpeg, mode):
    from fastapi import HTTPException

    from homeplane.embodied.calibration_save import CalibrationSaver
    from homeplane.embodied.calibration_store import CalibrationStore
    from tests.embodied.test_calibration_atomic import request as make_request
    config = calibration_draft(DEFAULT)
    config["mode"] = mode

    source=dict(worker='synthetic',generation=1,motion_generation=2)
    from homeplane.embodied.calibration_frames import CalibrationFrames
    frames=CalibrationFrames(clock=lambda:10,wall=lambda:1000)
    frames.publish(config,jpeg,{},source,1000)
    config['_source']=frames.open(source)['_source']
    request=make_request(dict(config=config,reference_jpeg=base64.b64encode(jpeg).decode()))
    invalidate=Mock()
    service=CalibrationSaver(tmp_path,invalidate)
    scope = dict(HTTPException=HTTPException, base64=base64, json=json,
                 calibration_saver=service,calibration_source=lambda:source,calibration_frames=frames,state={'status':'live'},
                 room=None,camera_planner=None)
    tree = ast.parse(Path("homeplane/embodied/worker.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "save_calibration")
    node.decorator_list = []
    node.args.args[0].annotation = None
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<original calibration POST>", "exec"), scope)
    try:
        assert (await scope["save_calibration"](request))["saved"]
    finally:
        await service.close()
    assert scope['room'] is None
    invalidate.assert_called_once()
    stored = CalibrationStore(tmp_path).read()[0]
    assert stored["calibration_source"] == "manual"
    loaded = load_room(tmp_path, b"not-an-image")
    assert loaded.floor is not None and loaded.reference == jpeg
