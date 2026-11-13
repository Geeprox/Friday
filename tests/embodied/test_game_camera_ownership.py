"""Real game envelopes and original worker callbacks with inert motor/bridge ports."""

import ast
import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.party_camera import PartyCamera
from tests.embodied.test_camera_director import clear, person, sample
from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope
from tests.embodied.test_game_controls import game_handler, request
from tests.embodied.test_motion_games import LOCK, human
from tests.embodied.test_motor_recovery import primary_scope, view
from tests.test_werewolf_camera_owner import renew, source

WORKER = Path("homeplane/embodied/worker.py")


def actual(name, scope):
    scope.setdefault('party_camera',PartyCamera())
    node = next(
        n
        for n in ast.parse(WORKER.read_text()).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    node.decorator_list = []
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(WORKER), "exec"),
        scope,
    )
    return scope[name]


@pytest.mark.parametrize("mode", ["auto", "pets", "survey"])
@pytest.mark.parametrize("covered", [False, True])
def test_active_game_keeps_the_main_camera_still_through_wait_play_pause_and_results(mode, covered):
    for game_mode in ("ski", "tennis", "quest", "hiit", "beam"):
        for phase in ("waiting", "calibrating", "playing", "paused", "complete"):
            director = CameraDirector()
            director.apply_mode(mode, 1)
            game = MotionGames()
            game.reset(game_mode)
            game.publish([], phase, "SYNTHETIC")
            decisions = []
            for at in (10, 10.4, 11.2, 20, 40):
                current = sample(at, visible=not covered, people=[person(1, 0.9)])
                current.update(
                    game=game.public, animals_current=True, animals_intent=1, animals=[person(2, 0.9)]
                )
                decisions.append(director.choose(current, at))
            assert decisions == [None] * 5, (mode, covered, game_mode, phase, decisions)
            assert director.phase == "game_held"
            assert director.survey_attempts == director.recovery_steps == 0
            assert director.public()["physical_motion_verified"] is False
            if mode == 'survey':
                assert director.public()['survey_result'] == 'interrupted'
                game.reset('off')
                assert director.choose(dict(sample(42), game=game.public), 42) is None
                assert director.phase == 'survey_finished'


@pytest.mark.parametrize("finish_before_send", [False, True])
def test_a_motor_decision_from_before_game_admission_cannot_be_sent_after_it(finish_before_send):
    director, game = CameraDirector(), MotionGames()
    clear(director)
    before = dict(sample(1, people=[person(identity="SYNTHETIC")]), game=game.public)
    assert director.choose(before, 1) == "right"
    game.reset("ski")
    if finish_before_send:
        game.reset("off")
    current = dict(sample(1.1, people=[person(identity="SYNTHETIC")]), game=game.public)
    assert not director.decision_current(current, 1.1, "right")


def test_leaving_the_game_requires_fresh_camera_evidence_before_automatic_following_returns():
    director, game = CameraDirector(), MotionGames()
    clear(director)
    game.reset("ski")
    held = dict(sample(1, people=[person(identity="SYNTHETIC")]), game=game.public)
    assert director.choose(held, 1) is None
    game.reset("off")
    assert director.choose(dict(held, game=game.public), 1.1) is None
    first = dict(sample(1.2, people=[person(identity="SYNTHETIC")]), game=game.public)
    assert director.choose(first, 1.2) is None
    second = dict(sample(1.4, people=[person(identity="SYNTHETIC")]), game=game.public)
    assert director.choose(second, 1.4) == "right"


def test_out_of_order_game_frames_cannot_lower_the_resume_watermark():
    director, game = CameraDirector(), MotionGames()
    game.reset("ski")
    assert director.choose(dict(sample(10), game=game.public), 10) is None
    assert director.choose(dict(sample(9.5), game=game.public), 10.1) is None
    assert director.last_sample == 10
    game.reset("off")
    old = dict(sample(9.8, people=[person(identity="SYNTHETIC")]), game=game.public)
    assert director.choose(old, 10.2) is None
    assert director.choose(old, 10.3) is None


@pytest.mark.asyncio
async def test_party_lease_blocks_local_and_tv_start_until_release_without_changing_the_round():
    clock = [10.0]
    game = MotionGames(clock=lambda: clock[0])
    holder = PartyCamera()
    holder.command(renew(holder), source(), clock[0])
    control, start, _ = game_handler(game, holder, clock=lambda: clock[0])
    await control(request({'action': 'select', 'mode': 'ski'}, game=game))
    round_id, revision = game.round_id, game.control_revision
    body = request({'action': 'start', 'mode': 'ski'}, game=game)
    for display in (None, '192.0.2.10'):
        with pytest.raises(HTTPException) as error:
            await control(body, display_host=display)
        assert (error.value.status_code, error.value.detail) == (409, 'party_camera_busy')
        assert (game.mode, game.round_id, game.control_revision) == ('off', round_id, revision)
    start.assert_not_awaited()
    clock[0] += holder.LEASE_SECONDS
    assert (await control(body))['mode'] == 'ski'
    start.assert_awaited_once()


@pytest.mark.parametrize('observed_during_hold', [False, True])
def test_party_handoff_requires_releasing_the_old_start_gesture(observed_during_hold):
    game, holder = MotionGames(), PartyCamera()
    game.select('ski')
    pose = [human()]
    ok = [{'person_id': 1, 'gesture': 'ok'}]
    for step in range(1, 10):
        at = 10 + step / 10
        game.update(pose, LOCK, at, ok, party_camera_window=holder.window(at))
    assert 0 < game.gesture_progress < 1
    holder.command(renew(holder, at=11.0), source(11.0), 11.0)
    if observed_during_hold:
        held = game.update(pose, LOCK, 11.0, ok, party_camera_window=holder.window(11.0))
        assert held['gesture_control']['progress'] == 0
    holder.command(dict(action='end', context='b'*32, expected_scope=holder.scope, sequence=2,
                        not_after=12.0), {}, 11.0)
    for step in range(11, 24):
        at = 10 + step / 10
        game.update(pose, LOCK, at, ok, party_camera_window=holder.window(at))
    assert game.mode == 'off'
    for step in range(24, 31):
        at = 10 + step / 10
        game.update(pose, LOCK, at, party_camera_window=holder.window(at))
    for step in range(31, 43):
        at = 10 + step / 10
        game.update(pose, LOCK, at, ok, party_camera_window=holder.window(at))
    assert game.mode == 'ski'


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_send", [False, True])
async def test_original_motor_boundary_rejects_the_pre_game_decision_after_waiting(finish_before_send):
    scope, sent = primary_scope()
    director, game = CameraDirector(), MotionGames()
    now = scope["last_frame"]
    director.choose(dict(view(now - 0.1), game=game.public), now - 0.1)
    assert director.choose(dict(view(now), game=game.public), now) == "right"
    scope["state"].update(view(now))

    class Waiting:
        async def __aenter__(self):
            await asyncio.sleep(0)
            game.reset("ski")
            if finish_before_send:
                game.reset("off")

        async def __aexit__(self, *_):
            pass

    scope["room_lock"] = Waiting()

    def current():
        latest = dict(scope["state"], _local_received=scope["last_frame"], game=game.public)
        return director.decision_current(latest, scope["time"].monotonic(), "right")

    with pytest.raises(RuntimeError, match="stale_input"):
        await scope["motor_step"]("right", now, expected_intent="original", decision_current=current)
    assert sent == [] and scope["motor_waiters"] == {}
    assert scope["motion_generation"] == 0


@pytest.mark.asyncio
async def test_explicit_manual_positioning_still_uses_the_original_current_camera_boundary():
    scope, sent = primary_scope()
    game = MotionGames()
    game.reset("ski")
    scope["state"]["game"] = game.public
    result = await scope["motor_step"]("left", scope["last_frame"], expected_intent="original")
    assert result["native_code"] == 0 and len(sent) == 1
    assert sent[0]["direction"] == "left" and not scope["motor_waiters"]


@pytest.mark.asyncio
@pytest.mark.parametrize("camera", ["primary", "secondary"])
@pytest.mark.parametrize("transition", ["none", "start", "start_then_stop"])
async def test_queued_gestures_use_the_original_camera_and_game_window(monkeypatch, camera, transition):
    from homeplane.embodied import bridge

    game, jobs = MotionGames(), []
    command = AsyncMock(return_value=dict(status="SIMULATED", acknowledged=False, reached_target=False))
    monkeypatch.setattr(bridge, "command", command)
    scope = dict(
        SELECTED={"id": "primary"},
        games=game,
        interaction_enabled=True,
        home=SimpleNamespace(armed=True, spawn=jobs.append),
        secondary_homes={"secondary": SimpleNamespace(armed=True)},
        interaction_source_current=lambda _: True,
        fleet=SimpleNamespace(current=lambda *_: True, receipts=deque()),
        practice=SimpleNamespace(suppresses=lambda _: False),
        wall=SimpleNamespace(notice=Mock()),
        time=SimpleNamespace(monotonic=lambda: 10, time=lambda: 1000),
    )
    dispatch = actual("dispatch_interactions", scope)
    event = dict(
        camera=camera,
        session="SYNTHETIC",
        action="on",
        target="SYNTHETIC-light",
        event_id="SYNTHETIC-event",
        binding="a" * 32,
        revision=1,
        observed_mono=10,
    )
    await dispatch([event])
    if transition != "none":
        game.reset("ski")
    if transition == "start_then_stop":
        game.reset("off")
    for job in jobs:
        await job
    if camera == "secondary" or transition == "none":
        command.assert_awaited_once()
    else:
        command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_primary_gesture_from_before_a_waiting_home_callback_cannot_reenter_after_a_complete_game_cycle(
    monkeypatch,
):
    from homeplane.embodied import bridge

    game, jobs = MotionGames(), []
    prior_window = (game.control_context, game.control_revision)
    game.reset("ski")
    game.reset("off")
    command = AsyncMock(return_value=dict(status="SIMULATED", acknowledged=False, reached_target=False))
    monkeypatch.setattr(bridge, "command", command)
    scope = dict(
        SELECTED={"id": "primary"},
        games=game,
        interaction_enabled=True,
        home=SimpleNamespace(armed=True, spawn=jobs.append),
        secondary_homes={},
        interaction_source_current=lambda _: True,
        fleet=SimpleNamespace(current=lambda *_: True, receipts=[]),
        practice=SimpleNamespace(suppresses=lambda _: False),
        wall=SimpleNamespace(notice=Mock()),
        time=SimpleNamespace(monotonic=lambda: 10, time=lambda: 1000),
    )
    event = dict(
        camera="primary",
        session="SYNTHETIC",
        action="on",
        target="SYNTHETIC-light",
        event_id="SYNTHETIC-event",
        binding="a" * 32,
        revision=1,
        observed_mono=10,
        _game_window=prior_window,
    )
    await actual("dispatch_interactions", scope)([event])
    for job in jobs:
        await job
    command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cycle_during_observe", [False, True])
async def test_original_frame_producer_captures_the_game_window_before_awaiting_home(
    tmp_path, monkeypatch, cycle_during_observe
):
    from homeplane.embodied import bridge

    scope, _ = primary_frame_scope(tmp_path)
    game = scope["games"]
    game.reset("off")
    original_window = (game.control_context, game.control_revision)
    queued = []
    event = dict(
        camera=scope["SELECTED"]["id"],
        session=scope["identities"].session,
        action="on",
        target="SYNTHETIC-light",
        event_id="SYNTHETIC-event",
        binding="a" * 32,
        revision=1,
        observed_mono=10,
    )
    scope["fleet"].update.return_value = [event]
    scope["fleet"].current = lambda *_: True
    scope["fleet"].receipts = []
    scope["home"].armed = True
    scope["home"].spawn = queued.append
    scope["wall"].notice = Mock()
    scope["interaction_source_current"] = lambda _: True
    command = AsyncMock(return_value=dict(status="SIMULATED", acknowledged=False, reached_target=False))
    monkeypatch.setattr(bridge, "command", command)

    async def observe(*_, **__):
        await asyncio.sleep(0)
        if cycle_during_observe:
            game.reset("ski")
            game.reset("off")

    scope["home"].observe = AsyncMock(side_effect=observe)
    actual("dispatch_interactions", scope)
    await frame_continuation(scope)()
    assert event["_game_window"] == original_window
    assert queued
    for job in queued:
        await job
    if cycle_during_observe:
        command.assert_not_awaited()
    else:
        command.assert_awaited_once()
