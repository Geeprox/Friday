"""Original match packets reach the original HUD and canvas renderer, inert only."""

import subprocess
from pathlib import Path


def test_original_tennis_scene_and_hud_journey():
    result = subprocess.run(
        ["node", "--test", "tests/tennis_scene.test.cjs"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
