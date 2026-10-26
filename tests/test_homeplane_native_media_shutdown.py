"""Exact shutdown/GC regression and bounded exits of test-owned workers."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from homeplane.media_contracts import MediaProbeResult

PROJECT = Path(__file__).resolve().parents[1]
CASES = ["shutdown_gc", "cancel", "repeat_cancel", "native_task_cancel", "cleanup_timeout", "cleanup_cancel",
    "worker_timeout", "worker_main_timeout", "worker_cancel", "worker_cleanup_timeout",
    "worker_broken_pipe", "worker_normal"]


@pytest.mark.parametrize("case", CASES)
@pytest.mark.asyncio
async def test_shutdown_lifetime_and_worker_exit(case, tmp_path):
    source = os.environ.get("FRIDAY_TEST_CAMERA_VENDOR_SOURCE")
    if not source:
        pytest.skip("Provide explicit pinned camera.py source; no SDK runtime is loaded")
    marker = tmp_path / "SIMULATED-deinit.json"
    began = time.monotonic()
    process = await asyncio.create_subprocess_exec(sys.executable, "-B",
        str(PROJECT / "scripts/check_camera_shutdown_lifetime.py"),
        "--vendor-source", source, "--case", case, "--marker", str(marker),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}, start_new_session=True)
    try:
        if case == "worker_cancel":
            line = await asyncio.wait_for(process.stderr.readline(), 2)
            assert line == b"SIMULATED-started\n"
            process.terminate()  # SIGTERM to this synthetic subprocess only.
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 1)
    assert time.monotonic() - began < 5
    assert process.returncode == (1 if case == "worker_broken_pipe" else 0), stderr.decode()
    assert "miot" not in sys.modules
    if case == "worker_broken_pipe":
        assert not stdout and not marker.exists() and not marker.with_suffix(".atexit").exists()
        return
    result = json.loads(stdout)
    assert result["simulated"]
    if case.startswith("worker_"):
        report = MediaProbeResult.model_validate(result)
        if case == "worker_normal":
            assert report.status == "decoded_sync_unknown" and report.native_start_status == "succeeded"
            assert json.loads(marker.read_text()) == {"native_call_still_blocked": False}
            assert marker.with_suffix(".atexit").exists()
        else:
            assert report.status == "failed"
            assert report.failure == ("stream_closed" if case == "worker_cancel" else "worker_timeout")
            assert not marker.exists() and not marker.with_suffix(".atexit").exists()
    else:
        assert result["manager_alive_after_shutdown_cancel_and_gc"]
        assert result["instance_alive_after_shutdown_cancel_and_gc"]
        assert result["native_call_still_blocked"] and result["destructor_deinit_calls"] == []
        assert result["explicit_close_started"] == (case in {"cleanup_timeout", "cleanup_cancel"})
