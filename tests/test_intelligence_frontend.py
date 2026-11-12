"""把语音权限、草稿保留和设置交互接入常规 CI。"""

import subprocess
from pathlib import Path


def test_intelligence_settings_and_voice_lifecycle():
    result = subprocess.run(
        ["node", "--test", "tests/intelligence_frontend.test.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
