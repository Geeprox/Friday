"""Real journal/query contracts with synthetic records only; no media/device IO."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from homeplane.contracts import Event
from homeplane.core import HomeCore
from homeplane.event_evidence import EventEvidence, EvidenceDetail, EvidenceQuery, create_router
from homeplane.query import HomeQueries
from homeplane.simulator import Simulator
from homeplane.store import TraceStore


class Clock:
    def __init__(self):
        self.wall = datetime.now(UTC)
        self.mono = 1000

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class Archive:
    def __init__(self):
        self.fingerprint = "b" * 64
        self.cameras = [{"id": "SIMULATED-camera", "name": "SIMULATED camera", "room": "living-room",
                         "simulated": True, "source_binding_current": True, "room_binding_current": True}]

    def context(self):
        return {"fingerprint": self.fingerprint, "configured": True, "closed": False, "cameras": self.cameras}

    async def resolve(self, *_args):
        pytest.fail("Opening evidence must not enumerate NAS or read recordings")


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    adapter = Simulator(wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    store = TraceStore(tmp_path / "SIMULATED-evidence.sqlite", wall_now=lambda: clock.wall)
    core = HomeCore(adapter, store)
    queries = HomeQueries(core, wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    archive = Archive()
    revision = {"value": 0}
    service = EventEvidence(core, queries, archive, context_revision=lambda: revision["value"],
                            wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    yield SimpleNamespace(core=core, queries=queries, owner=service, clock=clock, archive=archive, revision=revision)
    queries.close()
    store.close()


def event(rig, identifier="SIMULATED-event", *, entity="sensor.living.temperature", seconds=10, **updates):
    from homeplane.event_provenance import entity_source_context

    selected = next((item for item in rig.core.adapter.entities if item.id == entity), None)
    body = dict(event_id=identifier, source="simulator", kind="state-transition",
                correlation_id="SIMULATED-group", wall_time=rig.clock.wall - timedelta(seconds=seconds),
                received_at=rig.clock.wall - timedelta(seconds=seconds-1), time_basis="upstream_event",
                monotonic_time=rig.clock.mono-seconds, boot_id=rig.core.adapter.boot_id, simulated=True,
                source_context=entity_source_context(rig.core.adapter, selected) if selected else None,
                payload={"entity_id": entity, "from": "off", "to": "on", "token": "SIMULATED-private"})
    body.update(updates)
    return Event(**body)


async def query(rig, **updates):
    context = await rig.owner.context()
    params = dict(expected_context=context["fingerprint"], room="living-room", limit=30)
    params.update(updates)
    return await rig.owner.query(EvidenceQuery(**params))


def detail_request(projection, identifier="SIMULATED-event"):
    return EvidenceDetail(expected_context=projection["context_fingerprint"],
                          projection_id=projection["projection_id"], event_id=identifier)


@pytest.mark.asyncio
async def test_real_query_projection_preserves_evidence_and_redacts_sensitive_records(rig):
    rig.core.store.append_events([
        event(rig),
        event(rig, "SIMULATED-other-room", entity="light.hall"),
        event(rig, "SIMULATED-sensitive", sensitivity="sensitive"),
        event(rig, "SIMULATED-wrong-mode", simulated=False),
    ])
    result = await query(rig)
    assert [row["id"] for row in result["items"]] == ["SIMULATED-event"]
    encoded = json.dumps(jsonable_encoder(result))
    assert "SIMULATED-private" not in encoded and "SIMULATED-other-room" not in encoded
    assert result["simulated"] and not result["complete_household_history"]
    report = await rig.owner.detail(detail_request(result))
    assert report["explanation"]["level"] == "unknown"
    assert not report["physically_confirmed"] and not report["device_write_attempted"]
    assert not rig.core.adapter.managed_actions


@pytest.mark.asyncio
async def test_latest_events_are_selected_across_real_query_pages(rig):
    rig.core.store.append_events([event(rig, f"SIMULATED-{i:03}", seconds=200-i) for i in range(111)])
    result = await query(rig, limit=3)
    assert [row["id"] for row in result["items"]] == ["SIMULATED-110", "SIMULATED-109", "SIMULATED-108"]
    assert result["older_items_omitted"]
    assert len(rig.owner.projections[result["projection_id"]]["rows"]) == 111


@pytest.mark.asyncio
async def test_relative_window_uses_current_server_time_after_ui_context_has_aged(rig):
    context = await rig.owner.context()
    rig.clock.advance(120)
    rig.core.store.append_events([event(rig, seconds=1)])
    result = await rig.owner.query(EvidenceQuery(expected_context=context["fingerprint"], room="living-room",
                                                window_seconds=60))
    assert result["end"] == rig.clock.wall
    assert result["start"] == rig.clock.wall-timedelta(seconds=60)
    assert result["items"][0]["id"] == "SIMULATED-event"


@pytest.mark.asyncio
async def test_large_household_does_not_rehash_the_registry_per_journal_row(rig, monkeypatch):
    entity = rig.core.adapter.entities[0]
    rig.core.adapter.entities += [entity.model_copy(update={"id": f"SIMULATED-stress-{i}",
        "physical_source_id": f"SIMULATED-source-{i}", "room": "SIMULATED-other-room"}) for i in range(1400)]
    rig.core.store.append_events([event(rig, f"SIMULATED-stress-event-{i}", seconds=1000-i) for i in range(400)])
    calls = []
    original = rig.owner._binding

    def counted(snapshot):
        calls.append(len(snapshot.entities))
        return original(snapshot)

    monkeypatch.setattr(rig.owner, "_binding", counted)
    result = await query(rig, limit=5)
    assert len(result["items"]) == 5
    assert calls and max(calls) >= 1400
    assert len(calls) <= 4, "full household binding work must remain constant, not per event/check"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["legacy", "mismatched_digest", "foreign_source"])
async def test_unverified_history_has_no_current_action_rule_or_recording_attribution(rig, change):
    updates = {"boot_id": "SIMULATED-old-boot"}
    updates.update({"legacy": {"source_context": None}, "mismatched_digest": {"source_context": "d"*64},
                    "foreign_source": {"source": "SIMULATED-foreign"}}[change])
    rig.core.store.append_events([event(rig, **updates)])
    result = await query(rig)
    assert result["items"][0]["source_binding"] == "unverified"
    assert result["items"][0]["entity_names"] == []
    report = await rig.owner.detail(detail_request(result))
    assert report["explanation"]["level"] == "source_unverified"
    assert report["action"] is None and report["rules"] == [] and report["recordings"] == []
    assert report["recording_code"] == "event_source_unverified"


@pytest.mark.asyncio
async def test_explicit_reference_and_same_group_cannot_link_unverified_provenance(rig):
    foreign = event(rig, "SIMULATED-foreign-cause", seconds=20, source="SIMULATED-other-source")
    legacy = event(rig, "SIMULATED-old-group", seconds=15, boot_id="SIMULATED-old-boot", source_context=None)
    rig.core.store.append_events([foreign, legacy, event(rig, causation_id=foreign.event_id)])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert report["source_binding"] == "persistent_source_context"
    assert report["recorded_cause"] is None and report["correlated_events"] == []
    assert report["explanation"]["level"] == "unknown"


@pytest.mark.asyncio
async def test_new_persistent_source_digest_can_link_same_entity_across_boots(rig):
    cause = event(rig, "SIMULATED-cause", seconds=20, boot_id="SIMULATED-prior-boot")
    rig.core.store.append_events([cause, event(rig, causation_id=cause.event_id)])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert report["recorded_cause"]["id"] == cause.event_id
    assert report["recorded_cause"]["source_binding"] == "persistent_source_context"
    assert report["explanation"]["level"] == "source_recorded"


@pytest.mark.asyncio
async def test_real_simulator_contact_producer_is_admitted_with_its_source_context(rig):
    incoming = rig.core.adapter.contact_event("open")
    assert incoming.source == "simulator:explicit-contact" and incoming.source_context
    rig.clock.advance(1)
    selected = next(item for item in rig.core.adapter.entities if item.id == incoming.payload["entity_id"])
    result = await query(rig, room=selected.room)
    report = await rig.owner.detail(detail_request(result, incoming.event_id))
    assert report["event"]["source_binding"] == "persistent_source_context"
    assert report["event"]["source"] == "simulator:explicit-contact"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["restart", "instance", "registry", "room", "mapping_legacy", "mapping_changed"])
async def test_real_ha_producer_persistent_source_survives_only_unchanged_instance_binding_and_room(tmp_path, change):
    from homeplane.ha import HAReadOnlyAdapter
    from tests.test_homeplane_ha_stream import Server

    server = Server()
    def adapter(url="http://simulated.invalid"):
        return HAReadOnlyAdapter(url, "SIMULATED-test-secret", "SIMULATED-evidence-instance",
                                 ws_connect=server.connect, transport=httpx.MockTransport(server.http))

    first, replacement = adapter(), None
    original = HomeCore(first, TraceStore(tmp_path / "SIMULATED-journal.sqlite"))
    try:
        old_snapshot = await original.snapshot()
        old_entity = next(item for item in old_snapshot.entities if item.kind == "light")
        first._event("state-transition", {"entity_id": old_entity.id, "from": "off", "to": "on"},
                     datetime.now(UTC)-timedelta(seconds=2))
        incoming = first._events[-1]
        assert incoming.source == "ha" and incoming.source_context
        await original.snapshot()  # Persist the exact production event.
        if change == "registry":
            server.registry["config/entity_registry/list"][0]["unique_id"] = "SIMULATED-replacement-device"
        elif change == "room":
            server.registry["config/device_registry/list"][0]["area_id"] = "SIMULATED-moved-area"
            server.registry["config/area_registry/list"].append({"area_id": "SIMULATED-moved-area", "name": "SIMULATED moved room"})
        replacement = adapter("http://other-simulated.invalid" if change == "instance" else "http://simulated.invalid")
        original.adapter = replacement
        snapshot = await original.snapshot()
        current_entity = next(item for item in snapshot.entities if item.kind == "light")
        assert current_entity.id == old_entity.id, "Exercise identical normalized entity IDs across source changes"
        assert incoming.boot_id != original.adapter._boot
        archive = Archive()
        archive.cameras = [{"id": "SIMULATED-qualified-camera", "name": "SIMULATED camera", "room": current_entity.room,
                            "simulated": False, "source_binding_current": True, "timing_reviewed": True,
                            "room_binding_current": True,
                            "completion_convention": "closed_files_operator_reviewed", "clock_uncertainty_seconds": 0}]
        if change == "mapping_legacy":
            archive.cameras[0].pop("room_binding_current")
        elif change == "mapping_changed":
            archive.cameras[0]["room_binding_current"] = False
        owner = EventEvidence(original, HomeQueries(original), archive)
        context = await owner.context()
        projection = await owner.query(EvidenceQuery(expected_context=context["fingerprint"], room=current_entity.room))
        report = await owner.detail(detail_request(projection, incoming.event_id))
        if change in {"restart", "mapping_legacy", "mapping_changed"}:
            assert report["source_binding"] == "persistent_source_context"
            assert report["event"]["entity_names"]
            if change == "restart":
                assert len(report["recordings"]) == 1
            else:
                assert report["recordings"] == []
                assert report["recording_code"] == "camera_room_binding_unreviewed"
        else:
            assert report["source_binding"] == "unverified"
            assert report["event"]["entity_names"] == []
            assert report["recordings"] == [] and report["action"] is None and report["rules"] == []
    finally:
        await first.close()
        if replacement is not None:
            await replacement.close()
        original.store.close()


@pytest.mark.asyncio
async def test_real_ha_source_wide_gap_is_coverage_notice_without_room_or_recording_attribution(tmp_path):
    from tests.test_homeplane_ha_stream import Server

    server = Server()
    core = HomeCore(server.adapter(), TraceStore(tmp_path / "SIMULATED-coverage.sqlite"))
    try:
        snapshot = await core.snapshot()
        target = next(item for item in snapshot.entities if item.kind == "light")
        core.adapter._event("observation-gap", {"reason": "transport_lost"}, datetime.now(UTC)-timedelta(seconds=3))
        gap = core.adapter._events[-1]
        assert "entity_id" not in gap.payload and gap.source_context is None
        core.store.append_events([gap.model_copy(update={"event_id": "SIMULATED-old-gap", "boot_id": "SIMULATED-old-boot"}),
                                  gap.model_copy(update={"event_id": "SIMULATED-sensitive-gap", "sensitivity": "sensitive"})])
        owner = EventEvidence(core, HomeQueries(core), Archive())
        context = await owner.context()
        result = await owner.query(EvidenceQuery(expected_context=context["fingerprint"], room=target.room))
        assert len(result["coverage_gaps"]) == 1
        assert result["coverage_gaps"][0]["code"] == "transport_lost"
        assert result["coverage_gaps"][0]["scope"] == "current_connection_not_room_event"
        assert gap.event_id not in [item["id"] for item in result["items"]]
        assert "camera_id" not in result["coverage_gaps"][0] and "entity_refs" not in result["coverage_gaps"][0]
        assert all(request.method == "GET" for request in server.requests)
    finally:
        await core.adapter.close()
        core.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["endpoint", "registry"])
async def test_same_adapter_source_metadata_changes_invalidate_old_projection(tmp_path, change):
    from tests.test_homeplane_ha_stream import Server

    server = Server()
    core = HomeCore(server.adapter(), TraceStore(tmp_path / "SIMULATED-live-context.sqlite"))
    try:
        snapshot = await core.snapshot()
        target = next(item for item in snapshot.entities if item.kind == "light")
        core.adapter._event("state-transition", {"entity_id": target.id, "from": "off", "to": "on"},
                            datetime.now(UTC)-timedelta(seconds=2))
        incoming = core.adapter._events[-1]
        owner = EventEvidence(core, HomeQueries(core), None)
        context = await owner.context()
        projection = await owner.query(EvidenceQuery(expected_context=context["fingerprint"], room=target.room))
        if change == "endpoint":
            core.adapter.url = "http://another-simulated.invalid"
        else:
            server.registry["config/entity_registry/list"][0]["unique_id"] = "SIMULATED-replaced"
        with pytest.raises(ValueError, match="context_changed"):
            await owner.detail(detail_request(projection, incoming.event_id))
    finally:
        await core.adapter.close()
        core.store.close()


def test_default_app_simulator_uses_explicit_fixture_camera_room_binding_for_event_lookup(tmp_path):
    from fastapi.testclient import TestClient

    from homeplane.api import create_app
    from homeplane.archive_contracts import ArchiveResolveRequest
    from homeplane.event_provenance import entity_source_context

    app = create_app(db_path=tmp_path / "SIMULATED-composed.sqlite", settings_path=tmp_path / "settings.json")
    core = app.state.core
    target = next(item for item in core.adapter.entities if item.room == "living-room")
    incoming = Event(source="simulator", event_id="SIMULATED-living-event", kind="state-transition",
                     source_context=entity_source_context(core.adapter, target),
                     correlation_id="SIMULATED-living-group", wall_time=datetime.now(UTC)-timedelta(seconds=45),
                     monotonic_time=100, boot_id=core.adapter.boot_id, time_basis="local_event", simulated=True,
                     payload={"entity_id": target.id, "from": "off", "to": "on"})
    core.store.append_events([incoming])
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        context = client.get("/api/home/evidence/context").json()
        result = client.post("/api/home/evidence/query", json={
            "expected_context": context["fingerprint"], "room": "living-room", "window_seconds": 86400,
        })
        assert result.status_code == 200, result.text
        result = result.json()
        report = client.post("/api/home/evidence/detail", json={
            "expected_context": result["context_fingerprint"], "projection_id": result["projection_id"],
            "event_id": incoming.event_id,
        })
        assert report.status_code == 200, report.text
        report = report.json()
        assert report["event"]["simulated"]
        assert report["recordings"], report["recording_code"]
        lookup = ArchiveResolveRequest.model_validate(report["recordings"][0]["request"])
        assert lookup.camera_id == "simulated:archive:living"
        archive = app.state.archive.context()
        camera = next(item for item in archive["cameras"] if item["id"] == lookup.camera_id)
        assert camera["source_binding_current"] is True and camera["room_binding_current"] is True
        assert camera["room"] == target.room


@pytest.mark.asyncio
async def test_only_explicit_cause_and_group_links_are_presented_as_source_records(rig):
    cause = event(rig, "SIMULATED-cause", seconds=15)
    selected = event(rig, causation_id=cause.event_id)
    temporal = event(rig, "SIMULATED-nearby", seconds=9, correlation_id="SIMULATED-unrelated")
    rig.core.store.append_events([cause, selected, temporal])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert report["explanation"]["level"] == "source_recorded"
    assert report["recorded_cause"]["id"] == cause.event_id
    assert "SIMULATED-nearby" not in [row["id"] for row in report["correlated_events"]]
    assert "不证明因果" in report["correlation_caption"]
    assert report["action"] is None and report["rules"] == []


@pytest.mark.asyncio
async def test_removed_cause_is_not_replayed_from_cached_projection(rig):
    rig.core.store.append_events([event(rig, "SIMULATED-cause", seconds=15), event(rig, causation_id="SIMULATED-cause")])
    result = await query(rig)
    with rig.core.store.connection:
        rig.core.store.connection.execute("DELETE FROM events WHERE id=?", ("SIMULATED-cause",))
    report = await rig.owner.detail(detail_request(result))
    assert report["recorded_cause"] is None and not report["cause_reference_available"]
    assert not report["correlated_events"]


@pytest.mark.asyncio
async def test_removed_original_record_is_not_delivered_from_cache(rig):
    rig.core.store.append_events([event(rig)])
    result = await query(rig)
    with rig.core.store.connection:
        rig.core.store.connection.execute("DELETE FROM events WHERE id=?", ("SIMULATED-event",))
    with pytest.raises(ValueError, match="reference_unavailable"):
        await rig.owner.detail(detail_request(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "registry", "permission", "archive"])
async def test_source_registry_permission_and_recording_changes_expire_old_references(rig, change):
    rig.core.store.append_events([event(rig)])
    result = await query(rig)
    if change == "source":
        rig.core.adapter = Simulator(wall_now=lambda: rig.clock.wall, monotonic=lambda: rig.clock.mono)
    elif change == "registry":
        rig.core.adapter.entities[0] = rig.core.adapter.entities[0].model_copy(update={"name": "SIMULATED-renamed"})
    elif change == "permission":
        rig.revision["value"] += 1
    else:
        rig.archive.fingerprint = "c" * 64
    with pytest.raises(ValueError, match="context_changed"):
        await rig.owner.detail(detail_request(result))
    assert not rig.owner.projections


@pytest.mark.asyncio
async def test_expired_and_unselected_event_references_fail_closed(rig):
    rig.core.store.append_events([event(rig), event(rig, "SIMULATED-old", seconds=20)])
    result = await query(rig, limit=1)
    with pytest.raises(ValueError, match="reference_unavailable"):
        await rig.owner.detail(detail_request(result, "SIMULATED-old"))
    rig.clock.advance(121)
    with pytest.raises(ValueError, match="projection_expired"):
        await rig.owner.detail(detail_request(result))


@pytest.mark.asyncio
async def test_observation_gap_is_not_a_causal_explanation(rig):
    rig.core.store.append_events([event(rig, kind="observation-gap")])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert report["explanation"]["level"] == "history_gap"
    assert "不能完整还原" in report["explanation"]["caption"]


@pytest.mark.asyncio
async def test_recording_lookup_is_only_typed_metadata_and_never_scans_media(rig):
    from homeplane.archive_contracts import ArchiveResolveRequest

    rig.core.store.append_events([event(rig)])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert len(report["recordings"]) == 1
    lookup = report["recordings"][0]
    validated = ArchiveResolveRequest.model_validate(lookup["request"])
    assert validated.camera_id == "SIMULATED-camera"
    assert (validated.time_range.end - validated.time_range.start).total_seconds() == 30
    assert lookup["causal_relation"] == "unknown" and not lookup["playback_verified"]
    assert "url" not in lookup and "path" not in lookup


@pytest.mark.asyncio
async def test_unreviewed_camera_mapping_unknown_clock_and_other_rooms_offer_no_playback(rig):
    rig.archive.cameras = [{"id": "SIMULATED-real-camera", "name": "SIMULATED unreviewed",
                           "room": "living-room", "simulated": False, "timing_reviewed": False,
                           "source_binding_current": True}]
    rig.core.store.append_events([event(rig)])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert report["recordings"] == []
    rig.archive.cameras = [{"id": "SIMULATED-other", "room": "hall", "simulated": True}]
    result = await query(rig)
    assert (await rig.owner.detail(detail_request(result)))["recordings"] == []
    rig.core.store.append_events([event(rig, "SIMULATED-unknown-clock", time_basis="unknown", seconds=1)])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result, "SIMULATED-unknown-clock"))
    assert report["recordings"] == [] and report["recording_code"] == "event_clock_unknown"


@pytest.mark.asyncio
async def test_duplicate_registry_room_names_do_not_guess_a_recording_target(rig):
    rig.core.adapter._areas = {"SIMULATED-area-a": {"name": "living-room"},
                               "SIMULATED-area-b": {"name": "living-room"}}
    rig.core.store.append_events([event(rig)])
    result = await query(rig)
    report = await rig.owner.detail(detail_request(result))
    assert report["recordings"] == [] and report["recording_code"] == "recording_room_ambiguous"


@pytest.mark.asyncio
async def test_router_returns_real_projection_and_strictly_rejects_authority_fields(rig):
    rig.core.store.append_events([event(rig)])
    app = FastAPI()
    router = create_router(rig.core, asyncio.Lock(), rig.queries, rig.archive)
    router.evidence_owner.now = lambda: rig.clock.wall
    router.evidence_owner.monotonic = lambda: rig.clock.mono
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        context = await client.get("/api/home/evidence/context")
        assert context.status_code == 200 and context.headers["cache-control"] == "no-store"
        request = {"expected_context": context.json()["fingerprint"], "room": "living-room"}
        result = await client.post("/api/home/evidence/query", json=request)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        body = result.json()
        detail = await client.post("/api/home/evidence/detail", json={
            "expected_context": body["context_fingerprint"], "projection_id": body["projection_id"],
            "event_id": "SIMULATED-event",
        })
        assert detail.status_code == 200 and detail.json()["event"]["id"] == "SIMULATED-event"
        forged = await client.post("/api/home/evidence/query", json={**request, "permitted": True})
        assert forged.status_code == 422
        missing = await client.post("/api/home/evidence/query", json={**request, "room": "unregistered"})
        assert missing.status_code == 409
        targets = await client.get("/api/home/evidence/targets", params={**request, "search": "temperature"})
        assert targets.status_code == 200
        assert all("temperature" in row["name"].lower() for row in targets.json()["entities"])


@pytest.mark.parametrize("values", [
    {"start": datetime.now(UTC)},
    {"start": datetime.now(UTC)-timedelta(days=2), "end": datetime.now(UTC)},
    {"limit": True}, {"room": "bad\nroom"}, {"entity_id": "../../secret"},
    {"window_seconds": True}, {"window_seconds": 86401},
])
def test_request_bounds(values):
    with pytest.raises(ValidationError):
        EvidenceQuery(**{"expected_context": "a"*64, "room": "living-room", **values})


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "target_binding", "event_boot", "nonmember", "source_context"])
async def test_action_result_preserves_readback_only_with_verified_persistent_lineage(tmp_path, mutation):
    from homeplane.action_contracts import DevicePlanRequest, LightExecutionTrace
    from tests.test_homeplane_actions import close
    from tests.test_homeplane_device_controls import rig as ha_rig

    core, *_ = ha_rig(tmp_path)
    try:
        snapshot = await core.snapshot()
        target = next(e for e in snapshot.entities if e.kind == "light")
        now = datetime.now(UTC)
        event = Event(event_id="SIMULATED-action-event", source="action-gateway", kind="action-result",
                      correlation_id="SIMULATED-action-group", wall_time=now-timedelta(seconds=1),
                      monotonic_time=10, boot_id=core.actions.boot_id, payload={"action_id": "SIMULATED-action",
                      "status": "reported_match", "code": "readback_matches"})
        request = DevicePlanRequest(target=target.id, operation="light.set", desired={"power": "on"},
                                    expected_state_version=target.state_version, expires_at=now+timedelta(seconds=60),
                                    idempotency_key="SIMULATED-evidence-action")
        trace = LightExecutionTrace(action_id="SIMULATED-action", correlation_id=event.correlation_id,
                                    request=request, created_at=event.wall_time, boot_id=core.actions.boot_id,
                                    target_binding_fingerprint=core.actions.binding_fingerprint(target),
                                    status="reported_match", code="readback_matches", reasons=[],
                                    events=[event], real_write_attempted=True, reported={"power": "on"})
        if mutation == "target_binding":
            trace = trace.model_copy(update={"target_binding_fingerprint": "d"*64})
        elif mutation == "event_boot":
            event = event.model_copy(update={"boot_id": "SIMULATED-other-owner"})
            trace = trace.model_copy(update={"events": [event]})
        elif mutation == "nonmember":
            trace = trace.model_copy(update={"events": []})
        elif mutation == "source_context":
            event = event.model_copy(update={"source_context": "d"*64})
            trace = trace.model_copy(update={"events": [event]})
        core.store.save_light_action(trace)
        core.store.append_events([event])
        owner = EventEvidence(core, HomeQueries(core), None)
        context = await owner.context()
        projection = await owner.query(EvidenceQuery(expected_context=context["fingerprint"], room=target.room))
        report = await owner.detail(detail_request(projection, event.event_id))
        if mutation == "none":
            assert report["action"]["reported_match"]
            assert report["action"]["real_write_attempted"]
            assert not report["action"]["physically_confirmed"]
            assert "尚未独立确认" in report["action"]["readback_caption"]
            assert report["source_binding"] == "persistent_action_binding"
            assert report["recordings"] == []
        else:
            assert report["source_binding"] == "unverified"
            assert report["action"] is None and report["recordings"] == [] and report["rules"] == []
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_console_consumes_actual_backend_contracts_and_clears_stale_playback(rig, tmp_path):
    rig.core.store.append_events([event(rig)])
    context = await rig.owner.context()
    projection = await query(rig)
    detail = await rig.owner.detail(detail_request(projection))
    projections = {room["id"]: await query(rig, room=room["id"]) for room in context["rooms"]}
    projections[projection["room"]] = projection
    fixture = tmp_path / "SIMULATED-evidence-console.json"
    fixture.write_text(json.dumps(jsonable_encoder({"context": context, "projection": projection,
                                                  "projections": projections, "detail": detail})))
    result = subprocess.run(["node", "tests/event_evidence_console_fixture.cjs", str(fixture)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real API projections" in result.stdout


@pytest.mark.asyncio
async def test_owned_rule_and_action_lineage_uses_actual_runtime_records_without_new_effects(tmp_path):
    from tests.test_homeplane_core_rules import cleanup, enable, fixture, proposal
    from tests.test_homeplane_core_rules import event as incoming_event

    core, _rows, calls, _fault, _path, _grant, rule_owner, manager, runtime = await fixture(tmp_path)
    try:
        plan = await enable(manager, await proposal(core, manager))
        await runtime.set_mode("awake")
        incoming = incoming_event(core)
        await runtime.ingest(core.adapter, incoming)
        assert len(calls) == 1  # MockTransport only, no household network/device.
        owner = EventEvidence(core, HomeQueries(core), None, core_rules=manager)
        context = await owner.context()
        target = next(e for e in core.adapter._last.entities if e.kind == "binary_sensor")
        result = await owner.query(EvidenceQuery(expected_context=context["fingerprint"], room=target.room))
        report = await owner.detail(detail_request(result, incoming.event_id))
        assert report["explanation"]["level"] == "recorded_rule_run"
        assert len(report["rules"]) == 1
        assert report["rules"][0]["record_id"] == plan.new.id
        assert report["rules"][0]["relation"] == "recorded_trigger_or_action_lineage"
        assert not report["rules"][0]["direct_write_available"]
        action_id = next(row["id"] for row in result["items"] if row["source"] == "action-gateway"
                         and row["summary"].get("status") == "reported_match")
        action_report = await owner.detail(detail_request(result, action_id))
        assert action_report["action"]["actor"] == "core_rule"
        assert action_report["action"]["reported_match"] and not action_report["physically_confirmed"]
        assert action_report["rules"][0]["record_id"] == plan.new.id
        assert action_report["recorded_trigger"]["id"] == incoming.event_id
        assert len(calls) == 1, "Review must never resend or reconcile an actuator request"
    finally:
        await cleanup(core, rule_owner, runtime)
