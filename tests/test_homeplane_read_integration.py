"""F1 -> F2 -> F3 over the actual app, with synthetic-only private inputs."""

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeplane.api import create_app
from homeplane.nas_connection import (
    NASConnectionConfig,
    NASConnectionError,
    NASConnectionManager,
    NASCredentials,
)
from homeplane.query import HomeQueryAdoptionConfig
from homeplane.query_contracts import HomeQuery, ReadScope

PHOTO = b"SIMULATED integration photo bytes, no household pixels"
VIDEO = b"SIMULATED integration video bytes, no camera recording"
PHOTO_HASH = hashlib.sha256(PHOTO).hexdigest()
VIDEO_HASH = hashlib.sha256(VIDEO).hexdigest()
SOURCE = "SIMULATED-family"
HEADERS = {"origin": "http://testserver"}


class SyntheticTransport:
    def __init__(self):
        self.calls, self.closed = [], False
        self.files = {"SIMULATED-photo.jpg": PHOTO, "SIMULATED-copy.jpg": PHOTO, "SIMULATED-video.mp4": VIDEO}

    async def connect(self, config, credentials):
        assert config.host == "synthetic.invalid"
        assert credentials == NASCredentials("SIMULATED-user", "SIMULATED-secret")
        return {"reachable": True, "authenticated": True, "readable": True}

    def entry(self, path):
        if path not in self.files:
            raise NASConnectionError("nas_missing")
        content = self.files[path]
        return {"path": path, "kind": "file", "size": len(content), "modified_ns": 10,
                "revision": hashlib.sha256(content + b"SIMULATED revision").hexdigest()}

    async def request(self, operation, payload):
        self.calls.append(operation)
        if operation == "health":
            return {"reachable": True, "authenticated": True, "readable": True}
        if operation == "list":
            start = int(payload.get("cursor") or 0)
            names = sorted(self.files)
            end = start + payload["limit"]
            return {"entries": [self.entry(name) for name in names[start:end]],
                    "cursor": str(end) if end < len(names) else None, "complete": end >= len(names)}
        if operation == "stat":
            return self.entry(payload["path"])
        if operation == "read":
            assert self.entry(payload["path"])["revision"] == payload["revision"]
            data = self.files[payload["path"]][payload["offset"]:payload["offset"] + payload["length"]]
            return {"data": base64.b64encode(data).decode()}
        raise AssertionError(operation)

    async def close(self):
        self.closed = True


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    identity = pytest.importorskip("arl.identity")
    verifier = identity.RegisteredPeerVerifier({"mio"})
    state = {"peer": True, "scope": ReadScope(
        subject="mio", revision="SIMULATED-approved-read", boundary="registered_peer",
        expires_at=datetime.now(UTC) + timedelta(minutes=10), permitted=True,
        family_media_sources=frozenset({SOURCE}),
    )}
    monkeypatch.setattr(verifier, "is_registered", lambda app, pid: state["peer"] and app == "mio" and pid == 999999)
    monkeypatch.setattr(verifier, "register", lambda *args, **kwargs: pytest.fail("no real peer registration"))
    transports, auth_calls = [], []

    async def resolver(ref):
        auth_calls.append(ref)
        assert ref == "SIMULATED-credential"
        return NASCredentials("SIMULATED-user", "SIMULATED-secret")

    def factory():
        transport = SyntheticTransport()
        transports.append(transport)
        return transport

    manager = NASConnectionManager(tmp_path / "nas-connection.json", credential_resolver=resolver,
                                   transport_factory=factory)
    config = HomeQueryAdoptionConfig("mio", 999999, verifier, lambda: state["scope"])
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json",
                     nas_manager=manager, family_media_simulated=True, query_adoption_config=config)
    now = [datetime.now(UTC)]
    app.state.family_media.clock = lambda: now[0]
    with TestClient(app) as client:
        yield app, client, manager, transports, auth_calls, state, now
    assert manager.store.fd is None and app.state.family_media.closed and app.state.queries.closed
    assert app.state.query_adoption.closed
    assert all(transport.closed for transport in transports)


def enable(integrated):
    _, client, _, _, auth_calls, _, _ = integrated
    config = NASConnectionConfig(source_ref=SOURCE, transport="smb", host="synthetic.invalid",
                                 share="SIMULATED-share", credential_ref="SIMULATED-credential",
                                 enabled=True, health_interval_seconds=300)
    result = client.put("/api/settings/nas/connection", json={"expected_generation": 0,
                        "config": config.model_dump(mode="json")}, headers=HEADERS)
    assert result.status_code == 200
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if client.get("/api/settings/nas/connection").json()["state"] == "online":
            break
        time.sleep(0.005)
    else:
        pytest.fail("synthetic NAS manager did not become online")
    assert auth_calls == ["SIMULATED-credential"]


def ready_catalog(integrated):
    _, client, _, _, _, _, now = integrated
    enable(integrated)
    context = client.get("/api/family-media/context").json()
    first = client.post("/api/family-media/scan", json={"expected_context": context["fingerprint"]}, headers=HEADERS)
    assert first.status_code == 200 and first.json()["hashes_completed"] == 0
    page = client.post("/api/family-media/query", json={"expected_context": first.json()["context"]["fingerprint"]}, headers=HEADERS).json()
    assert all(item["status"] == "pending" and item["asset_id"] is None for item in page["items"])
    now[0] += timedelta(seconds=3)
    reviewed = client.post("/api/family-media/source-review", json={"expected_context": page["context_fingerprint"],
                           "completion_convention": "closed_files_operator_reviewed"}, headers=HEADERS)
    assert reviewed.status_code == 200
    scanned = client.post("/api/family-media/scan", json={"expected_context": reviewed.json()["fingerprint"]}, headers=HEADERS)
    assert scanned.status_code == 200 and scanned.json()["hashes_completed"] == 3
    return scanned.json()["context"]["fingerprint"]


def request_for(context, *, limit=20, refs=None):
    return HomeQuery(kind="evidence", source="family_media", source_ref=SOURCE, expected_context=context,
                     evidence_refs=refs or ["family-media:" + PHOTO_HASH, "family-media:" + VIDEO_HASH], limit=limit)


def peer_read(app, client, request, **kwargs):
    return client.portal.call(lambda: app.state.query_adoption.read(
        {"request_json": request.model_dump_json()}, deadline=time.monotonic() + 1, **kwargs,
    ))


def test_actual_pipeline_defaults_disabled_shared_owner_and_no_implicit_reads(integrated):
    app, client, manager, transports, auth_calls, _, _ = integrated
    assert client.get("/api/settings/nas/connection").json()["state"] == "not_configured"
    assert not client.get("/api/family-media/context").json()["source_online"]
    assert client.get("/api/query/contracts").json()["consumer_adoption"]["peer_verified"] is None
    assert app.state.family_media.reader is manager
    assert app.state.family_media.connection is app.state.core.store.connection
    assert app.state.queries.family_media is app.state.family_media
    assert not transports and not auth_calls
    assert client.get("/api/snapshot").status_code == 200


def test_actual_reader_catalog_query_and_arl_consumer_with_exact_hashes(integrated):
    app, client, _, transports, _, _, _ = integrated
    context = ready_catalog(integrated)
    before = list(transports[0].calls)
    request = request_for(context)
    console = client.post("/api/query", json=request.model_dump(mode="json"), headers=HEADERS).json()
    report = peer_read(app, client, request)
    assert console["source"] == report["source"] == "family_media"
    assert report["status"] == "partial" and report["simulated"]
    assert report["permission_scope"]["requested_source_ref"] == SOURCE
    photo, video = report["items"]
    assert photo["original_hash"] == PHOTO_HASH and photo["original_count"] == photo["ready_original_count"] == 2
    assert video["original_hash"] == VIDEO_HASH and video["original_count"] == 1
    assert all(item["captured_at"] is None and item["hardware_sample_time"] is None
               and item["physical_copy_count"] is None and item["causal_relation"] == "unknown"
               and item["confidence"] is None and not item["immutable_source_verified"] for item in report["items"])
    assert all(original["original_ref"]["source_ref"] == SOURCE for original in photo["originals"])
    assert not report["household_media_sent"] and not report["execution_authority"]
    assert transports[0].calls == before, "query must never enumerate, stat or read"
    encoded = json.dumps(report)
    assert all(secret not in encoded for secret in ["SIMULATED-photo.jpg", "synthetic.invalid", "SIMULATED-secret", PHOTO.decode()])


def test_family_source_permission_is_separate_from_room_or_all_home_grant(integrated):
    app, client, _, transports, _, state, _ = integrated
    context = ready_catalog(integrated)
    state["scope"] = replace(state["scope"], all_home=True, history=True, family_media_sources=frozenset())
    before = list(transports[0].calls)
    report = peer_read(app, client, request_for(context))
    assert report["code"] == "family_media_permission_required" and not report["items"]
    assert transports[0].calls == before


def test_cross_source_hash_aliases_do_not_expand_scoped_counts(integrated):
    app, client, _, _, _, _, _ = integrated
    context = ready_catalog(integrated)

    def inject_other_source_metadata():
        connection = app.state.family_media.connection
        row = json.loads(connection.execute("SELECT body FROM family_media_items WHERE original_hash=? LIMIT 1", (PHOTO_HASH,)).fetchone()[0])
        row.update(item_id="f" * 64, source_ref="SIMULATED-unapproved-source")
        with connection:
            connection.execute("INSERT INTO family_media_items VALUES (?,?,?,?,?,?)",
                               (row["item_id"], row["source_ref"], row["binding"], row["scope"], PHOTO_HASH, json.dumps(row)))

    client.portal.call(inject_other_source_metadata)
    owner = client.post("/api/family-media/asset", json={"expected_context": context, "asset_id": PHOTO_HASH}, headers=HEADERS).json()
    assert owner["original_count"] == 3  # Synthetic negative fixture: owner-wide local metadata.
    report = peer_read(app, client, request_for(context, refs=["family-media:" + PHOTO_HASH]))
    assert report["items"][0]["original_count"] == 2
    assert "SIMULATED-unapproved-source" not in json.dumps(report)


def test_family_cursor_age_and_source_change_never_preserve_ready(integrated):
    app, client, _, transports, _, _, now = integrated
    context = ready_catalog(integrated)
    request = request_for(context, limit=1)
    first = peer_read(app, client, request)
    assert first["next_cursor"]
    before = list(transports[0].calls)
    now[0] += timedelta(seconds=61)
    second = peer_read(app, client, request.model_copy(update={"cursor": first["next_cursor"]}))
    assert second["items"][0]["status"] == "stale" and second["items"][0]["ready_original_count"] == 0
    assert transports[0].calls == before
    client.post("/api/settings/nas/connection/disable", json={"expected_generation": 1}, headers=HEADERS)
    changed = peer_read(app, client, request)
    assert changed["code"] == "query_context_changed" and not changed["items"]


def test_revocation_during_metadata_read_and_cancellation_discard_results(integrated, monkeypatch):
    app, client, _, transports, _, state, _ = integrated
    context = ready_catalog(integrated)
    request = request_for(context)
    before = list(transports[0].calls)
    cancelled = peer_read(app, client, request, cancelled=lambda: True)
    assert cancelled["code"] == "query_cancelled" and not cancelled["items"]
    original = app.state.family_media.asset_evidence

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        state["peer"] = False
        return result

    monkeypatch.setattr(app.state.family_media, "asset_evidence", revoke)
    report = peer_read(app, client, request)
    assert report["code"] == "query_context_changed" and not report["items"] and not app.state.queries.pages
    assert transports[0].calls == before


def test_family_router_inherits_origin_body_and_validation_redaction(integrated):
    _, client, _, transports, _, _, _ = integrated
    context = client.get("/api/family-media/context").json()["fingerprint"]
    assert client.post("/api/family-media/scan", json={"expected_context": context}).status_code == 403
    assert client.post("/api/family-media/scan", json={"expected_context": context},
                       headers={"origin": "https://synthetic.invalid"}).status_code == 403
    assert client.get("/api/family-media/context", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    large = client.post("/api/family-media/query", content=b"x" * 16385, headers=HEADERS)
    assert large.status_code == 413
    invalid = client.post("/api/family-media/scan", json={"expected_context": "SIMULATED-secret"}, headers=HEADERS)
    assert invalid.status_code == 422 and invalid.json() == {"code": "invalid_request"}
    assert not transports


def test_corrupt_nas_state_keeps_catalog_and_household_core_available(tmp_path):
    (tmp_path / "nas-connection.json").write_text("SIMULATED invalid source state")
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app) as client:
        assert app.state.nas_connection is None
        assert client.get("/api/settings/nas/connection").json()["failure"] == "nas_invalid_request"
        context = client.get("/api/family-media/context").json()
        assert not context["source_online"] and context["source_ref"] is None
        assert client.get("/api/snapshot").status_code == 200
    assert app.state.family_media.closed


def test_lifespan_exception_still_closes_shared_controllers(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json")

    async def fail_inside_owner():
        with pytest.raises(RuntimeError, match="SIMULATED abort"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("SIMULATED abort")

    asyncio.run(fail_inside_owner())
    assert app.state.family_media.closed and app.state.queries.closed
    assert app.state.nas_connection.store.fd is None


@pytest.mark.parametrize("patch", [
    {"source_ref": None}, {"expected_context": None}, {"rooms": ["living-room"]},
    {"kind": "history"}, {"evidence_refs": [PHOTO_HASH]}, {"start": datetime.now(UTC)},
])
def test_explicit_family_context_and_asset_selection_required(patch):
    data = request_for("a" * 64).model_dump()
    with pytest.raises(ValidationError):
        HomeQuery.model_validate({**data, **patch})


@pytest.mark.parametrize("name", ["HomeQueryFamilyMediaOriginal", "HomeQueryFamilyMediaEvidence"])
def test_family_query_schema_snapshots_match_closed_contracts(name):
    from homeplane import query_contracts

    path = Path(__file__).resolve().parents[1] / "schemas" / (name + ".json")
    assert json.loads(path.read_text()) == getattr(query_contracts, name).model_json_schema()


@pytest.mark.parametrize("change", [
    {"original_count": 99}, {"original_hash": "e" * 64},
    {"physical_copy_count": 2}, {"captured_at": "2026-10-01T00:00:00Z"},
    {"causal_relation": "proven"}, {"confidence": 0.9}, {"raw": "SIMULATED-secret"},
])
def test_family_evidence_rejects_invented_counts_time_cause_and_raw_fields(integrated, change):
    from homeplane.query_contracts import HomeQueryFamilyMediaEvidence

    app, client, _, _, _, _, _ = integrated
    context = ready_catalog(integrated)
    row = peer_read(app, client, request_for(context))["items"][0]
    with pytest.raises(ValidationError):
        HomeQueryFamilyMediaEvidence.model_validate({**row, **change})
