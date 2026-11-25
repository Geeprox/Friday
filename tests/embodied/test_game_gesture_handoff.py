"""Game-to-home gesture handoff through the actual coordinator, no camera/model I/O."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeplane.embodied.party_camera import PartyCamera

from homeplane.embodied import interaction, interaction_fleet
from homeplane.embodied.interaction import Binding, Configuration
from homeplane.embodied.interaction_fleet import InteractionFleet
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_game_camera_ownership import actual
from tests.embodied.test_gesture_continuity import person as arm_pose

CAMERA = "cam_1111111111"
OTHER = "cam_2222222222"


def person(*, raised=False, unknown=False, track=7):
    joints = {}
    for side, x in (("left", 0.35), ("right", 0.65)):
        joints[side + "Shoulder"] = dict(x=x, y=0.25, confidence=1.0)
        joints[side + "Hip"] = dict(x=x, y=0.55, confidence=1.0)
        joints[side + "Wrist"] = dict(x=x, y=0.04 if raised else 0.72, confidence=1.0)
        joints[side + "Knee"] = dict(x=x, y=0.75, confidence=1.0)
        joints[side + "Ankle"] = dict(x=x, y=0.95, confidence=1.0)
    return dict(id=track, joints={} if unknown else joints, identity={})


def configure(monkeypatch, gestures=("hand.open_palm",)):
    config = Configuration(
        revision=1,
        enabled=True,
        bindings=[
            Binding(
                id=str(index + 1) * 32, gesture=gesture, target="SYNTHETIC-light", action="on", enabled=True
            )
            for index, gesture in enumerate(gestures)
        ],
    )
    monkeypatch.setattr(interaction, "load", lambda: config)
    monkeypatch.setattr(interaction_fleet, "load", lambda: config)
    return config


def step(fleet, at, *, label=None, pose=None, active=True, camera=CAMERA, session="SYNTHETIC", **kwargs):
    pose = pose or person()
    hands = [dict(person_id=pose["id"], gesture=label)] if label else []
    return fleet.update(camera, session, [pose], hands, dict(status="locked"), active, at, **kwargs)


def consume(fleet):
    for i in range(12):
        assert not step(fleet, i / 10, active=False, label="open_palm", pose=person(raised=True))


@pytest.mark.asyncio
async def test_a_held_finish_pose_cannot_reach_the_original_household_dispatch(monkeypatch):
    from homeplane.embodied import bridge

    configure(monkeypatch)
    fleet, game, queued = InteractionFleet(), MotionGames(), []
    consume(fleet)
    clock = [1.2]
    command = AsyncMock(return_value=dict(status="SIMULATED", acknowledged=False, reached_target=False))
    monkeypatch.setattr(bridge, "command", command)
    scope = dict(
        SELECTED={"id": CAMERA},
        games=game,
        party_camera=PartyCamera(),
        interaction_enabled=True,
        home=SimpleNamespace(armed=True, spawn=queued.append),
        secondary_homes={},
        interaction_source_current=lambda _: True,
        fleet=fleet,
        practice=SimpleNamespace(suppresses=lambda _: False),
        wall=SimpleNamespace(notice=Mock()),
        time=SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]),
    )
    dispatch = actual("dispatch_interactions", scope)
    for i in range(12, 51):
        clock[0] = i / 10
        events = step(fleet, clock[0], label="open_palm", pose=person(raised=True))
        await dispatch(events)
        while queued:
            await queued.pop(0)
    command.assert_not_awaited()
    assert fleet.public(clock[0])["cameras"][0]["interaction"]["release_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_at", [None, "before_secondary", "after_secondary"])
async def test_fresh_gesture_can_dispatch_from_the_other_camera_if_the_first_source_closes(monkeypatch, lost_at):
    from homeplane.embodied import bridge

    configure(monkeypatch)
    fleet, game, queued = InteractionFleet(), MotionGames(), []
    current_sources = {CAMERA, OTHER}
    clock = [0.0]

    async def send(*args, **kwargs):
        await asyncio.sleep(0)
        return dict(status="unknown_outcome", acknowledged=False, reached_target=False)

    command = AsyncMock(side_effect=send)
    monkeypatch.setattr(bridge, "command", command)
    scope = dict(
        SELECTED=dict(id=CAMERA, room="SYNTHETIC-room"), games=game, party_camera=PartyCamera(),
        interaction_enabled=True, home=SimpleNamespace(armed=True, spawn=queued.append),
        secondary_homes={OTHER: SimpleNamespace(armed=True)},
        interaction_source_current=lambda event: event["camera"] in current_sources,
        fleet=fleet, practice=SimpleNamespace(suppresses=lambda _: False),
        wall=SimpleNamespace(cameras=[dict(id=OTHER, room="SYNTHETIC-room")], notice=Mock()),
        time=SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]),
    )
    dispatch = actual("dispatch_interactions", scope)

    def frame(camera, track, at, label=None):
        clock[0] = at
        pose = person(track=track)
        pose["identity"] = dict(state="matched", person_id="SYNTHETIC-member")
        return step(fleet, at, label=label, camera=camera, pose=pose, session=camera)

    def close_primary():
        current_sources.remove(CAMERA)
        fleet.clear(CAMERA)

    for at in (10.0, 10.4, 10.8):
        assert not frame(CAMERA, 7, at)
        assert not frame(OTHER, 99, at)
    assert not frame(CAMERA, 7, 11.3, "open_palm")
    assert not frame(OTHER, 99, 11.35, "open_palm")
    await dispatch(frame(CAMERA, 7, 12.05, "open_palm"))
    if lost_at == "before_secondary":
        close_primary()
    await dispatch(frame(OTHER, 99, 12.15, "open_palm"))
    if lost_at == "after_secondary":
        close_primary()
    await asyncio.gather(*queued)

    command.assert_awaited_once()
    assert command.await_args.kwargs["camera_id"] == (OTHER if lost_at else CAMERA)
    assert fleet.receipts[0]["status"] == "unknown_outcome"


@pytest.mark.parametrize("gesture,label", [("hand.open_palm", "open_palm"), ("body.hands_up", None)])
def test_relax_then_make_a_fresh_gesture_restores_control_once(monkeypatch, gesture, label):
    configure(monkeypatch, (gesture,))
    fleet = InteractionFleet()
    consume(fleet)
    for i in range(12, 35):
        assert not step(fleet, i / 10, label=label, pose=person(raised=True))
    for i in range(35, 42):
        assert not step(fleet, i / 10)
    events = []
    for i in range(42, 61):
        events += step(fleet, i / 10, label=label, pose=person(raised=True))
    assert len(events) == 1
    assert fleet.current(events[0], 5.1)


@pytest.mark.parametrize("fault", ["occlusion", "short_release", "source_gap", "new_session"])
def test_missing_or_discontinuous_input_is_not_a_release(monkeypatch, fault):
    configure(monkeypatch)
    fleet = InteractionFleet()
    consume(fleet)
    session = "SYNTHETIC"
    for i in range(12, 30):
        assert not step(fleet, i / 10, label="open_palm", pose=person(raised=True))
    if fault == "new_session":
        session = "SYNTHETIC-new"
    elif fault == "short_release":
        assert not step(fleet, 3.0)
    elif fault == "source_gap":
        fleet.clear(CAMERA)
    else:
        for i in range(30, 41):
            assert not step(fleet, i / 10, pose=person(unknown=True))
    for i in range(42, 70):
        assert not step(fleet, i / 10, label="open_palm", pose=person(raised=True), session=session)


def test_primary_game_handoff_does_not_block_another_camera_or_its_microphone(monkeypatch):
    configure(monkeypatch)
    fleet = InteractionFleet()
    fleet.session(CAMERA, "SYNTHETIC")
    assert fleet.voice(CAMERA, 0, enabled=True, session="SYNTHETIC")
    consume(fleet)
    assert fleet.voice_current(CAMERA, 1)
    events = []
    for i in range(12, 40):
        assert not step(fleet, i / 10, label="open_palm", pose=person(raised=True))
        events += step(fleet, i / 10, label="open_palm", camera=OTHER)
    assert len(events) == 1 and events[0]["camera"] == OTHER


@pytest.mark.asyncio
async def test_hiit_arm_open_in_the_same_room_never_reaches_the_device_bridge(tmp_path, monkeypatch):
    from homeplane.embodied import bridge

    outside = "cam_3333333333"
    config = configure(monkeypatch, ("body.arms_open",))
    config.bindings[0].target = "SYNTHETIC-curtain"
    config.bindings[0].action = "open"
    fleet, game, queued = InteractionFleet(), MotionGames(), []
    command = AsyncMock(return_value=dict(status="SIMULATED", acknowledged=False, reached_target=False))
    monkeypatch.setattr(bridge, "command", command)
    cameras = [
        dict(id=OTHER, room="SYNTHETIC-exercise-room"),
        dict(id=outside, room="SYNTHETIC-other-room"),
    ]
    services = {
        row["id"]: SimpleNamespace(enabled=True, armed=True, speaking=False, last_audio=None,
                                  input_allowed=lambda: True,
                                  attention=SimpleNamespace(candidate=lambda: None), observe=AsyncMock())
        for row in cameras
    }
    clock = [0.0]
    scope = dict(
        SELECTED=dict(id=CAMERA, room="SYNTHETIC-exercise-room"), games=game, fleet=fleet,
        home=SimpleNamespace(spawn=queued.append), secondary_homes=services,
        interaction_enabled=True, interaction_source_current=lambda _: True,
        secondary_frame_current=lambda *_: True, PRIVATE=tmp_path,
        time=SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]),
        practice=SimpleNamespace(observe=Mock(), suppresses=lambda _: False),
        safety=SimpleNamespace(offer=Mock(), sound_requested=lambda _: False),
        wall=SimpleNamespace(cameras=cameras, observe=Mock(), notice=Mock()),
    )
    actual("dispatch_interactions", scope)
    observe = actual("secondary_observation", scope)

    async def play(start, end, pose):
        for index in range(start, end):
            clock[0] = index / 10
            for camera in (OTHER, outside):
                await observe(dict(camera=camera, session="SYNTHETIC", now=clock[0], captured_mono=clock[0],
                                   people=[pose], hands=[], faces=[], registration=dict(status="locked")), object())
            while queued:
                await queued.pop(0)

    game.reset("hiit")
    await play(0, 13, arm_pose(False))
    await play(13, 22, arm_pose(True))
    assert command.await_count == 1
    assert command.await_args.kwargs["camera_id"] == outside
    assert services[OTHER].armed and services[OTHER].observe.await_args.args[5] is True
    assert fleet.cameras[OTHER]["state"] == "game"

    game.reset("off")
    await play(22, 35, arm_pose(True))
    assert command.await_count == 1
    await play(35, 46, person())
    await play(46, 51, arm_pose(False))
    await play(51, 56, arm_pose(True))
    assert [call.kwargs["camera_id"] for call in command.await_args_list].count(OTHER) == 1


def test_game_window_changes_require_release_even_without_an_intermediate_camera_frame(monkeypatch):
    configure(monkeypatch)
    fleet, game = InteractionFleet(), MotionGames()
    window = (game.control_context, game.control_revision)
    for i in range(10):
        assert not step(fleet, i / 10, game_window=window)
    game.reset("ski")
    game.reset("off")
    window = (game.control_context, game.control_revision)
    for i in range(10, 40):
        assert not step(fleet, i / 10, label="open_palm", pose=person(raised=True), game_window=window)


def test_a_binding_with_missing_body_evidence_does_not_block_another_released_binding(monkeypatch):
    configure(monkeypatch, ("hand.open_palm", "body.squat"))
    fleet = InteractionFleet()
    consume(fleet)
    upper = person()
    for key in list(upper["joints"]):
        if key.endswith(("Knee", "Ankle")):
            del upper["joints"][key]
    for i in range(12, 40):
        assert not step(fleet, i / 10, pose=copy.deepcopy(upper))
    events = []
    for i in range(40, 56):
        events += step(fleet, i / 10, pose=copy.deepcopy(upper), label="open_palm")
    assert len(events) == 1


def test_the_consumed_operator_ok_is_still_observed_as_held_during_handoff(monkeypatch):
    configure(monkeypatch, ("hand.ok",))
    fleet = InteractionFleet()
    consume(fleet)
    for i in range(12, 50):
        assert not step(fleet, i / 10, label="ok")
    status = fleet.public(4.9)["cameras"][0]["interaction"]
    assert status["release_required"], "ownership selection cannot invent a release of the same physical OK"


def test_pointing_requires_relaxation_and_does_not_borrow_a_held_confirmation(monkeypatch):
    config = configure(monkeypatch)
    config.bindings = [
        Binding(
            id="a" * 32,
            camera=CAMERA,
            scope="camera",
            gesture="hand.point",
            target="SYNTHETIC-light",
            action="on",
            enabled=True,
            anchor=[0.8, 0.5],
        )
    ]
    fleet = InteractionFleet()
    consume(fleet)
    pointing = [
        dict(person_id=7, gesture="pointing", ray=dict(origin=[0.1, 0.5], direction=[1, 0])),
        dict(person_id=7, gesture="ok"),
    ]
    for i in range(12, 50):
        assert not fleet.update(
            CAMERA, "SYNTHETIC", [person()], pointing, dict(status="locked"), True, i / 10
        )
    assert fleet.cameras[CAMERA]["engine"].pending is None
    for i in range(50, 56):
        assert not step(fleet, i / 10)
    for i in range(56, 67):
        assert not fleet.update(
            CAMERA, "SYNTHETIC", [person()], pointing[:1], dict(status="locked"), True, i / 10
        )
    event = step(fleet, 6.8, label="ok")
    assert len(event) == 1 and event[0]["target"] == "SYNTHETIC-light"


def test_the_original_primary_frame_passes_the_game_window_to_the_real_coordinator(tmp_path, monkeypatch):
    from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope

    async def check():
        configure(monkeypatch)
        scope, _ = primary_frame_scope(tmp_path)
        scope["games"].reset("off")
        await frame_continuation(scope)()
        window = (scope["games"].control_context, scope["games"].control_revision)
        assert scope["fleet"].update.call_args.kwargs["game_window"] == (window,scope['party_camera'].window(10)[:2])
        assert scope["fleet"].update.call_args.kwargs["game_active"] is False
        assert scope["fleet"].update.call_args.kwargs["captured_mono"] == 10

    import asyncio

    asyncio.run(check())


def test_repeated_frames_cannot_advance_release_time(monkeypatch):
    configure(monkeypatch)
    fleet = InteractionFleet()
    consume(fleet)
    for i in range(12, 30):
        assert not step(fleet, i / 10, label="open_palm", pose=person(raised=True))
    for i in range(30, 38):
        assert not step(fleet, i / 10, captured_mono=2.95)
    assert fleet.cameras[CAMERA]["engine"].rearm, "one repeated relaxed frame is not a release interval"
    for i in range(38, 60):
        assert not step(fleet, i / 10, label="open_palm", pose=person(raised=True))
    for i in range(60, 66):
        assert not step(fleet, i / 10, captured_mono=i / 10)
    events = []
    for i in range(66, 82):
        events += step(fleet, i / 10, label="open_palm", pose=person(raised=True), captured_mono=i / 10)
    assert len(events) == 1


def test_buffered_relaxed_frames_from_before_the_game_ended_cannot_rearm_control(monkeypatch):
    configure(monkeypatch)
    fleet = InteractionFleet()
    original_window, finished_window = ("SYNTHETIC-game", 1), ("SYNTHETIC-game", 3)
    for i in range(20):
        assert not step(fleet, i / 10, game_window=original_window, captured_mono=i / 10)
    # These are different captures, but all preceded the game-finish revision.
    for i in range(30, 38):
        assert not step(fleet, i / 10, game_window=finished_window, captured_mono=2.6 + (i - 30) * 0.05)
    for i in range(38, 50):
        assert not step(
            fleet,
            i / 10,
            game_window=finished_window,
            label="open_palm",
            pose=person(raised=True),
            captured_mono=i / 10,
        )
    assert fleet.cameras[CAMERA]["engine"].rearm
    for i in range(50, 56):
        assert not step(fleet, i / 10, game_window=finished_window, captured_mono=i / 10)
    events = []
    for i in range(56, 72):
        events += step(
            fleet,
            i / 10,
            game_window=finished_window,
            label="open_palm",
            pose=person(raised=True),
            captured_mono=i / 10,
        )
    assert len(events) == 1
