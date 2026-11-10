"""Actual producer packets and inert scene commands; no runtime owners or hardware."""
import subprocess
from pathlib import Path

def test_tennis_pose_animation_and_existing_scene_lifetimes():
    result = subprocess.run(
        ['node', '--test', 'tests/tennis_animation.test.cjs', 'tests/tennis_scene.test.cjs',
         'tests/game_commands.test.cjs', 'tests/quest_scene.test.cjs', 'tests/ski_scene.test.cjs', 'tests/game_stage_lifecycle.test.cjs'],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
