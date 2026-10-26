"""Explicit simulated providers over real private IPC; no household gateway/PIN."""
import asyncio
import json
import os
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_homeplane_geek_session import Process, settle, simulated_snapshot

from homeplane.api import create_app
from homeplane.geek_background import MARKER_NAME, GeekBackground, enabled, last_attempt
from homeplane.geek_background_contracts import BackgroundInstallation, ConnectionIntent
from homeplane.geek_daemon import GeekDaemon, atomic_private
from homeplane.geek_service import LABEL, install
from homeplane.geek_session import GeekSession
from homeplane.geek_setup import GeekSetup
from homeplane.settings import SettingsService

URL = "http://192.168.50.1:8086"
ORIGIN = "http://127.0.0.1:8740"


@pytest.fixture
def private(monkeypatch):
    # macOS Unix socket paths are limited; pytest's long test names exceed it.
    with tempfile.TemporaryDirectory(prefix="friday-geek-test-", dir=str(Path("/tmp").resolve())) as name:
        root = Path(name)
        settings = SettingsService(root / "settings.json")
        settings.update({"geek_url": URL})
        GeekSetup(root).save("SIMULATED-private-pin")
        atomic_private(root / MARKER_NAME, BackgroundInstallation(use_for_connections=True).model_dump_json())
        async def simulated_build(*_):
            return Path("SIMULATED-helper-not-executed")
        monkeypatch.setattr("homeplane.geek_service.build_helper", simulated_build)
        monkeypatch.setattr("homeplane.geek_service.PERMISSION_POLLS", 2)
        yield root


@pytest.fixture
def macos_service(monkeypatch):
    # Simulate only the service platform check; every launch/build boundary is mocked.
    monkeypatch.setattr('homeplane.geek_service.platform', SimpleNamespace(system=lambda: 'Darwin'))


def launcher(processes, *, failure=None, snapshot=False):
    async def launch(url, output):
        assert url == URL
        process = Process()
        processes.append(process)
        if snapshot:
            simulated_snapshot(output)
        process.emit(**(failure or {"graph_count": 1}))
        if failure:
            process.stdout.feed_eof()
        return process
    return launch


@pytest.mark.asyncio
async def test_console_lifecycle_does_not_disconnect_independent_owner(private):
    processes = []
    daemon = GeekDaemon(private, launcher=launcher(processes, snapshot=True))
    await daemon.start()
    try:
        first = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
        async with first.router.lifespan_context(first):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url=ORIGIN) as client:
                assert (await client.get("/api/settings/geek/session")).json()["status"] == "stopped"
                await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})
                await settle()
                view = (await client.get("/api/settings/geek/inventory")).json()
                assert view["status"] == "ready" and len(view["graphs"]) == 1
                assert view["session"]["connection_host"] == "background_service"
                assert view["session"]["keep_connected"]
                assert "SIMULATED-private-pin" not in json.dumps(view)
        assert processes[0].returncode is None and len(processes) == 1
        second = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
        async with second.router.lifespan_context(second):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url=ORIGIN) as client:
                assert (await client.get("/api/settings/geek/session")).json()["status"] == "online"
                assert (await client.post("/api/settings/geek/probe", headers={"origin": ORIGIN})).json()["status"] == "online"
                assert len(processes) == 1
                result = await client.post("/api/settings/geek/disconnect", headers={"origin": ORIGIN})
                assert result.json()["status"] == "stopped"
        assert processes[0].returncode == 0 and not daemon.intent.keep_connected
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_service_restart_only_restores_exact_readonly_intent_with_gap(private):
    processes = []
    daemon = GeekDaemon(private, launcher=launcher(processes))
    await daemon.start()
    await daemon.connect(URL)
    await settle()
    await daemon.close()
    assert processes[0].returncode == 0
    intent = ConnectionIntent.model_validate_json((private / "geek-connection-intent.json").read_bytes())
    assert intent.keep_connected and set(intent.model_dump()) == {
        "schema_version", "keep_connected", "url", "credential_stamp", "authentication_blocked", "automatic_retry_blocked",
        "failure_status", "failure_phase", "failure_code"}
    replacement = GeekDaemon(private, launcher=launcher(processes))
    await replacement.start()
    try:
        await settle()
        assert len(processes) == 2 and replacement.status()["status"] == "online"
        assert replacement.status()["gap"]
        await replacement.disconnect()
    finally:
        await replacement.close()
    final = GeekDaemon(private, launcher=launcher(processes))
    await final.start()
    try:
        assert len(processes) == 2 and not final.intent.keep_connected
    finally:
        await final.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["endpoint", "credential"])
async def test_out_of_band_changes_close_owner_and_never_retarget_credential(private, change):
    processes = []
    daemon = GeekDaemon(private, launcher=launcher(processes))
    await daemon.start()
    try:
        await daemon.connect(URL)
        await settle()
        if change == "endpoint":
            SettingsService(private / "settings.json").update({"geek_url": "http://192.168.50.2:8086"})
        else:
            GeekSetup(private).save("SIMULATED-next-pin")
        await daemon.tick()
        assert processes[0].returncode == 0 and len(processes) == 1
        assert not daemon.intent.keep_connected
    finally:
        await daemon.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"status": "blocked", "failure_phase": "authenticate", "failure_code": "gateway_error_frame"},
    {"status": "blocked", "failure_phase": "inventory", "failure_code": "check_failed"},
])
async def test_failure_latch_survives_service_restart_no_repeated_pin(private, failure):
    processes = []
    daemon = GeekDaemon(private, launcher=launcher(processes, failure=failure))
    await daemon.start()
    await daemon.connect(URL)
    await settle()
    await daemon.close()  # Shutdown itself captures failure without waiting for monitor.
    replacement = GeekDaemon(private, launcher=launcher(processes))
    await replacement.start()
    try:
        await replacement.tick()
        assert len(processes) == 1
        assert replacement.intent.authentication_blocked or replacement.intent.automatic_retry_blocked
        assert replacement.status()["status"] == "blocked"
        assert replacement.status()["failure_code"] == failure["failure_code"]
        await replacement.connect(URL)  # Explicit user retry clears latch.
        await settle()
        assert len(processes) == 2
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_transport_retry_backoff_then_auth_rejection_stops_retry(private):
    now, processes = [0.0], []
    failure = {"status": "blocked", "failure_phase": "websocket_connect", "failure_code": "connection_refused"}
    daemon = GeekDaemon(private, launcher=launcher(processes, failure=failure), clock=lambda: now[0])
    await daemon.start()
    try:
        await daemon.connect(URL)
        await settle()
        await daemon.tick()
        assert len(processes) == 1
        now[0] = 301
        daemon.session.launcher = launcher(processes, failure={"status": "blocked", "failure_phase": "authenticate", "failure_code": "gateway_error_frame"})
        await daemon.tick()
        await settle()
        now[0] = 1000
        await daemon.tick()
        await daemon.tick()
        assert len(processes) == 2 and daemon.intent.authentication_blocked
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_second_owner_cannot_remove_first_socket(private):
    first = GeekDaemon(private)
    await first.start()
    second = GeekDaemon(private)
    try:
        with pytest.raises(BlockingIOError):
            await second.start()
        await second.close()
        assert first.socket_path.exists()
        assert (await GeekBackground(private).refresh())["background_service_live"]
    finally:
        await first.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    b'{"operation":"setProperties","url":"http://192.168.50.1"}\n',
    b'{"operation":"status","passcode":"SIMULATED-private-pin"}\n',
    b'{"operation":"connect","url":"http://192.168.50.2:8086"}\n',
    b'{"operation":"status","url":"http://192.168.50.1:8086"}\n',
    b'x' * 1500 + b'\n',
])
async def test_ipc_rejects_untyped_operation_secrets_or_other_destination(private, frame):
    processes = []
    daemon = GeekDaemon(private, launcher=launcher(processes))
    await daemon.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
        writer.write(frame)
        await writer.drain()
        assert json.loads(await reader.readline()) == {"error": "background_request_failed"}
        writer.close()
        await writer.wait_closed()
        assert not processes and not daemon.intent.keep_connected
        assert daemon.socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_invalid_marker_or_missing_owner_never_falls_back_or_claims_stopped(private):
    app = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
    atomic_private(private / MARKER_NAME, '{"installed":true,"arbitrary":"rejected"}')
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            result = (await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})).json()
            assert result["status"] == "background_unavailable" and not result["session_live"]
            assert (await client.post("/api/settings/geek/probe", headers={"origin": ORIGIN})).json()["status"] == "background_unavailable"
            assert (await client.post("/api/settings/geek/credential", headers={"origin": ORIGIN}, json={"passcode": "SIMULATED-next-pin"})).status_code == 400
            assert GeekSetup(private).secret.read_text() == "SIMULATED-private-pin"


@pytest.mark.asyncio
async def test_install_is_fixed_user_job_and_does_not_chmod_shared_agent_directory(private, monkeypatch, macos_service):
    calls = []
    home = private / "SIMULATED-home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True, mode=0o755)
    agents.chmod(0o755)
    unrelated = agents / "SIMULATED-unrelated.plist"
    unrelated.write_text("untouched")
    async def runner(arguments, timeout):
        calls.append(arguments)
        return (1, "") if len(calls) == 1 else (0, "SIMULATED-launchctl")
    async def status(_):
        return {"background_service_live": True, "status": "stopped", "keep_connected": False}
    monkeypatch.setattr(GeekBackground, "refresh", status)
    result = await install(private, home=home, runner=runner, node="/opt/homebrew/bin/node")
    assert not result["keep_connected"]
    path = agents / (LABEL + ".plist")
    definition = plistlib.loads(path.read_bytes())
    assert definition["KeepAlive"] and definition["RunAtLoad"]
    assert definition["ProgramArguments"][0].endswith("FridayGeekConnector.app/Contents/MacOS/FridayGeekConnector")
    assert definition["AssociatedBundleIdentifiers"] == ["com.friday.home.geek-connector"]
    assert "passcode" not in str(definition) and "codex" not in str(definition).lower()
    assert definition["StandardErrorPath"] == "/dev/null"
    assert path.stat().st_mode & 0o777 == 0o600 and agents.stat().st_mode & 0o777 == 0o755
    assert unrelated.read_text() == "untouched"
    assert calls[1] == ["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]
    definition["ProgramArguments"] = ["SIMULATED-unrelated-program"]
    path.write_bytes(plistlib.dumps(definition))
    with pytest.raises(ValueError, match="existing_service_conflict"):
        await install(private, home=home, runner=runner, node="/opt/homebrew/bin/node")


@pytest.mark.asyncio
async def test_failed_bootstrap_never_records_successful_install(private, macos_service):
    (private / MARKER_NAME).unlink()
    async def denied(*_, **__):
        return 1, "SIMULATED-bootstrap-failure"
    with pytest.raises(ValueError, match="service_bootstrap_failed"):
        await install(private, home=private / "SIMULATED-home", runner=denied, node="/opt/homebrew/bin/node")
    assert not (private / MARKER_NAME).exists()
    assert not (private / "geek-connection-intent.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [True, False])
async def test_prepare_background_before_switching_console_owner(private, monkeypatch, succeeds):
    atomic_private(private / MARKER_NAME, BackgroundInstallation().model_dump_json())
    direct, background = [], []
    failure = None if succeeds else {"status": "blocked", "failure_phase": "websocket_connect", "failure_code": "network_unreachable", "network_errno": "EHOSTUNREACH"}
    daemon = GeekDaemon(private, launcher=launcher(background, failure=failure, snapshot=succeeds))
    await daemon.start()
    async def installed(*_):
        return daemon.status()
    monkeypatch.setattr("homeplane.geek_service.install", installed)
    app = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json", geek_launcher=launcher(direct))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})
                await settle()
                assert direct[0].returncode is None
                result = (await client.post("/api/settings/geek/background/install", headers={"origin": ORIGIN})).json()
                assert result["enabled"] is succeeds and enabled(private) is succeeds
                assert result["current_connection_preserved"] is (not succeeds)
                if succeeds:
                    assert direct[0].returncode == 0 and background[0].returncode is None
                    assert (await client.get("/api/settings/geek/session")).json()["connection_host"] == "background_service"
                else:
                    assert direct[0].returncode is None and background[0].returncode == 0
                    assert not daemon.intent.keep_connected
                    assert (await client.get("/api/settings/geek/session")).json()["connection_host"] == "console_child"
        if succeeds:
            assert background[0].returncode is None
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_reinstall_preserves_admitted_background_owner(private, monkeypatch, macos_service):
    async def loaded(*_, **__):
        return 0, "SIMULATED-loaded"
    async def status(_):
        return {"background_service_live": True, "status": "online", "keep_connected": True}
    monkeypatch.setattr(GeekBackground, "refresh", status)
    await install(private, home=private / "SIMULATED-home", runner=loaded, node="/opt/homebrew/bin/node")
    assert enabled(private)


@pytest.mark.asyncio
async def test_background_cleanup_never_deletes_another_live_console_or_legacy_root(private):
    # Regression for the real migration failure. The Console root is older
    # than the background root, so naive newest-root retention would erase it.
    setup = GeekSetup(private)
    roots, processes = [], []
    async def old_console(url, output):
        roots.append(output)
        return await launcher(processes, snapshot=True)(url, output)
    console = GeekSession(setup, launcher=old_console)
    await console.connect(URL)
    await settle()
    legacy = setup.evidence / ("session-" + str(uuid4()))
    legacy.mkdir(mode=0o700)
    old = time.time() - 8 * 86400
    os.utime(legacy, (old, old))
    os.utime(roots[0], (old, old))
    background = GeekDaemon(private, launcher=launcher([], snapshot=True))
    await background.start()
    try:
        await background.connect(URL)
        await settle()
        await background.disconnect()
        background.setup.prune(active=background.session.inventory_root)
        assert roots[0].exists() and legacy.exists()
        assert (await console.inventory()).status == "ready"
        assert processes[0].returncode is None
    finally:
        await background.close()
        await console.close()
    setup.prune()
    assert not roots[0].exists() and legacy.exists()  # Only now is the owned old root eligible.


def test_actual_second_process_holding_lease_prevents_cleanup(private):
    from homeplane.geek_setup import ROOT
    setup = GeekSetup(private)
    root = setup.evidence / ("session-" + str(uuid4()))
    simulated_snapshot(root)
    old = time.time() - 8 * 86400
    os.utime(root, (old, old))
    script = "import sys,os; from pathlib import Path; from homeplane.geek_setup import GeekSetup; root=Path(sys.argv[1]); fd=GeekSetup(root.parent.parent).lease(root); print('SIMULATED lease held',flush=True); sys.stdin.read(1); os.close(fd)"
    process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-c", script, str(root)], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout.readline() == b"SIMULATED lease held\n"
        setup.prune()
        assert root.exists()
    finally:
        process.communicate(b"x", timeout=5)
    setup.prune()
    assert not root.exists()


@pytest.mark.asyncio
async def test_primary_connect_uses_background_once_without_first_console_handshake(private, monkeypatch):
    atomic_private(private / MARKER_NAME, BackgroundInstallation().model_dump_json())
    processes = []
    daemon = GeekDaemon(private, launcher=launcher(processes, snapshot=True))
    await daemon.start()
    async def installed(*_):
        return daemon.status()
    async def forbidden(*_):
        raise AssertionError("must not authenticate through a second Console worker")
    monkeypatch.setattr("homeplane.geek_service.install", installed)
    monkeypatch.setattr(GeekSession, "_launch", forbidden)
    app = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                result = (await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})).json()
                assert result["status"] == "online" and result["connection_host"] == "background_service"
                await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})
                assert len(processes) == 1
        assert processes[0].returncode is None
    finally:
        await daemon.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,code", [("websocket_connect", "network_unreachable"), ("authenticate", "gateway_error_frame")])
async def test_failed_background_preparation_survives_reload_without_repeated_auth(private, monkeypatch, phase, code):
    atomic_private(private / MARKER_NAME, BackgroundInstallation().model_dump_json())
    processes = []
    failure = {"status": "blocked", "failure_phase": phase, "failure_code": code}
    daemon = GeekDaemon(private, launcher=launcher(processes, failure=failure))
    await daemon.start()
    async def installed(*_):
        return daemon.status()
    async def forbidden(*_):
        raise AssertionError("a status read must not launch a Console handshake")
    monkeypatch.setattr("homeplane.geek_service.install", installed)
    monkeypatch.setattr(GeekSession, "_launch", forbidden)
    secret_before = GeekSetup(private).secret.read_bytes()
    try:
        for lifecycle in range(2):
            app = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                    if lifecycle == 0:
                        result = await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})
                        assert result.status_code == 200
                    for path in ("/api/settings", "/api/settings/geek/session", "/api/settings/geek/probe"):
                        response = await client.post(path, headers={"origin": ORIGIN}) if path.endswith("probe") else await client.get(path)
                        report = response.json()["geek_session"] if path == "/api/settings" else response.json()
                        assert report["status"] == "stopped" and not report["session_live"]
                        assert report["background_last_attempt"]["failure_phase"] == phase
                        assert report["background_last_attempt"]["failure_code"] == code
                        assert report["background_last_attempt"]["outcome"] == "failed"
                        assert "SIMULATED-private-pin" not in response.text
                    await daemon.tick()
                    assert len(processes) == 1 and not daemon.intent.keep_connected and not enabled(private)
        assert last_attempt(private)["failure_code"] == code
        assert GeekSetup(private).secret.read_bytes() == secret_before
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_connection_mutations_reject_concurrency_instead_of_queueing_pin_or_stop(private, monkeypatch):
    atomic_private(private / MARKER_NAME, BackgroundInstallation().model_dump_json())
    processes, entered, release = [], asyncio.Event(), asyncio.Event()
    daemon = GeekDaemon(private, launcher=launcher(processes, snapshot=True))
    await daemon.start()
    async def installing(*_):
        entered.set()
        await release.wait()
        return daemon.status()
    monkeypatch.setattr("homeplane.geek_service.install", installing)
    app = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
    secret_before = GeekSetup(private).secret.read_bytes()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                first = asyncio.create_task(client.post("/api/settings/geek/connect", headers={"origin": ORIGIN}))
                await asyncio.wait_for(entered.wait(), 1)
                for path, body in (("/api/settings/geek/connect", {}), ("/api/settings/geek/disconnect", {}),
                                   ("/api/settings/geek/credential", {"passcode": "SIMULATED-next-pin"})):
                    response = await client.post(path, headers={"origin": ORIGIN}, json=body)
                    assert response.status_code == 409 and response.json()["detail"] == "geek_operation_in_progress"
                response = await client.put("/api/settings", headers={"origin": ORIGIN}, json={"geek_url": "http://192.168.50.2:8086"})
                assert response.status_code == 409
                assert GeekSetup(private).secret.read_bytes() == secret_before
                release.set()
                assert (await first).json()["status"] == "online"
                assert len(processes) == 1 and daemon.intent.keep_connected
                assert (await client.get("/api/settings/geek/session")).json()["status"] == "online"
    finally:
        release.set()
        await daemon.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["endpoint", "credential"])
async def test_first_connection_without_saved_pairing_never_installs_or_authenticates(private, monkeypatch, missing):
    atomic_private(private / MARKER_NAME, BackgroundInstallation().model_dump_json())
    if missing == "endpoint":
        SettingsService(private / "settings.json").update({"geek_url": None})
    else:
        GeekSetup(private).secret.unlink()
    async def forbidden(*_):
        raise AssertionError("incomplete pairing must not install or send a PIN")
    monkeypatch.setattr("homeplane.geek_service.install", forbidden)
    monkeypatch.setattr(GeekSession, "_launch", forbidden)
    app = create_app(db_path=private / "trace.sqlite", settings_path=private / "settings.json")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            report = (await client.post("/api/settings/geek/connect", headers={"origin": ORIGIN})).json()
            assert report["status"] == "pairing_required" and not report["session_live"]
            assert not (private / "geek-connection-intent.json").exists()


@pytest.mark.asyncio
async def test_non_macos_install_refuses_before_touching_jobs(private, monkeypatch):
    from unittest.mock import AsyncMock
    runner = AsyncMock()
    monkeypatch.setattr('homeplane.geek_service.platform', SimpleNamespace(system=lambda: 'Linux'))
    with pytest.raises(ValueError, match='macos_required'):
        await install(private, runner=runner)
    runner.assert_not_awaited()
