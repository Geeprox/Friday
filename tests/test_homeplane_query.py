"""Scoped reads using explicit Simulator fixtures; no hardware/model/media effects."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeplane.api import create_app
from homeplane.contracts import Event
from homeplane.core import HomeCore
from homeplane.query import HomeQueries
from homeplane.query_contracts import HomeQuery, ReadScope, arl_query_descriptor, decode_arl_query
from homeplane.simulator import Simulator
from homeplane.store import TraceStore


class Clock:
    def __init__(self):
        self.wall = datetime.now(UTC)
        self.mono = 1000

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    core = HomeCore(
        Simulator(wall_now=lambda: clock.wall, monotonic=lambda: clock.mono),
        TraceStore(tmp_path / "SIMULATED-query.sqlite"),
    )
    queries = HomeQueries(core, wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    scope = ReadScope(
        subject="SIMULATED-approved-reader",
        revision="fixture-v1",
        expires_at=clock.wall + timedelta(minutes=5),
        boundary="registered_peer",
        rooms=frozenset({"living-room"}),
        permitted=True,
        history=True,
    )
    yield core, queries, scope, clock
    core.store.close()


@pytest.mark.asyncio
async def test_scoped_state_keeps_time_quality_and_relative_evidence_not_confidence(setup):
    core, queries, scope, _ = setup
    report = await queries.read(HomeQuery(kind="state", rooms=["living-room"]), scope)
    assert report.status == "ok" and report.simulated and report.confidence is None
    assert report.items and all(item["room"] == "living-room" for item in report.items)
    assert not report.execution_authority and not core.adapter.managed_actions
    temperature = next(item for item in report.items if item["entity_id"] == "sensor.living.temperature")
    assert temperature["value"] == 24.5 and temperature["quality"] == "healthy"
    assert temperature["source_times"][0]["measured_at"]
    assert temperature["as_of_basis"] == "canonical_resolution_not_hardware_sample"
    assert temperature["confidence"] is None
    assert "external_rules_may_be_invisible_and_are_not_intercepted" in report.limitations


@pytest.mark.asyncio
async def test_stale_and_unknown_never_export_old_temperature_as_current(setup):
    core, queries, scope, _ = setup
    core.adapter.set_scenario("stale-temperature")
    request = HomeQuery(kind="state", entities=["sensor.living.temperature"], capabilities=["temperature"])
    report = await queries.read(request, scope)
    assert len(report.items) == 1
    state = report.items[0]
    assert state["value"] is None and state["quality"] == "stale"
    assert state["source_times"][0]["cached"] is True
    assert state["source_times"][0]["measured_at"] < state["source_times"][0]["received_at"]
    assert not core.adapter.managed_actions


@pytest.mark.asyncio
async def test_room_and_entity_selection_intersect_not_union(setup):
    _, queries, scope, _ = setup
    request = HomeQuery(kind="state", rooms=["living-room"], entities=["light.hall"])
    report = await queries.read(request, scope)
    assert report.status == "empty" and report.items == []
    assert "some_requested_resources_unknown_or_outside_selection" in report.limitations
    broad = await queries.read(HomeQuery(kind="state", rooms=["hall"]), scope)
    assert broad.code == "outside_read_scope" and not broad.items


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unverified", "not_permitted", "expired", "no_history"])
async def test_permission_denial_happens_before_reading_snapshot(setup, monkeypatch, change):
    core, queries, scope, clock = setup
    scope = replace(
        scope,
        **{
            "unverified": {"boundary": "unverified"},
            "not_permitted": {"permitted": False},
            "expired": {"expires_at": clock.wall},
            "no_history": {"history": False},
        }[change],
    )

    async def forbidden():
        pytest.fail("unapproved read attempted")

    monkeypatch.setattr(core, "snapshot", forbidden)
    request = HomeQuery(
        kind="history", rooms=["living-room"], start=clock.wall - timedelta(seconds=10), end=clock.wall
    )
    report = await queries.read(request, scope)
    assert report.status == "refused" and not report.items


@pytest.mark.asyncio
async def test_physical_devices_group_functions_without_exporting_bindings_or_actions(setup):
    _, queries, scope, _ = setup
    report = await queries.read(HomeQuery(kind="devices", rooms=["living-room"]), scope)
    assert len(report.items) == 2
    assert all(item["kind"] == "device" and not item["execution_authority"] for item in report.items)
    assert all("bindings" not in item and "capabilities" not in item for item in report.items)
    assert not any("hall" in key for item in report.items for key in item["function_ids"])


@pytest.mark.asyncio
async def test_history_is_bounded_scoped_sensitive_redacted_and_does_not_invent_causation(setup):
    core, queries, scope, clock = setup
    stamp = clock.wall - timedelta(seconds=10)
    events = [
        Event(
            source="ha",
            kind="state-transition",
            correlation_id="SIMULATED-correlation",
            wall_time=stamp,
            received_at=stamp + timedelta(seconds=1),
            time_basis="upstream_event",
            monotonic_time=999,
            boot_id="SIMULATED-boot",
            simulated=True,
            payload={
                "entity_id": "sensor.living.temperature",
                "from": 24,
                "to": 25,
                "raw": "SIMULATED-secret",
                "api_key": "SIMULATED-secret",
            },
        ),
        Event(
            source="ha",
            kind="source-report",
            correlation_id="SIMULATED-sensitive",
            wall_time=stamp,
            monotonic_time=999,
            boot_id="SIMULATED-boot",
            simulated=True,
            sensitivity="sensitive",
            payload={"entity_id": "sensor.living.temperature", "reason": "SIMULATED-secret"},
        ),
        Event(
            source="ha",
            kind="state-transition",
            correlation_id="SIMULATED-other-room",
            wall_time=stamp,
            monotonic_time=999,
            boot_id="SIMULATED-boot",
            simulated=True,
            payload={"entity_id": "light.hall", "from": "off", "to": "on"},
        ),
    ]
    core.store.append_events(events)
    request = HomeQuery(
        kind="history", rooms=["living-room"], start=stamp - timedelta(seconds=1), end=clock.wall
    )
    report = await queries.read(request, scope)
    assert (
        report.status == "partial"
        and report.coverage == "bounded_local_journal_not_complete_household_history"
    )
    rows = [row for row in report.items if row["id"] == events[0].event_id]
    assert len(rows) == 1
    row = rows[0]
    assert row["source_reported_at"] != row["observed_at"]
    assert row["hardware_sample_time"] is None and row["causation_status"] == "unknown"
    assert row["summary"] == {"from": 24, "to": 25}
    assert "SIMULATED-secret" not in report.model_dump_json()
    assert "SIMULATED-other-room" not in report.model_dump_json()
    assert "temporal_proximity_does_not_establish_causation" in report.limitations


@pytest.mark.asyncio
async def test_history_offset_timezone_and_old_unknown_receipt_stay_honest(setup):
    core, queries, scope, clock = setup
    event = Event(
        source="simulator",
        kind="source-report",
        correlation_id="SIMULATED-old",
        wall_time=(clock.wall - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8))),
        monotonic_time=999,
        boot_id="SIMULATED-old",
        simulated=True,
        payload={"entity_id": "sensor.living.temperature", "reason": "legacy_receipt_unknown"},
    )
    core.store.append_events([event])
    report = await queries.read(
        HomeQuery(
            kind="history", rooms=["living-room"], start=clock.wall - timedelta(seconds=2), end=clock.wall
        ),
        scope,
    )
    row = next(row for row in report.items if row["id"] == event.event_id)
    assert row["observed_at"] is None and row["source_reported_at"] is None
    assert row["event_time_basis"] == "unknown"


@pytest.mark.asyncio
async def test_pagination_is_stable_and_cannot_cross_request_scope_source_or_binding(setup):
    core, queries, scope, clock = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)
    assert first.next_cursor and len(first.items) == 1
    follow = request.model_copy(update={"cursor": first.next_cursor})
    second = await queries.read(follow, scope)
    assert second.items[0]["id"] != first.items[0]["id"]
    assert second.snapshot_ref == first.snapshot_ref
    assert (await queries.read(follow.model_copy(update={"limit": 2}), scope)).code == "query_context_changed"
    assert (
        await queries.read(follow, replace(scope, revision="revoked-and-regranted"))
    ).code == "query_context_changed"
    assert (
        await queries.read(follow.model_copy(update={"cursor": first.next_cursor[:-4] + "AAAA"}), scope)
    ).code == "invalid_query_cursor"
    entity = next(e for e in core.adapter.entities if e.room == "living-room")
    entity.room = "hall"
    assert (await queries.read(follow, scope)).code == "query_context_changed"
    entity.room = "living-room"
    core.adapter = Simulator(wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    assert (await queries.read(follow, scope)).code == "query_context_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_change", ["elapsed", "wall_backwards", "process_restart"])
async def test_expired_or_restart_cursor_never_rehydrates_old_page(setup, clock_change):
    core, queries, scope, clock = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)
    if clock_change == "elapsed":
        clock.advance(31)
    elif clock_change == "wall_backwards":
        clock.wall -= timedelta(seconds=1)
    else:
        queries = HomeQueries(core, wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    report = await queries.read(request.model_copy(update={"cursor": first.next_cursor}), scope)
    assert report.status == "refused" and not report.items


@pytest.mark.asyncio
async def test_snapshot_io_does_not_extend_cursor_or_revoked_grant(setup, monkeypatch):
    core, queries, scope, clock = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)
    snapshot = core.snapshot

    async def elapsed_snapshot():
        clock.advance(2)
        return await snapshot()

    monkeypatch.setattr(core, "snapshot", elapsed_snapshot)
    clock.advance(29)
    report = await queries.read(request.model_copy(update={"cursor": first.next_cursor}), scope)
    assert report.code == "query_cursor_expired" and not report.items and not queries.pages
    scope = replace(scope, expires_at=clock.wall + timedelta(seconds=1))
    report = await queries.read(request, scope)
    assert report.code == "query_context_changed" and not report.items
    assert not core.adapter.managed_actions


@pytest.mark.asyncio
async def test_cached_page_expires_temperature_without_refreshing_its_snapshot_time(setup):
    _, queries, scope, clock = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)
    page = next(iter(queries.pages.values()))
    temperature = next(item for item in page["items"] if item["entity_id"] == "sensor.living.temperature")
    # A qualified reading can expire before the page's shorter-lived cursor.
    temperature["valid_until"] = (clock.wall + timedelta(seconds=1)).isoformat()
    clock.advance(2)
    follow = request.model_copy(update={"cursor": first.next_cursor, "limit": 1})
    rows = []
    while follow.cursor:
        report = await queries.read(follow, scope)
        assert report.snapshot_at == first.snapshot_at and report.snapshot_ref == first.snapshot_ref
        rows += report.items
        follow = follow.model_copy(update={"cursor": report.next_cursor})
    reading = next(item for item in rows if item["id"] == temperature["id"])
    assert reading["quality"] == "stale" and reading["value"] is None
    assert "query_page_value_expired" in reading["reasons"]
    queries.close()
    assert not queries.pages


@pytest.mark.asyncio
async def test_read_fault_does_not_actuate_or_disable_core_and_retries_are_reads(setup, monkeypatch):
    core, queries, scope, _ = setup
    original = core.snapshot

    async def failed():
        raise OSError("SIMULATED-secret-upstream-error")

    monkeypatch.setattr(core, "snapshot", failed)
    request = HomeQuery(kind="state", rooms=["living-room"])
    report = await queries.read(request, scope)
    assert report.code == "query_source_unavailable" and "SIMULATED-secret" not in report.model_dump_json()
    monkeypatch.setattr(core, "snapshot", original)
    assert (await queries.read(request, scope)).status == "ok"
    assert core.adapter.managed_actions == []


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "state"},
        {"kind": "state", "rooms": ["hall", "hall"]},
        {"kind": "history", "rooms": ["hall"]},
        {"kind": "state", "rooms": ["hall"], "limit": True},
        {"kind": "devices", "rooms": ["hall"], "execute": True},
        {"kind": "state", "rooms": ["hall"], "scope": {"all_home": True}},
    ],
)
def test_strict_request_rejects_unbounded_reads_actions_and_self_grants(body):
    with pytest.raises(ValidationError):
        HomeQuery.model_validate(body)


def test_api_has_origin_guard_no_identity_fields_and_draft_arl_not_registered(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED-api.sqlite", settings_path=tmp_path / "settings.json")
    origin = "http://127.0.0.1:8740"
    with TestClient(app, base_url=origin) as client:
        body = {"kind": "state", "rooms": ["living-room"]}
        assert client.post("/api/query", json=body).status_code == 403
        assert (
            client.post(
                "/api/query", json={**body, "actor": "friday"}, headers={"origin": origin}
            ).status_code
            == 422
        )
        report = client.post("/api/query", json=body, headers={"origin": origin}).json()
        assert report["simulated"] and report["status"] == "ok" and not report["execution_authority"]
        assert report["permission_scope"]["boundary"] == "loopback_console"
        descriptor = client.get("/api/query/contracts").json()
        assert not descriptor["registered_in_arl"]
        assert descriptor["arl_proposal"]["availability"] == "unsupported_transport"


def test_proposed_wrapper_passes_actual_arl_subset_without_inventing_array_support():
    contracts = pytest.importorskip("arl.contracts")
    request = HomeQuery(kind="state", rooms=["living-room"])
    descriptor = arl_query_descriptor()
    contracts.validate({"request_json": request.model_dump_json()}, descriptor["inputSchema"])
    with pytest.raises(contracts.Fault):
        contracts.validate(
            {"request_json": request.model_dump_json(), "permissions": "all"}, descriptor["inputSchema"]
        )
    assert descriptor["permissions"] == ["friday_household_read"] and not descriptor["identity_verified"]


@pytest.mark.parametrize(
    "bad",
    [
        {"request_json": '{"kind":"state","rooms":["living-room"],"kind":"history"}'},
        {"request_json": '{"kind":"state","rooms":["living-room"],"actor":"SIMULATED-secret"}'},
        {"request_json": '{"kind":"state","rooms":["living-room"],"execute":true}'},
        {"request_json": "[1,2,3]"},
        {"request_json": "x" * 8193},
        {"request_json": '{"kind":"state","rooms":["living-room"]}', "all_home": True},
    ],
)
def test_arl_wrapper_cannot_smuggle_actions_grants_duplicate_keys_or_oversize(bad):
    with pytest.raises(ValueError) as error:
        decode_arl_query(bad)
    assert str(error.value) in {"invalid_query_json", "invalid_query_wrapper"}
    assert "SIMULATED-secret" not in str(error.value)
    good = HomeQuery(kind="state", rooms=["living-room"])
    assert decode_arl_query({"request_json": good.model_dump_json()}) == good


def query_event(clock, *, entity="sensor.living.temperature", **changes):
    body = {
        "source": "SIMULATED-source", "kind": "state-transition",
        "correlation_id": "SIMULATED-correlation", "wall_time": clock.wall - timedelta(seconds=2),
        "received_at": clock.wall - timedelta(seconds=1), "time_basis": "upstream_event",
        "monotonic_time": clock.mono - 1, "boot_id": "SIMULATED-boot", "simulated": True,
        "payload": {"entity_id": entity, "from": 24, "to": 25},
    }
    return Event(**{**body, **changes})


@pytest.mark.asyncio
async def test_explicit_evidence_reads_only_scoped_retained_event_refs(setup):
    core, queries, scope, clock = setup
    selected = query_event(clock)
    other = query_event(clock, entity="light.hall")
    sensitive = query_event(clock, sensitivity="sensitive")
    core.store.append_events([selected, other, sensitive])
    request = HomeQuery(
        kind="evidence", rooms=["living-room"], start=clock.wall - timedelta(seconds=10), end=clock.wall,
        evidence_refs=[selected.event_id, other.event_id, sensitive.event_id, "SIMULATED-missing"],
    )
    report = await queries.read(request, scope)
    assert report.status == "partial" and [row["id"] for row in report.items] == [selected.event_id]
    assert "some_evidence_refs_missing_expired_or_outside_selection" in report.limitations
    assert report.items[0]["hardware_sample_time"] is None
    assert report.items[0]["causation_status"] == "unknown"
    assert not core.adapter.managed_actions
    denied = await queries.read(request, replace(scope, history=False))
    assert denied.code == "history_permission_required" and not denied.items


@pytest.mark.parametrize("body", [
    {"kind": "evidence", "rooms": ["living-room"]},
    {"kind": "state", "rooms": ["living-room"], "timeout_ms": True},
    {"kind": "state", "rooms": ["living-room"], "timeout_ms": 3001},
    {"kind": "state", "rooms": ["living-room"], "schema_version": True},
    {"kind": "state", "rooms": ["living-room"], "evidence_refs": ["SIMULATED-event"]},
])
def test_extended_contract_cannot_broaden_read_or_self_authorize(body):
    with pytest.raises(ValidationError):
        HomeQuery.model_validate(body)


@pytest.mark.parametrize("ref", ["/tmp/private.mp4", "file:///private", "https://household/asset", "a" * 129])
def test_evidence_refs_are_opaque_not_paths_or_media_urls(ref):
    with pytest.raises(ValidationError):
        HomeQuery(kind="evidence", entities=["sensor.living.temperature"], evidence_refs=[ref],
                  start=datetime.now(UTC) - timedelta(hours=1), end=datetime.now(UTC))


@pytest.mark.asyncio
async def test_revocation_during_snapshot_withholds_content_and_erases_cached_pages(setup, monkeypatch):
    import asyncio

    core, queries, scope, clock = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)
    assert first.next_cursor and queries.pages
    current = [scope]
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = core.snapshot

    async def blocked():
        entered.set()
        await release.wait()
        return await snapshot()

    monkeypatch.setattr(core, "snapshot", blocked)
    task = asyncio.create_task(queries.read(request, scope, current_scope=lambda: current[0]))
    await entered.wait()
    current[0] = replace(scope, permitted=False, revision="SIMULATED-revoked")
    queries.revoke(scope.subject)
    release.set()
    report = await task
    assert report.code == "query_context_changed" and report.items == [] and not queries.pages
    assert core.adapter.managed_actions == []
    assert clock.mono == 1000


@pytest.mark.asyncio
async def test_trusted_scope_callback_failure_is_redacted_before_snapshot(setup, monkeypatch):
    core, queries, scope, _ = setup

    def failed():
        raise OSError("SIMULATED-secret-host-error")

    async def forbidden():
        pytest.fail("scope failure must prevent snapshot")

    monkeypatch.setattr(core, "snapshot", forbidden)
    report = await queries.read(HomeQuery(kind="state", rooms=["living-room"]), scope, current_scope=failed)
    assert report.code == "query_context_changed"
    assert "SIMULATED-secret" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_deadline_includes_projection_not_only_snapshot(setup, monkeypatch):
    _, queries, scope, clock = setup
    states = queries._states

    def slow_projection(*args):
        clock.advance(1)
        return states(*args)

    monkeypatch.setattr(queries, "_states", slow_projection)
    report = await queries.read(HomeQuery(kind="state", rooms=["living-room"], timeout_ms=100), scope)
    assert report.code == "query_deadline_exceeded" and not report.items and not queries.pages


@pytest.mark.asyncio
async def test_wall_and_monotonic_deadlines_cancel_source_io_without_retry(setup, monkeypatch):
    import asyncio

    core, queries, scope, clock = setup
    calls = []

    async def blocked():
        try:
            calls.append("entered")
            await asyncio.Event().wait()
        finally:
            calls.append("cancelled")

    monkeypatch.setattr(core, "snapshot", blocked)
    request = HomeQuery(kind="state", rooms=["living-room"], timeout_ms=10)
    report = await queries.read(request, scope)
    assert report.code == "query_deadline_exceeded" and calls == ["entered", "cancelled"]
    calls.clear()
    assert (await queries.read(request, scope, deadline=clock.mono)).code == "query_deadline_exceeded"
    assert not calls
    assert (await queries.read(request, scope, deadline=True)).code == "invalid_query_deadline"
    assert not core.adapter.managed_actions


@pytest.mark.asyncio
async def test_external_task_cancellation_releases_query_gate(setup, monkeypatch):
    import asyncio

    core, queries, scope, _ = setup
    entered = asyncio.Event()
    original = core.snapshot

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(core, "snapshot", blocked)
    request = HomeQuery(kind="state", rooms=["living-room"])
    task = asyncio.create_task(queries.read(request, scope))
    await entered.wait()
    assert (await queries.read(request, scope)).code == "query_busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not queries.gate.locked()
    monkeypatch.setattr(core, "snapshot", original)
    assert (await queries.read(request, scope)).status == "ok"


@pytest.mark.asyncio
async def test_explicit_cancellation_discards_cursor_and_never_reads_source(setup, monkeypatch):
    core, queries, scope, _ = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)

    async def forbidden():
        pytest.fail("cancelled reads must not reach source")

    monkeypatch.setattr(core, "snapshot", forbidden)
    report = await queries.read(request.model_copy(update={"cursor": first.next_cursor}), scope,
                                cancelled=lambda: True)
    assert report.code == "query_cancelled" and not report.items and not queries.pages


@pytest.mark.asyncio
async def test_same_revision_expiry_change_is_a_different_cursor_grant(setup):
    _, queries, scope, clock = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    first = await queries.read(request, scope)
    renewed = replace(scope, expires_at=clock.wall + timedelta(minutes=10))
    report = await queries.read(request.model_copy(update={"cursor": first.next_cursor}), renewed)
    assert report.code == "query_context_changed" and not report.items


@pytest.mark.asyncio
async def test_future_history_is_refused_before_any_source_read(setup, monkeypatch):
    core, queries, scope, clock = setup

    async def forbidden():
        pytest.fail("future window should fail before source I/O")

    monkeypatch.setattr(core, "snapshot", forbidden)
    report = await queries.read(HomeQuery(kind="history", rooms=["living-room"], start=clock.wall,
                                         end=clock.wall + timedelta(seconds=10)), scope)
    assert report.code == "future_history_window"


@pytest.mark.asyncio
async def test_cache_capacity_and_revocation_are_bounded_and_principal_specific(setup):
    _, queries, scope, _ = setup
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    other = replace(scope, subject="SIMULATED-second-reader")
    assert (await queries.read(request, other)).next_cursor
    for _ in range(15):
        assert (await queries.read(request, scope)).next_cursor
    assert len(queries.pages) == 16
    assert (await queries.read(request, scope)).code == "query_capacity_exceeded"
    queries.revoke(scope.subject)
    assert len(queries.pages) == 1
    assert (await queries.read(request, scope)).next_cursor


@pytest.mark.asyncio
async def test_journal_oversize_corruption_and_free_text_content_never_escape(setup, monkeypatch):
    core, queries, scope, clock = setup
    good = query_event(clock, payload={"entity_id": "sensor.living.temperature", "from": 24, "to": 25,
                                      "reason": "SYNTHETIC-PERSONAL-CONTENT /private/photo.mov", "code": "source_gap"},
                       causation_id="file:///private/context", correlation_id="/private/context")
    huge = query_event(clock, payload={"entity_id": "sensor.living.temperature", "raw": "secret" * 4000})
    core.store.append_events([good])
    snapshot = await core.snapshot()

    async def projected_snapshot():
        return snapshot

    # Corrupt legacy storage below the store's normal admission checks. Core's
    # unrelated recent_events parser is protected; this exercises query projection.
    monkeypatch.setattr(core, "snapshot", projected_snapshot)
    core.store.connection.execute("INSERT INTO events(id,created,body) VALUES (?,?,?)",
                                  (huge.event_id, clock.wall.isoformat(), huge.model_dump_json()))
    core.store.connection.execute("INSERT INTO events(id,created,body) VALUES (?,?,?)",
                                  ("SIMULATED-corrupt", clock.wall.isoformat(), "[broken"))
    core.store.connection.commit()
    report = await queries.read(HomeQuery(kind="history", rooms=["living-room"],
                                         start=clock.wall - timedelta(minutes=1), end=clock.wall), scope)
    row = next(item for item in report.items if item["id"] == good.event_id)
    assert row["summary"] == {"from": 24, "to": 25, "reason": None, "code": "source_gap"}
    assert row["causation_ref"] is None and row["causation_status"] == "unknown"
    assert row["correlation_ref"] is None
    assert huge.event_id not in {item["id"] for item in report.items}
    assert "secret" not in report.model_dump_json() and "/private" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_scope_does_not_export_other_entity_source_times_or_source_metadata(setup, monkeypatch):
    core, queries, scope, _ = setup
    original = core.snapshot

    async def aliased_sources():
        snapshot = await original()
        selected = next(o for o in snapshot.observations if o.entity_id == "sensor.living.temperature")
        hidden = selected.model_copy(update={"entity_id": "light.hall", "ingest_source": "SIMULATED-hidden"})
        return snapshot.model_copy(update={"observations": [*snapshot.observations, hidden]})

    monkeypatch.setattr(core, "snapshot", aliased_sources)
    report = await queries.read(HomeQuery(kind="state", entities=["sensor.living.temperature"]), scope)
    assert report.status == "ok" and "SIMULATED-hidden" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_unknown_sample_time_stays_null_despite_new_receipt_and_canonical_validity(setup, monkeypatch):
    core, queries, scope, clock = setup
    original = core.snapshot

    async def unknown_sample():
        snapshot = await original()
        observations = [o.model_copy(update={"measured_at": None, "source_reported_at": None,
                          "integration_written_at": clock.wall, "received_at": clock.wall,
                          "freshness_basis": "cache_write", "cached": True}) for o in snapshot.observations]
        return snapshot.model_copy(update={"observations": observations})

    monkeypatch.setattr(core, "snapshot", unknown_sample)
    report = await queries.read(HomeQuery(kind="state", entities=["sensor.living.temperature"]), scope)
    assert report.items[0]["freshness"] == "canonical_valid"
    times = report.items[0]["source_times"][0]
    assert times["measured_at"] is None and times["source_reported_at"] is None
    assert times["integration_written_at"] and times["freshness_basis"] == "cache_write"
    assert report.confidence is None


def test_closed_result_schemas_validate_without_allowing_raw_fields(setup):
    from homeplane.query_contracts import HomeQueryResult

    schema = HomeQueryResult.model_json_schema()
    assert len(schema["properties"]["items"]["items"]["anyOf"]) == 4
    assert schema["$defs"]["HomeQuerySourceTime"]["additionalProperties"] is False
    assert schema["$defs"]["HomeQueryEvent"]["additionalProperties"] is False
    base = {"kind": "state", "status": "refused", "code": "read_permission_required",
            "observed_at": datetime.now(UTC), "coverage": "selected_only",
            "permission_scope": {"subject": "SIMULATED-reader", "revision": "SIMULATED-v1",
                                 "boundary": "unverified", "requested_rooms": [], "requested_entities": []}}
    with pytest.raises(ValidationError):
        HomeQueryResult(**base, items=[{"kind": "state", "raw": "SIMULATED-secret"}])
    with pytest.raises(ValidationError):
        HomeQueryResult(**base, next_cursor="SIMULATED-refused-cursor")


@pytest.mark.parametrize("change", [
    {"expires_at": datetime.now()}, {"rooms": {"living-room"}}, {"permitted": 1}, {"subject": "x\nsecret"},
])
def test_trusted_scope_requires_aware_time_immutable_selectors_and_real_booleans(setup, change):
    _, _, scope, _ = setup
    with pytest.raises(ValueError, match="invalid_read_scope"):
        replace(scope, **change)


def test_arl_wrapper_rejects_invalid_unicode_without_exposing_parser_detail():
    with pytest.raises(ValueError, match="invalid_query_wrapper"):
        decode_arl_query({"request_json": "\ud800"})


@pytest.mark.asyncio
async def test_source_only_consumer_uses_actual_arl_sdk_and_returns_closed_fixture_result(setup, monkeypatch):
    contracts = pytest.importorskip("arl.contracts")
    from homeplane.query import HomeQueryConsumer
    from homeplane.query_contracts import HomeQueryResult

    core, queries, scope, clock = setup
    consumer = HomeQueryConsumer(queries, current_scope=lambda: scope)
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    descriptor = arl_query_descriptor()
    proposed = contracts.Tool(name=descriptor["name"], schema=descriptor["inputSchema"], handler=None,
                              permissions=tuple(descriptor["permissions"]), availability="unsupported_transport")
    assert proposed.requires_identity and proposed.descriptor()["inputSchema"] == descriptor["inputSchema"]

    async def forbidden(*args, **kwargs):
        pytest.fail("queries cannot dispatch an action")

    monkeypatch.setattr(core, "dry_run", forbidden)
    result = await consumer.read({"request_json": request.model_dump_json()}, deadline=clock.mono + 1)
    assert HomeQueryResult.model_validate(result).items and result["execution_authority"] is False
    assert len(contracts.encode(result)) <= 65536 and result["household_media_sent"] is False
    assert descriptor["registered"] is False and descriptor["cloud_egress"] == "not_authorized"
    with pytest.raises(contracts.Fault) as fault:
        await consumer.read({"request_json": request.model_dump_json(), "permissions": "all"},
                            deadline=clock.mono + 1)
    assert fault.value.code == "invalid_arguments"
    assert core.adapter.managed_actions == []


@pytest.mark.asyncio
async def test_consumer_host_scope_and_cancellation_fail_closed(setup):
    contracts = pytest.importorskip("arl.contracts")
    from homeplane.query import HomeQueryConsumer

    _, queries, scope, clock = setup
    arguments = {"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()}
    unverified = HomeQueryConsumer(queries, current_scope=lambda: replace(scope, boundary="unverified"))
    with pytest.raises(contracts.Fault) as fault:
        await unverified.read(arguments, deadline=clock.mono + 1)
    assert fault.value.code == "read_permission_required"
    consumer = HomeQueryConsumer(queries, current_scope=lambda: scope)
    result = await consumer.read(arguments, deadline=clock.mono + 1, cancelled=lambda: True)
    assert result["code"] == "query_cancelled" and not result["items"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["state", "devices", "history", "evidence"])
async def test_actual_sdk_consumer_fixture_covers_each_query_kind(setup, kind):
    contracts = pytest.importorskip("arl.contracts")
    from homeplane.query import HomeQueryConsumer
    from homeplane.query_contracts import HomeQueryResult

    core, queries, scope, clock = setup
    event = query_event(clock)
    core.store.append_events([event])
    fields = {"start": clock.wall - timedelta(minutes=1), "end": clock.wall} if kind in {"history", "evidence"} else {}
    if kind == "evidence":
        fields["evidence_refs"] = [event.event_id]
    request = HomeQuery(kind=kind, rooms=["living-room"], **fields)
    result = await HomeQueryConsumer(queries, current_scope=lambda: scope).read(
        {"request_json": request.model_dump_json()}, deadline=clock.mono + 1,
    )
    assert HomeQueryResult.model_validate(result).items
    assert len(contracts.encode(result)) <= 65536 and not core.adapter.managed_actions


@pytest.mark.asyncio
async def test_close_during_read_prevents_result_and_cache_repopulation(setup, monkeypatch):
    core, queries, scope, _ = setup
    snapshot = core.snapshot

    async def stopped():
        value = await snapshot()
        queries.close()
        return value

    monkeypatch.setattr(core, "snapshot", stopped)
    request = HomeQuery(kind="state", rooms=["living-room"], limit=1)
    report = await queries.read(request, scope)
    assert report.code == "query_closed" and not report.items and not queries.pages
    assert (await queries.read(request, scope)).code == "query_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("count,limit,expected", [
    (20, 15, "query_response_budget"), (70, 1, "query_projection_too_broad"),
])
async def test_metadata_and_response_limits_never_admit_a_failed_page(setup, monkeypatch, count, limit, expected):
    core, queries, scope, _ = setup
    snapshot = await core.snapshot()
    state = next(s for s in snapshot.states if s.id.startswith("sensor.living.temperature:"))
    states = [state.model_copy(update={"id": f"sensor.living.temperature:SIMULATED-{index}",
                                      "reasons": ["x" * 256] * 16}) for index in range(count)]

    async def bloated():
        return snapshot.model_copy(update={"states": states})

    monkeypatch.setattr(core, "snapshot", bloated)
    report = await queries.read(HomeQuery(kind="state", rooms=["living-room"], limit=limit), scope)
    assert report.code == expected and not report.items and not queries.pages


@pytest.mark.asyncio
async def test_invalid_later_page_row_is_rejected_before_any_cursor_admission(setup, monkeypatch):
    core, queries, scope, _ = setup
    snapshot = await core.snapshot()
    invalid = snapshot.states[-1].model_copy(update={"resolver_version": "/private/secret\nraw"})

    async def source():
        return snapshot.model_copy(update={"states": [*snapshot.states, invalid.model_copy(
            update={"id": "sensor.living.temperature:z" * 150}
        )]})

    monkeypatch.setattr(core, "snapshot", source)
    report = await queries.read(HomeQuery(kind="state", rooms=["living-room"], limit=1), scope)
    assert report.code == "query_projection_unavailable" and not queries.pages


@pytest.mark.parametrize("value", ["/private/family/photo.mp4", "https://example.com/?token=secret", "C:\\private\\secret"])
def test_content_scalar_projection_never_returns_paths_or_media_urls(value):
    assert HomeQueries._scalar(value) is None


def test_console_pagination_survives_recreated_request_local_scope(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED-pagination.sqlite", settings_path=tmp_path / "settings.json")
    origin = "http://127.0.0.1:8740"
    with TestClient(app, base_url=origin) as client:
        body = {"kind": "state", "rooms": ["living-room"], "limit": 1}
        first = client.post("/api/query", json=body, headers={"origin": origin}).json()
        assert first["next_cursor"]
        second = client.post("/api/query", json={**body, "cursor": first["next_cursor"]},
                             headers={"origin": origin}).json()
        assert second["status"] == "ok" and second["snapshot_ref"] == first["snapshot_ref"]
        assert second["items"][0]["id"] != first["items"][0]["id"]


@pytest.mark.parametrize("name", [
    "HomeQuery", "HomeQueryResult", "HomeQueryState", "HomeQueryDevice", "HomeQueryEvent",
    "HomeQuerySourceTime", "HomeQueryPermissionScope",
])
def test_consumer_schema_snapshots_match_runnable_contract(name):
    import json
    from pathlib import Path

    from homeplane import query_contracts

    artifact = Path(__file__).resolve().parents[1] / "schemas" / (name + ".json")
    assert json.loads(artifact.read_text()) == getattr(query_contracts, name).model_json_schema()


def adoption_fixture(queries, scope, monkeypatch):
    identity = pytest.importorskip("arl.identity")
    from homeplane.query import HomeQueryAdoptionConfig

    verifier = identity.RegisteredPeerVerifier({"mio"})
    current = {"registered": True, "scope": replace(scope, subject="mio"), "calls": []}

    def checked(app, pid):
        current["calls"].append((app, pid))
        return current["registered"] and app == "mio" and pid == 999999

    def forbidden(*args, **kwargs):
        pytest.fail("adoption must never register peers or create grants")

    monkeypatch.setattr(verifier, "is_registered", checked)
    monkeypatch.setattr(verifier, "register", forbidden)
    config = HomeQueryAdoptionConfig("mio", 999999, verifier, lambda: current["scope"])
    return config.bind(queries), config, current


@pytest.mark.asyncio
async def test_adoption_uses_existing_sdk_guard_and_transient_core_source(setup, monkeypatch):
    core, queries, scope, clock = setup
    adoption, _, current = adoption_fixture(queries, scope, monkeypatch)
    adoption.bind_loop()
    arguments = {"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()}
    result = await adoption.read(arguments, deadline=clock.mono + 1)
    assert result["items"] and result["permission_scope"]["subject"] == "mio"
    assert current["calls"] and all(row == ("mio", 999999) for row in current["calls"])
    assert not core.store.connection.execute("SELECT name FROM sqlite_master WHERE name='jobs'").fetchall()
    assert adoption.status()["friday_result_persistence"] == "none"
    assert adoption.status()["cloud_egress"] is False and not core.adapter.managed_actions


@pytest.mark.asyncio
async def test_adoption_unregistered_actual_sdk_peer_fails_before_source(setup, monkeypatch):
    contracts = pytest.importorskip("arl.contracts")
    identity = pytest.importorskip("arl.identity")
    from homeplane.query import HomeQueryAdoptionConfig

    core, queries, scope, clock = setup
    verifier = identity.RegisteredPeerVerifier({"mio"})  # Real empty SDK registry, no enrollment.
    adoption = HomeQueryAdoptionConfig("mio", 999999, verifier, lambda: replace(scope, subject="mio")).bind(queries)
    adoption.bind_loop()

    async def forbidden():
        pytest.fail("unregistered peer must never query a source")

    monkeypatch.setattr(core, "snapshot", forbidden)
    with pytest.raises(contracts.Fault) as error:
        await adoption.read({"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()},
                            deadline=clock.mono + 1)
    assert error.value.code == "read_permission_required" and not verifier.registry


@pytest.mark.asyncio
async def test_adoption_peer_revocation_during_io_discards_result(setup, monkeypatch):
    core, queries, scope, clock = setup
    adoption, _, current = adoption_fixture(queries, scope, monkeypatch)
    adoption.bind_loop()
    snapshot = core.snapshot

    async def revoked():
        result = await snapshot()
        current["registered"] = False
        return result

    monkeypatch.setattr(core, "snapshot", revoked)
    report = await adoption.read({"request_json": HomeQuery(kind="state", rooms=["living-room"], limit=1).model_dump_json()},
                                 deadline=clock.mono + 1)
    assert report["code"] == "query_context_changed" and not report["items"] and not queries.pages


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"subject": "another-reader"}, {"boundary": "loopback_console"}])
async def test_adoption_never_converts_console_scope_or_cross_owner_grant(setup, monkeypatch, change):
    contracts = pytest.importorskip("arl.contracts")
    _, queries, scope, clock = setup
    adoption, _, current = adoption_fixture(queries, scope, monkeypatch)
    adoption.bind_loop()
    current["scope"] = replace(current["scope"], **change)
    with pytest.raises(contracts.Fault) as error:
        await adoption.read({"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()},
                            deadline=clock.mono + 1)
    assert error.value.code == "read_permission_required"


@pytest.mark.asyncio
async def test_adoption_lifecycle_and_metadata_never_probe_source_or_permissions(setup, monkeypatch):
    contracts = pytest.importorskip("arl.contracts")
    _, queries, scope, clock = setup
    adoption, _, current = adoption_fixture(queries, scope, monkeypatch)
    arguments = {"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()}
    assert adoption.status()["peer_verified"] is None and not current["calls"]
    with pytest.raises(contracts.Fault) as error:
        await adoption.read(arguments, deadline=clock.mono + 1)
    assert error.value.code == "query_consumer_not_bound"
    adoption.bind_loop()
    adoption.close()
    with pytest.raises(contracts.Fault) as error:
        await adoption.read(arguments, deadline=clock.mono + 1)
    assert error.value.code == "query_closed" and not current["calls"]


def test_default_api_does_not_activate_consumer_or_publish_peer_fields(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED-adoption-default.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app) as client:
        report = client.get("/api/query/contracts").json()
        adoption = report["consumer_adoption"]
        assert not adoption["configured"] and not report["registered_in_arl"]
        assert adoption["external_transport"] == "not_composed" and adoption["peer_verified"] is None
        assert app.state.query_adoption is None
        paths = client.get("/openapi.json").json()["paths"]
        assert not any("peer" in path or "adoption" in path for path in paths)


def test_owner_api_composition_reuses_existing_core_and_event_loop(tmp_path, monkeypatch):
    import time

    identity = pytest.importorskip("arl.identity")
    from homeplane.query import HomeQueryAdoptionConfig

    verifier = identity.RegisteredPeerVerifier({"mio"})
    monkeypatch.setattr(verifier, "is_registered", lambda app, pid: app == "mio" and pid == 999999)
    scope = ReadScope(subject="mio", revision="SIMULATED-approved", expires_at=datetime.now(UTC)+timedelta(minutes=1),
                      boundary="registered_peer", rooms=frozenset({"living-room"}), permitted=True, history=True)
    config = HomeQueryAdoptionConfig("mio", 999999, verifier, lambda: scope)
    app = create_app(db_path=tmp_path / "SIMULATED-adoption-api.sqlite", settings_path=tmp_path / "settings.json",
                     query_adoption_config=config)
    with TestClient(app) as client:
        status = client.get("/api/query/contracts").json()["consumer_adoption"]
        assert status["configured"] and status["lifecycle"] == "bound" and status["peer_verified"] is None
        assert app.state.query_adoption.queries is app.state.queries
        assert app.state.queries.core is app.state.core
        arguments = {"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()}
        report = client.portal.call(lambda: app.state.query_adoption.read(arguments, deadline=time.monotonic()+1))
        assert report["items"] and report["simulated"]
        assert report["permission_scope"]["subject"] == "mio"
    assert app.state.query_adoption.closed and app.state.queries.closed


@pytest.mark.asyncio
async def test_adoption_rejects_worker_thread_event_loop_before_source(setup, monkeypatch):
    import asyncio

    contracts = pytest.importorskip("arl.contracts")
    _, queries, scope, clock = setup
    adoption, _, current = adoption_fixture(queries, scope, monkeypatch)
    adoption.bind_loop()
    arguments = {"request_json": HomeQuery(kind="state", rooms=["living-room"]).model_dump_json()}

    def worker():
        with pytest.raises(contracts.Fault) as error:
            asyncio.run(adoption.read(arguments, deadline=clock.mono + 1))
        return error.value.code

    assert await asyncio.to_thread(worker) == "query_consumer_wrong_loop"
    assert not current["calls"]
