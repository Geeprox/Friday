import asyncio
import hashlib
import io
import json
import sqlite3
import threading
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.core import HomeCore
from homeplane.diagnostic_export import console_origin, download, save, validate_download
from homeplane.diagnostics import (
    MAX_BYTES,
    Diagnostics,
    DiagnosticsPolicy,
    DiagnosticsReport,
    JournalSummary,
    bundle,
    journal_summary,
    project,
)
from homeplane.simulator import Simulator
from homeplane.store import TraceStore

SECRET = "SIMULATED_PRIVATE_TOKEN_ROOM_TRANSCRIPT_DO_NOT_EXPORT"
ORIGIN = {"Origin": "http://testserver"}


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["report.json", "manifest.json"]
        raw = archive.read("report.json")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["files"] == [
            {"name": "report.json", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        ]
        return json.loads(raw)


def report():
    return project(None, "simulator", {}, {}, JournalSummary(status="missing", sampled_rows={}))


def test_export_is_allowlisted_and_does_not_refresh_or_mutate(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "traces.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app) as client:
        client.post("/api/simulator/scenario/stale-temperature", headers=ORIGIN)
        core = app.state.core
        owner, cached = core._diagnostic_snapshot
        cached = cached.model_copy(
            update={
                "entities": [
                    e.model_copy(update={"name": SECRET, "id": SECRET, "room": SECRET})
                    for e in cached.entities
                ],
                "rules": [r.model_copy(update={"raw": SECRET}) for r in cached.rules],
            }
        )
        core._diagnostic_snapshot = (owner, cached)
        app.state.geek_session.setup.save(SECRET)
        app.state.geek_session.report = {
            "status": "blocked",
            "failure_phase": "authenticate",
            "failure_code": "gateway_error_frame",
        }
        core.store.connection.execute("INSERT INTO events VALUES (?,?,?)", (SECRET, "2000-01-01", SECRET))
        core.store.connection.execute(
            "INSERT INTO light_actions VALUES (?,?,?,?,?)", (SECRET, SECRET, "2000-01-01", SECRET, SECRET)
        )
        core.store.connection.commit()
        before = {
            table: list(core.store.connection.execute(f"SELECT * FROM {table}"))
            for table in ("events", "traces", "light_actions")
        }

        async def forbidden(*args, **kwargs):
            raise AssertionError("Export must not call a provider, reauthenticate, or sample hardware")

        monkeypatch.setattr(core.adapter, "snapshot", forbidden)
        monkeypatch.setattr(app.state.arl, "status", forbidden)
        monkeypatch.setattr(app.state.geek_session, "connect", forbidden)
        response = client.post("/api/diagnostics/export", headers=ORIGIN, json={"redacted": True})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "no-store"
        assert len(response.content) <= MAX_BYTES
        assert SECRET.encode() not in response.content
        exported = unpack(response.content)
        assert exported["source"]["simulated"] is True
        assert exported["source"]["quality"]["stale"] > 0
        assert exported["geek"]["failure_code"] == "gateway_error_frame"
        assert exported["journal"]["sampled_action_stages"] == {"unknown": 1}
        assert exported["policy"]["server_retention_seconds"] == 0
        assert exported["policy"]["automatic_upload"] is False
        assert exported["real_writes_attempted"] is False
        assert before == {
            table: list(core.store.connection.execute(f"SELECT * FROM {table}")) for table in before
        }
        assert not list(tmp_path.rglob("*.zip"))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"redacted": False},
        {"redacted": 1},
        {"redacted": "true"},
        {"redacted": True, "include_logs": SECRET},
    ],
)
def test_export_requires_explicit_redaction_and_rejects_extra_data(tmp_path, body):
    with TestClient(
        create_app(db_path=tmp_path / "traces.sqlite", settings_path=tmp_path / "settings.json")
    ) as client:
        response = client.post("/api/diagnostics/export", json=body, headers=ORIGIN)
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request"}
        assert SECRET not in response.text


def test_export_requires_origin_and_current_owner_cache(tmp_path):
    app = create_app(db_path=tmp_path / "traces.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app) as client:
        client.get("/api/snapshot")
        app.state.core.adapter = Simulator()
        assert client.post("/api/diagnostics/export", json={"redacted": True}).status_code == 403
        assert (
            client.post(
                "/api/diagnostics/export",
                headers={"Origin": "https://foreign.example"},
                json={"redacted": True},
            ).status_code
            == 403
        )
        response = client.post("/api/diagnostics/export", headers=ORIGIN, json={"redacted": True})
        assert unpack(response.content)["source"]["snapshot_available"] is False
        policy = client.get("/api/diagnostics").json()
        assert policy["downloaded_file_retention"] == "user_managed"
        assert policy["collection_basis"] == "cached_local_state_no_upstream_refresh"


def test_unknown_and_malformed_upstream_fields_never_escape():
    data = project(
        None,
        [],
        {"transport": [], "ha_version": SECRET, "continuous_subscription": SECRET, "error": SECRET},
        {
            "status": SECRET,
            "connection_host": SECRET,
            "failure_code": [],
            "failure_phase": SECRET,
            "network_errno": SECRET,
            "session_live": SECRET,
            "url": SECRET,
        },
        report().journal,
    )
    assert SECRET.encode() not in bundle(data)
    assert data.source.mode == "unavailable"
    assert data.ha.version is None and data.ha.transport == "unavailable"
    assert data.geek.status == "unknown" and data.geek.failure_code is None


def test_journal_read_is_bounded_missing_and_corrupt_are_visible(tmp_path):
    missing = tmp_path / "missing.sqlite"
    assert journal_summary(missing).status == "missing"
    assert not missing.exists()
    broken = tmp_path / "broken.sqlite"
    broken.write_text(SECRET)
    assert journal_summary(broken).status == "unavailable"
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as database:
        for table in ("events", "traces", "light_actions"):
            database.execute(f"CREATE TABLE {table} (status TEXT, body TEXT)")
        database.executemany("INSERT INTO events VALUES (?,?)", [("accepted", SECRET)] * 1005)
        database.execute("INSERT INTO light_actions VALUES (?,?)", ("unknown_outcome", SECRET))
    result = journal_summary(path)
    assert result.sampled_rows["events"] == 1000 and result.truncated
    assert result.sampled_action_stages == {"unknown_outcome": 1}
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT count(*) FROM events").fetchone()[0] == 1005


@pytest.mark.asyncio
async def test_slow_cancelled_export_has_one_collector_and_does_not_block_core(tmp_path, monkeypatch):
    import homeplane.diagnostics as module

    started, finish = threading.Event(), threading.Event()

    def slow(path):
        started.set()
        finish.wait(3)
        return report().journal

    monkeypatch.setattr(module, "journal_summary", slow)
    exporter = Diagnostics(tmp_path / "unused.sqlite")
    job = asyncio.create_task(exporter.export(None, "simulator", {}, {}))
    try:
        await asyncio.to_thread(started.wait, 1)
        store = TraceStore(tmp_path / "core.sqlite")
        core = HomeCore(Simulator(), store)
        snapshot = await asyncio.wait_for(core.snapshot(), 0.5)
        assert snapshot.mode == "simulator"
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        with pytest.raises(ValueError, match="diagnostics_busy"):
            await exporter.export(None, "simulator", {}, {})
        assert exporter.task is not None and not exporter.task.done()
        store.close()
    finally:
        finish.set()
        if exporter.task:
            await exporter.task


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8740",
        "http://192.168.50.1:8086",
        "http://127.0.0.1:8740?token=" + SECRET,
        "http://user:" + SECRET + "@127.0.0.1:8740",
        "http://127.0.0.1:8740/other",
        "http://127.0.0.1:bad",
    ],
)
def test_cli_never_redirects_exports_to_non_console_destination(url):
    with pytest.raises(ValueError, match="local_console_required"):
        console_origin(url)


def test_cli_bounded_download_validates_and_saves_private_exclusive_file(tmp_path):
    data = bundle(report())
    calls = []

    def serve(request):
        calls.append(request)
        assert request.headers["Origin"] == "http://127.0.0.1:8740"
        assert json.loads(request.content) == {"redacted": True}
        return httpx.Response(200, headers={"content-type": "application/zip"}, content=data)

    received = download("http://127.0.0.1:8740", transport=httpx.MockTransport(serve))
    assert len(calls) == 1
    output = tmp_path / "private" / "support.zip"
    saved = save(received, output)
    assert saved["redacted"] and saved["retention"] == "user_managed"
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save(received, output)
    link = tmp_path / "alias.zip"
    link.symlink_to(output)
    with pytest.raises(ValueError, match="local_regular_output_required"):
        save(received, link)


def test_cli_rejects_oversized_unqualified_or_compressed_bundle():
    with pytest.raises(ValueError):
        validate_download(SECRET.encode())
    with pytest.raises(ValueError):
        validate_download(b"x" * (MAX_BYTES + 1))
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", b"x" * 1000000)
        archive.writestr("manifest.json", "{}")
    with pytest.raises(ValueError):
        validate_download(content.getvalue())
    bad = report().model_copy(update={"policy": DiagnosticsPolicy(excluded=(SECRET,))})
    with pytest.raises(ValueError):
        validate_download(bundle(bad))

    def oversized(request):
        return httpx.Response(
            200, headers={"content-type": "application/zip"}, content=b"x" * (MAX_BYTES + 1)
        )

    with pytest.raises(ValueError, match="diagnostics_budget_exceeded"):
        download("http://127.0.0.1:8740", transport=httpx.MockTransport(oversized))


def test_generated_diagnostics_contracts_match():
    from homeplane.diagnostics import DiagnosticsExportRequest

    for model in (DiagnosticsExportRequest, DiagnosticsReport, DiagnosticsPolicy):
        assert (
            json.loads((Path("schemas") / (model.__name__ + ".json")).read_text())
            == model.model_json_schema()
        )


@pytest.mark.parametrize("replaced", [False, True])
def test_failed_cli_write_only_removes_its_own_partial_file(tmp_path, monkeypatch, replaced):
    import homeplane.diagnostic_export as module

    output = tmp_path / "support.zip"
    original = module.os.fdopen

    class FailedWrite:
        def __init__(self, fd):
            self.file = original(fd, "wb")

        def __enter__(self):
            return self

        def write(self, data):
            self.file.write(data[:10])
            if replaced:
                output.unlink()
                output.write_bytes(b"OTHER_SIMULATED_USER_FILE")
            raise OSError(SECRET)

        def __exit__(self, *args):
            self.file.close()

    monkeypatch.setattr(module.os, "fdopen", lambda fd, mode: FailedWrite(fd))
    with pytest.raises(OSError):
        save(bundle(report()), output)
    if replaced:
        assert output.read_bytes() == b"OTHER_SIMULATED_USER_FILE"
    else:
        assert not output.exists()
