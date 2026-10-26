"""Executable first-run consumer fixtures; this is not browser rendering coverage."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", [
    "first_run", "sftp", "lost_response", "conflict_and_disconnect",
    "navigation_draft_and_late_read", "unknown_and_validation", "truth_and_polling", "catalog_completeness",
])
def test_synthetic_nas_console_consumer(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for Console fixture")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(root / "tests/nas_onboarding_console_fixture.cjs"),
                             str(root / "homeplane/static/app.js"), scenario],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
