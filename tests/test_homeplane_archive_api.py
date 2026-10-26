"""SIMULATED Console/readonly-adapter contract paths; no household storage used."""

import asyncio
import sqlite3

import httpx
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.settings import SettingsService
from homeplane.simulator import Simulator

ORIGIN = "http://127.0.0.1:8740"
HEADERS = {"Origin": ORIGIN}


class SimulatedReadonlyNamespace:
    def __init__(self):
        self.fixture = Simulator()

    async def snapshot(self):
        return (await self.fixture.snapshot()).model_copy(update={"mode": "ha-read-only"})

    def health(self):
        return {"transport": "online"}

    async def close(self):
        pass


def test_simulator_api_origin_limits_and_no_effects(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app, base_url=ORIGIN) as client:
        context = client.get("/api/archive/context").json()
        assert context["simulated"] and context["read_only"]
        camera = context["cameras"][0]
        body = {
            "expected_context": context["fingerprint"],
            "camera_id": camera["id"],
            "time_range": camera["example_ranges"]["pending"],
        }
        assert client.post("/api/archive/resolve", json=body).status_code == 403
        assert (
            client.post(
                "/api/archive/resolve", json={**body, "path": "/etc/passwd"}, headers=HEADERS
            ).status_code
            == 422
        )
        report = client.post("/api/archive/resolve", json=body, headers=HEADERS).json()
        assert report["status"] == "pending" and report["simulated"]
        changed = client.post(
            "/api/archive/simulator",
            json={"expected_context": context["fingerprint"], "scenario": "late-arrives"},
            headers=HEADERS,
        ).json()
        assert client.post("/api/archive/resolve", json=body, headers=HEADERS).status_code == 409
        body["expected_context"] = changed["fingerprint"]
        report = client.post("/api/archive/resolve", json=body, headers=HEADERS).json()
        assert (
            report["status"] == "ready"
            and not report["media_copied"]
            and not report["device_write_attempted"]
        )
        assert (
            app.state.core.store.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0] == 0
        )
        assert "archive_resolution" in client.get("/api/contracts").json()


def test_unconfigured_and_real_namespace_cannot_simulate(tmp_path):
    app = create_app(
        SimulatedReadonlyNamespace(),
        db_path=tmp_path / "SIMULATED.sqlite",
        settings_path=tmp_path / "settings.json",
    )
    with TestClient(app, base_url=ORIGIN) as client:
        context = client.get("/api/archive/context").json()
        assert not context["configured"] and not context["simulated"]
        body = {"expected_context": context["fingerprint"]}
        assert (
            client.post("/api/archive/scan", json=body, headers=HEADERS).json()["code"]
            == "archive_not_configured"
        )
        response = client.post(
            "/api/archive/simulator", json={**body, "scenario": "late-arrives"}, headers=HEADERS
        )
        assert response.status_code == 409


def test_local_index_failure_has_bounded_error_and_does_not_block_devices(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app, base_url=ORIGIN) as client:
        def broken_context():
            raise sqlite3.OperationalError("SIMULATED private database failure")

        monkeypatch.setattr(app.state.archive, "context", broken_context)
        response = client.get("/api/archive/context")
        assert response.status_code == 503 and response.json() == {"code": "archive_unavailable"}
        assert client.get("/api/snapshot").status_code == 200


def test_slow_archive_scan_does_not_own_console_control_lock(tmp_path):
    settings = SettingsService(tmp_path / "settings.json")
    settings.update({"nas_path": str(tmp_path)})

    async def check():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def runner(*_):
            entered.set()
            await finish.wait()
            return {"status": "readable", "files": [], "folders": [], "scan_complete": True}

        app = create_app(
            SimulatedReadonlyNamespace(),
            db_path=tmp_path / "SIMULATED.sqlite",
            settings_service=settings,
            archive_runner=runner,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS
        ) as client:
            context = (await client.get("/api/archive/context")).json()
            task = asyncio.create_task(
                client.post("/api/archive/scan", json={"expected_context": context["fingerprint"]})
            )
            await asyncio.wait_for(entered.wait(), 1)
            state = await asyncio.wait_for(client.get("/api/snapshot"), 0.5)
            assert state.status_code == 200 and app.state.archive.lock.locked()
            busy = await client.post("/api/archive/scan", json={"expected_context": context["fingerprint"]})
            assert busy.status_code == 409 and busy.json()["code"] == "archive_busy"
            finish.set()
            assert (await task).status_code == 200
        await app.state.archive.close()
        app.state.core.actions.close()
        app.state.rules.close()
        app.state.core.store.close()

    asyncio.run(check())
