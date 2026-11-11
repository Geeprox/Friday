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
        "/api/embodied/start", json=dict(
            camera=CAMERA, app="ski", difficulty="easy", ski_trail="first-snow", expected_intent=intent)
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
        {"mode": "ski", "action": "start", "ski_trail": "first-snow"},
        {"mode": "ski", "action": "select", "ski_trail": "missing"},
        {"mode": "ski", "action": "select", "ski_trail": ["first-snow"]},
        {"mode": "tennis", "action": "select", "ski_trail": "first-snow"},
        {"mode": "tennis", "action": "start", "tennis_variant": "practice"},
        {"mode": "tennis", "action": "prepare"},
        {"mode": "ski", "action": "prepare", "tennis_variant": "practice"},
        {"mode": "ski", "action": "select", "tennis_variant": "practice"},
        {"mode": "tennis", "action": "select", "tennis_variant": "unknown"},
        {"mode": "tennis", "action": "select", "tennis_variant": ["practice"]},
    ],
)
async def test_worker_rejects_invalid_game_options_without_effects(data):
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
async def test_worker_selects_tennis_practice_before_start_and_rejects_mid_round_switch():
    game = MotionGames()
    game.select('tennis')
    control, start, home = game_handler(game)
    solo = dict(mode='tennis', action='select', tennis_variant='practice')
    selected = await control(request(solo, game=game))
    assert selected['mode'] == 'off' and selected['tennis_variant'] == 'practice'
    start.assert_not_awaited()
    active = await control(request(dict(mode='tennis', action='start'), game=game))
    assert active['mode'] == 'tennis' and active['source_recovery']['required_players'] == 1
    assert active['tennis_variant'] == 'practice'
    start.assert_awaited_once()
    with pytest.raises(HTTPException) as error:
        await control(request(dict(solo, tennis_variant='match'), game=game))
    assert error.value.status_code == 400 and game.public == active
    home.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("difficulty,trail", [("easy", "first-snow"), ("challenge", "pine-slalom")])
async def test_actual_asgi_launch_receipts_drive_library_choice_readback_and_continue(
    bridge, tmp_path, difficulty, trail
):
    catalogue = (await bridge.client.get("/api/games")).json()
    command = dict(
        camera=CAMERA, app="ski", expected_intent=catalogue["service"]["game_intent"],
        difficulty=difficulty, ski_trail=trail,
    )
    result = await bridge.client.post("/api/embodied/start", json=command)
    assert result.status_code == 200
    acknowledged = result.json()["game"]
    route = next(item for item in catalogue["ski_trails"] if item["id"] == trail)
    assert acknowledged["ski_trail"] == trail
    assert [row[:2] for row in acknowledged["ski"]["route"]] == route["path"]
    assert [path for path, _ in bridge.calls if path in {"/api/game", "/api/start"}] == ["/api/game", "/api/start"]
    assert next(body for path, body in bridge.calls if path == "/api/game")["ski_trail"] == trail
    opened = await bridge.client.get("/api/games/ski/open")
    assert opened.status_code == 200
    alternate = "challenge" if difficulty == "easy" else "easy"
    peer = await bridge.client.post(
        "/api/embodied/start",
        json=dict(
            camera=CAMERA, app="ski", difficulty=alternate, ski_trail="summit",
            expected_intent=result.json()["game_intent"],
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
@pytest.mark.parametrize("app,trail,code", [("tennis", "first-snow", 409), ("ski", "unknown", 422)])
async def test_invalid_library_trail_cannot_change_worker(bridge, app, trail, code):
    before = copy.deepcopy(bridge.game.public)
    result = await bridge.client.post("/api/embodied/start", json=dict(
        camera=CAMERA, app=app, ski_trail=trail, expected_intent=await displayed(bridge)))
    assert result.status_code == code
    assert bridge.game.public == before
    assert not any(path in {"/api/game", "/api/start"} for path, _ in bridge.calls)


@pytest.mark.asyncio
async def test_library_does_not_enable_capture_if_worker_acknowledges_another_trail(bridge, monkeypatch):
    original = bridge.owner._send_game_command

    async def mismatched(command, game):
        selected = await original(command, game)
        return dict(selected, ski_trail="summit")

    monkeypatch.setattr(bridge.owner, "_send_game_command", mismatched)
    result = await bridge.client.post("/api/embodied/start", json=dict(
        camera=CAMERA, app="ski", ski_trail="first-snow", expected_intent=await displayed(bridge)))
    assert result.status_code == 409 and result.json()["detail"] == "game_state_changed"
    assert not any(path == "/api/start" for path, _ in bridge.calls)


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


@pytest.mark.asyncio
@pytest.mark.parametrize('variant,players', [('practice', 1), ('solo', 1), ('match', 2)])
@pytest.mark.parametrize('previous', ['off', 'ski', 'tennis'])
async def test_library_prepares_the_chosen_tennis_variant_as_a_new_round(bridge, variant, players, previous):
    if previous == 'tennis' and variant == 'match':
        bridge.game.reset('off')
        bridge.game.select('tennis', tennis_variant='practice')
    bridge.game.reset(previous)
    old = await displayed(bridge)
    chosen = await bridge.client.post('/api/embodied/start', json=dict(
        camera=CAMERA, app='tennis', tennis_variant=variant, difficulty='easy', expected_intent=old))
    assert chosen.status_code == 200
    result = chosen.json()
    assert result['game']['mode'] == 'off' and result['game']['selected_mode'] == 'tennis'
    assert result['game']['tennis_variant'] == variant and result['game']['difficulty'] == 'easy'
    assert result['game_intent'] != old
    writes = [(path, body) for path, body in bridge.calls if path in {'/api/game', '/api/start'}]
    assert [path for path, _ in writes] == ['/api/game', '/api/start']
    assert writes[0][1]['tennis_variant'] == variant
    bridge.owner._spawn_guarded_worker.assert_not_awaited()
    bridge.owner._stop.assert_not_awaited()
    stage = await bridge.client.get('/api/games/tennis/open', headers={'X-Friday-Game-Intent': result['game_intent']})
    assert stage.status_code == 200
    outdated = await bridge.client.get('/api/games/tennis/open', headers={'X-Friday-Game-Intent': old})
    assert outdated.status_code == 409 and outdated.json()['detail'] == 'game_state_changed'
    control, start, _ = game_handler(bridge.game)
    playing = await control(request(dict(mode='tennis', action='start'), game=bridge.game))
    assert playing['tennis_variant'] == variant and playing['source_recovery']['required_players'] == players
    start.assert_awaited_once()


@pytest.mark.asyncio
async def test_library_stage_handoff_rejects_a_newer_round_even_on_the_same_court(bridge):
    bridge.game.select('tennis')
    intent = await displayed(bridge)
    bridge.game.select('tennis', tennis_variant='practice')
    response = await bridge.client.get('/api/games/tennis/open', headers={'X-Friday-Game-Intent': intent})
    assert response.status_code == 409
    current = await displayed(bridge)
    wrong_app = await bridge.client.get('/api/games/ski/open', headers={'X-Friday-Game-Intent': current})
    assert wrong_app.status_code == 409
    assert not any(path in {'/api/game', '/api/start'} for path, _ in bridge.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('app,variant,code', [('ski', 'practice', 409), ('tennis', 'unknown', 422)])
async def test_invalid_tennis_setup_leaves_the_current_round(bridge, app, variant, code):
    before = copy.deepcopy(bridge.game.public)
    selected = await bridge.client.post('/api/embodied/start', json=dict(
        camera=CAMERA, app=app, tennis_variant=variant, expected_intent=await displayed(bridge)))
    assert selected.status_code == code and bridge.game.public == before
    assert not any(path in {'/api/game', '/api/start'} for path, _ in bridge.calls)


@pytest.mark.asyncio
async def test_tennis_launch_requires_the_worker_to_acknowledge_the_requested_variant(bridge, monkeypatch):
    original = bridge.owner._send_game_command

    async def other_variant(body, game):
        result = await original(body, game)
        return dict(result, tennis_variant='match')

    monkeypatch.setattr(bridge.owner, '_send_game_command', other_variant)
    response = await bridge.client.post('/api/embodied/start', json=dict(
        camera=CAMERA, app='tennis', tennis_variant='practice', expected_intent=await displayed(bridge)))
    assert response.status_code == 409 and response.json()['detail'] == 'game_state_changed'
    assert not any(path == '/api/start' for path, _ in bridge.calls)
