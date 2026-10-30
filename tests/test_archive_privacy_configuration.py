"""Private configuration never starts inference or replaces unverified assets."""

import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/archive_privacy.py"


def run_cli(state, *arguments):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), "--state-dir", str(state), *arguments],
                          text=True, capture_output=True, timeout=10)


def test_status_is_noncreating_and_full_frame_is_owner_only(tmp_path):
    state = tmp_path.resolve() / "private-state"
    result = run_cli(state, "status")
    report = json.loads(result.stdout)
    assert result.returncode == 0 and not state.exists()
    assert report["mode"] == "full_frame" and not report["model_started"] and not report["recordings_read"]
    result = run_cli(state, "full-frame")
    assert result.returncode == 0
    config = state / "archive-privacy.json"
    assert json.loads(config.read_text()) == {"version": 1, "mode": "full_frame"}
    assert state.stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == 0o600


def test_unacknowledged_or_wrong_asset_keeps_existing_policy(tmp_path):
    state = tmp_path.resolve() / "state"
    assert run_cli(state, "full-frame").returncode == 0
    before = (state / "archive-privacy.json").read_bytes()
    source = tmp_path / "SIMULATED-wrong.onnx"
    source.write_bytes(b"SIMULATED-wrong-model")
    result = run_cli(state, "local-model", "--model", str(source))
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "privacy_model_terms_review_required"
    result = run_cli(state, "local-model", "--model", str(source), "--accept-model-terms")
    assert result.returncode == 1 and json.loads(result.stdout)["code"] == "privacy_model_digest_mismatch"
    assert (state / "archive-privacy.json").read_bytes() == before
    assert not list(state.rglob("*.tmp")) and not list(state.rglob("*.onnx"))


def test_verified_copy_rejects_symlinks_and_preserves_old_target(tmp_path):
    copy = runpy.run_path(str(SCRIPT))["copy_verified"]
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"SIMULATED-reviewed-source")
    target.write_bytes(b"SIMULATED-existing")
    with pytest.raises(ValueError, match="digest_mismatch"):
        copy(source, target, "0" * 64, 1024)
    assert target.read_bytes() == b"SIMULATED-existing"
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="source_unavailable"):
        copy(link, target, hashlib.sha256(source.read_bytes()).hexdigest(), 1024)
    copy(source, target, hashlib.sha256(source.read_bytes()).hexdigest(), 1024)
    assert target.read_bytes() == source.read_bytes() and target.stat().st_mode & 0o777 == 0o600
