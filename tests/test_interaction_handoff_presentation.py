"""Actual interaction workspace with inert fetch/DOM, no browser/hardware."""

import subprocess
from pathlib import Path


def test_game_release_status_reaches_the_original_workspace_without_policy_writes():
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "tests/whole_house_interactions_fixture.cjs"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All-camera voice" in result.stdout
