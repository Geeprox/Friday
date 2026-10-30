"""Candidate production functions with simulated workers; no network or real PIN."""
import importlib.util
import json
import asyncio
import tempfile
from pathlib import Path

import pytest
from homeplane.geek_setup import GeekSetup
from homeplane.settings import SettingsService
from test_homeplane_geek_session import Process, settle

import homeplane.geek_daemon as module
ROOT = Path(__file__).resolve().parents[1]
URL = "http://192.168.50.1:8086"


def make(tmp_path, failure=None):
    SettingsService(tmp_path / "settings.json").update({"geek_url": URL})
    GeekSetup(tmp_path).save("SIMULATED-private-pin")
    now, processes = [0.0], []

    async def launch(url, output):
        assert url == URL
        p = Process()
        processes.append(p)
        p.emit(**(failure if len(processes) == 1 and failure else {"graph_count": 1}))
        if failure and len(processes) == 1:
            p.stdout.feed_eof()
        return p

    return module.GeekDaemon(tmp_path, launcher=launch, clock=lambda: now[0]), processes, now


@pytest.mark.asyncio
async def test_worker_crash_after_long_health_waits_then_recovers_with_paired_private_logs(tmp_path):
    daemon, processes, now = make(tmp_path)
    try:
        await daemon.connect(URL)
        await settle()
        now[0] = 3600
        processes[0].emit(graph_count=1)
        await settle()
        await daemon.tick()
        processes[0].stdout.feed_eof()
        await settle()
        now[0] = 3601
        await daemon.tick()
        assert not daemon.intent.automatic_retry_blocked
        assert len(processes) == 1 and daemon.status()["status"] == "worker_stopped"
        now[0] = 3900
        await daemon.tick()
        assert len(processes) == 1
        now[0] = 3901
        await daemon.tick()
        await settle()
        await daemon.tick()
        assert len(processes) == 2 and processes[0].returncode is not None
        assert daemon.status()["status"] == "online" and daemon.status()["gap"]
        rows = list(daemon.runtime_log.rows)
        assert [r["event"] for r in rows] == ["start", "result"]
        assert rows[0]["correlation"] == rows[1]["correlation"]
        assert rows[-1]["result"] == "ok"
        assert not any(secret in json.dumps(rows) for secret in ["SIMULATED-private-pin", URL, "graph_count"])
        assert (tmp_path / "geek-runtime/runtime.jsonl").stat().st_mode & 0o777 == 0o600
    finally:
        await daemon.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["connection_refused", "network_unreachable", "connection_timeout"])
async def test_explicit_transport_failure_during_authentication_can_recover(tmp_path, code):
    daemon, processes, now = make(tmp_path, {"status": "blocked", "failure_phase": "authenticate", "failure_code": code})
    try:
        await daemon.connect(URL)
        await settle()
        await daemon.tick()
        assert not daemon.intent.authentication_blocked and not daemon.intent.automatic_retry_blocked
        assert len(processes) == 1
        now[0] = 300
        await daemon.tick()
        await settle()
        await daemon.tick()
        assert len(processes) == 2 and daemon.status()["status"] == "online"
    finally:
        await daemon.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["gateway_error_frame", "gateway_closed_connection", "authentication_timeout", "authentication_handshake_failed", "check_failed"])
async def test_authentication_rejection_or_unknown_never_retries(tmp_path, code):
    daemon, processes, now = make(tmp_path, {"status": "blocked", "failure_phase": "authenticate", "failure_code": code})
    try:
        await daemon.connect(URL)
        await settle()
        await daemon.tick()
        now[0] = 100000
        await daemon.tick()
        assert len(processes) == 1 and daemon.intent.authentication_blocked
        saved = json.loads(daemon.intent_path.read_text())
        assert saved["authentication_blocked"] and saved["failure_code"] == code
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_malformed_worker_output_remains_blocked(tmp_path):
    daemon, processes, now = make(tmp_path)
    try:
        await daemon.connect(URL)
        await settle()
        processes[0].stdout.feed_data(b'{"arbitrary":"SIMULATED-private-pin"}\n')
        await settle()
        await daemon.tick()
        now[0] = 100000
        await daemon.tick()
        assert len(processes) == 1 and daemon.intent.automatic_retry_blocked
        assert daemon.status()["failure_code"] == "check_failed"
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_stop_cancels_pending_recovery_and_disk_failure_is_visible(tmp_path, monkeypatch):
    daemon, processes, now = make(tmp_path, {"status": "blocked", "failure_phase": "inventory", "failure_code": "connection_timeout"})
    try:
        await daemon.connect(URL)
        await settle()
        await daemon.tick()
        monkeypatch.setattr(daemon.runtime_log, "_append", lambda _: (_ for _ in ()).throw(OSError()))
        now[0] = 300
        await daemon.tick()
        assert daemon.runtime_log.export()["code"] == "logging_unavailable"
        await daemon.disconnect()
        assert daemon.runtime_log.rows[-1]["result"] == "cancelled"
        now[0] = 100000
        await daemon.tick()
        assert len(processes) == 2 and not daemon.intent.keep_connected
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_readonly_diagnostics_ipc_and_old_status_client_compatibility(monkeypatch):
    from homeplane.geek_background import GeekBackground
    repo = ROOT
    # AF_UNIX paths must also fit when the checkout is a deep frozen export.
    with tempfile.TemporaryDirectory(prefix="gk-", dir=Path(tempfile.gettempdir()).resolve()) as name:
        daemon, processes, now = make(Path(name))
        from homeplane.geek_background_contracts import BackgroundInstallation
        module.atomic_private(Path(name) / "geek-background-installed.json", BackgroundInstallation(use_for_connections=True).model_dump_json())
        await daemon.start()
        try:
            monkeypatch.setattr(daemon.runtime_log, "_append", lambda _: (_ for _ in ()).throw(OSError()))
            for _ in range(80):
                daemon.runtime_log.record("infrastructure.reconcile", "result", result="unavailable", code="transport_error")
            old_view = await GeekBackground(name).refresh()
            assert old_view["background_service_live"] and old_view["status"] == "stopped"
            assert "logging_available" not in old_view
            reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
            writer.write(b'{"operation":"diagnostics"}\n')
            await writer.drain()
            view = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            assert view["code"] == "logging_unavailable" and view["disk_available"] is False
            assert len(view["events"]) == 64 and not view["automatic_upload"]
            assert "SIMULATED-private-pin" not in json.dumps(view) and URL not in json.dumps(view)
            assert not processes and not daemon.intent.keep_connected
        finally:
            await daemon.close()
