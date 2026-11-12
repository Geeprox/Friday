"""Unowned service -> original launch API -> original UI -> stopped/read-back return."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeplane.embodied import service as module
from homeplane.embodied.motion_games import MotionGames
from tests.embodied.test_game_library_intent import CAMERA
from tests.embodied.test_game_library_intent import bridge as bridge


@pytest.mark.asyncio
async def test_first_launch_and_stopped_exit_with_original_asgi_receipts(bridge, monkeypatch, tmp_path):
    owner = bridge.owner
    owner.process = None
    owner.selected = None
    bridge.game = MotionGames()
    probe = MagicMock()
    probe.__enter__.return_value.connect_ex.return_value = 61
    monkeypatch.setattr(module, "socket", SimpleNamespace(socket=lambda: probe))
    monkeypatch.setattr(owner, "_recover_pending", AsyncMock())

    async def spawn(environment):
        assert environment["FRIDAY_CAMERA_ID"] == CAMERA
        owner.process = SimpleNamespace(returncode=None, pid=99999998)

    owner._spawn_guarded_worker = AsyncMock(side_effect=spawn)
    catalogue = (await bridge.client.get("/api/games")).json()
    assert not catalogue["service"]["owned"] and "game" not in catalogue["service"]
    command = dict(
        camera=CAMERA, app="ski", difficulty="standard", ski_trail="pine-slalom",
        expected_intent=catalogue["service"]["game_intent"],
    )
    result = await bridge.client.post("/api/embodied/start", json=command)
    assert result.status_code == 200, result.text
    started = result.json()
    assert started["owned"] and started["game"]["selected_mode"] == "ski"
    assert started["game"]["ski_trail"] == "pine-slalom"
    opened = await bridge.client.get("/api/games/ski/open")
    assert opened.status_code == 200
    owner._spawn_guarded_worker.assert_awaited_once()
    owner._stop.assert_not_awaited()
    owner.process = None
    stopped = (await bridge.client.get("/api/games")).json()
    assert not stopped["service"]["owned"]
    packet = tmp_path / "synthetic-first-launch.json"
    packet.write_text(
        json.dumps(
            dict(catalogue=catalogue, command=command, started=started, opened=opened.json(), stopped=stopped)
        )
    )
    run = subprocess.run(
        ["node", "tests/game_launch_dom_fixture.cjs", str(packet)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert all(json.loads(run.stdout).values())
