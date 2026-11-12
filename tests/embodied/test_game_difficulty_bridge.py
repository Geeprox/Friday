"""Actual API -> service -> original worker handler -> rules, with inert I/O."""

import copy
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_game_controls import game_handler, request
from tests.embodied.test_game_library_intent import CAMERA, displayed
from tests.embodied.test_game_library_intent import bridge as bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("difficulty", ["easy", "standard", "challenge"])
@pytest.mark.parametrize("mode", ["quest", "hiit", "ski", "tennis"])
async def test_real_launch_bridge_carries_difficulty_before_input_start(bridge, mode, difficulty):
    payload = dict(camera=CAMERA, app=mode, difficulty=difficulty, expected_intent=await displayed(bridge))
    result = await bridge.client.post("/api/embodied/start", json=payload)
    assert result.status_code == 200
    assert result.json()["game"]["difficulty"] == difficulty
    assert result.json()["game"]["selected_mode"] == mode
    writes = [(path, data) for path, data in bridge.calls if path != "/api/status"]
    assert [path for path, _ in writes] == ["/api/game", "/api/start"]
    assert writes[0][1]["difficulty"] == difficulty
    assert bridge.game.mode == "off" and bridge.game.game_input_enabled
    bridge.owner._spawn_guarded_worker.assert_not_awaited()
    bridge.owner._stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("difficulty", ["expert", "", 0, True, [], {}])
async def test_invalid_launch_profile_is_rejected_before_worker_or_camera_work(bridge, difficulty):
    before = copy.deepcopy(bridge.game.public)
    response = await bridge.client.post(
        "/api/embodied/start",
        json=dict(camera=CAMERA, app="ski", difficulty=difficulty, expected_intent=await displayed(bridge)),
    )
    assert response.status_code == 422 and bridge.game.public == before
    assert not any(path in {"/api/game", "/api/start"} for path, _ in bridge.calls)


@pytest.mark.asyncio
async def test_profile_change_after_read_invalidates_the_old_launch_without_retry(bridge):
    intent = await displayed(bridge)
    bridge.game.reset("ski", difficulty="challenge")
    before = copy.deepcopy(bridge.game.public)
    result = await bridge.client.post(
        "/api/embodied/start", json=dict(camera=CAMERA, app="ski", difficulty="easy", expected_intent=intent)
    )
    assert result.status_code == 409 and bridge.game.public == before
    assert not any(path in {"/api/game", "/api/start"} for path, _ in bridge.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"mode": "ski", "action": "pause", "difficulty": "easy"},
        {"mode": "ski", "action": "resume", "difficulty": "easy"},
        {"mode": "off", "difficulty": "easy"},
        {"mode": "home", "action": "select", "difficulty": "easy"},
        {"mode": "ski", "action": "select", "difficulty": None},
        {"mode": "ski", "action": "start", "difficulty": "expert"},
    ],
)
async def test_worker_rejects_difficulty_on_non_selection_commands_without_effects(data):
    game = MotionGames()
    game.reset("ski", difficulty="challenge")
    before = copy.deepcopy(game.public)
    control, start, home = game_handler(game)
    with pytest.raises(HTTPException) as error:
        await control(request(data, game=game))
    assert error.value.status_code == 400 and game.public == before
    start.assert_not_awaited()
    home.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("difficulty", ["easy", "challenge"])
async def test_actual_asgi_launch_receipts_drive_library_choice_readback_and_continue(
    bridge, tmp_path, difficulty
):
    catalogue = (await bridge.client.get("/api/games")).json()
    command = dict(
        camera=CAMERA, app="ski", expected_intent=catalogue["service"]["game_intent"], difficulty=difficulty
    )
    result = await bridge.client.post("/api/embodied/start", json=command)
    assert result.status_code == 200
    opened = await bridge.client.get("/api/games/ski/open")
    assert opened.status_code == 200
    alternate = "challenge" if difficulty == "easy" else "easy"
    peer = await bridge.client.post(
        "/api/embodied/start",
        json=dict(
            camera=CAMERA, app="ski", difficulty=alternate, expected_intent=result.json()["game_intent"]
        ),
    )
    assert peer.status_code == 200
    packet = tmp_path / "synthetic-game-difficulty.json"
    packet.write_text(
        json.dumps(
            dict(
                catalogue=catalogue,
                command=command,
                started=result.json(),
                opened=opened.json(),
                peer=peer.json(),
            )
        )
    )
    run = subprocess.run(
        ["node", "tests/game_difficulty_dom_fixture.cjs", str(packet)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert all(json.loads(run.stdout).values())


@pytest.mark.asyncio
async def test_actual_worker_receipts_drive_stage_difficulty_replay_and_stop(tmp_path):
    game = MotionGames()
    game.select("ski")
    initial = copy.deepcopy(game.public)
    control, start, home = game_handler(game)
    command = dict(
        mode="ski",
        action="select",
        difficulty="easy",
        expected_context=initial["control_context"],
        expected_revision=initial["control_revision"],
    )
    changed = await control(request(command))
    started = await control(request(dict(mode="ski", action="start"), game=game))
    stopped = await control(request(dict(mode="off"), game=game))
    start.assert_awaited_once()
    home.assert_not_awaited()
    path = tmp_path / "synthetic-stage-difficulty.json"
    path.write_text(
        json.dumps(dict(initial=initial, command=command, changed=changed, started=started, stopped=stopped))
    )
    result = subprocess.run(
        ["node", "tests/game_difficulty_stage_fixture.cjs", str(path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(json.loads(result.stdout).values())
