"""Synthetic fixtures only; no household or real protocol/hardware access."""
import json

import httpx
import pytest

from homeplane.ha import HAReadOnlyAdapter


class Socket:
    def __init__(self, entity_id="sensor.simulated_temperature", *, deny=False):
        self.sent = []
        self.entity_id = entity_id
        self.deny = deny
        self.messages = [{"type": "auth_required"}, {"type": "auth_ok", "ha_version": "simulated"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def send(self, message):
        data = json.loads(message)
        self.sent.append(data)
        inventories = {
            "config/entity_registry/list": [{"entity_id": self.entity_id, "id": "stable-registry-id", "device_id": "physical-1"}],
            "config/device_registry/list": [{"id": "physical-1", "area_id": "room-1"}],
            "config/area_registry/list": [{"area_id": "room-1", "name": "Simulated Room"}],
        }
        if "id" in data:
            self.messages.append({"id": data["id"], "success": not self.deny, "result": inventories[data["type"]]})

    async def recv(self):
        return json.dumps(self.messages.pop(0))


def adapter(*, unavailable=False, deny=False):
    fixture = {"entity_id": "sensor.simulated_temperature", "state": "unavailable" if unavailable else "21.5",
               "attributes": {"device_class": "temperature", "unit_of_measurement": "°C", "friendly_name": "Simulated Temperature"},
               "last_reported": "2026-09-29T08:00:00Z"}
    requests, sockets = [], []
    fail = {"now": False}

    def handler(request):
        requests.append(request)
        if fail["now"]:
            return httpx.Response(503)
        return httpx.Response(200, json=[fixture] if request.url.path.endswith("states") else [])

    def ws(*_args, **_kwargs):
        item = Socket(fixture["entity_id"], deny=deny)
        sockets.append(item)
        return item

    return HAReadOnlyAdapter("http://simulated.invalid", "SYNTHETIC-SECRET", "test-instance",
                             transport=httpx.MockTransport(handler), ws_connect=ws), fixture, requests, sockets, fail


@pytest.mark.asyncio
async def test_snapshot_reads_only_and_does_not_claim_new_sample(tmp_path):
    ha, _, requests, sockets, _ = adapter()
    snap = await ha.snapshot()
    assert snap.mode == "ha-read-only"
    assert snap.entities[0].id == "ha:test-instance:stable-registry-id"
    assert snap.entities[0].room == "Simulated Room"
    obs = snap.observations[0]
    assert obs.measured_at is None and obs.source_reported_at is None
    assert obs.integration_written_at is not None and obs.freshness_basis == "cache_write"
    assert obs.cached and snap.states[0].quality == "unknown"
    assert all(r.method == "GET" for r in requests)
    assert [r.url.path for r in requests] == ["/api/states", "/api/services"]
    assert all(m["type"] in {"auth", "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list"}
               for m in sockets[0].sent)
    assert "SYNTHETIC-SECRET" not in snap.model_dump_json()
    assert snap.rules[0].normalization == "opaque" and snap.rules[0].raw is None
    from datetime import UTC, datetime, timedelta

    from homeplane.core import HomeCore
    from homeplane.query import HomeQueries
    from homeplane.query_contracts import HomeQuery, ReadScope
    from homeplane.store import TraceStore

    store = TraceStore(tmp_path / "SIMULATED-ha-query.sqlite")
    try:
        query = HomeQueries(HomeCore(ha, store))
        report = await query.read(HomeQuery(kind="state", rooms=["Simulated Room"]),
            ReadScope("SIMULATED-reader", "fixture-v1", datetime.now(UTC) + timedelta(seconds=5),
                      boundary="registered_peer", rooms=frozenset({"Simulated Room"}), permitted=True))
        row = report.items[0]
        assert row["quality"] == "unknown" and row["value"] is None
        source = row["source_times"][0]
        assert source["measured_at"] is None and source["source_reported_at"] is None
        assert source["integration_written_at"] and source["cached"]
        assert source["freshness_basis"] == "cache_write" and report.confidence is None
        assert all(r.method == "GET" for r in requests) and not report.execution_authority
    finally:
        store.close()
    await ha.close()


@pytest.mark.asyncio
async def test_identity_survives_entity_rename():
    ha, fixture, *_ = adapter()
    first = await ha.snapshot()
    fixture["entity_id"] = "sensor.simulated_renamed"
    fixture["attributes"]["friendly_name"] = "New name"
    second = await ha.snapshot()
    assert first.entities[0].id == second.entities[0].id
    assert second.entities[0].bindings[0].external_id == "sensor.simulated_renamed"
    await ha.close()


@pytest.mark.asyncio
async def test_outage_marks_retained_data_stale_and_recovery_reconciles():
    ha, _, _, _, fail = adapter()
    first = await ha.snapshot()
    fail["now"] = True
    failed = await ha.snapshot()
    assert failed.states[0].quality == "stale"
    assert failed.observations[0].received_at == first.observations[0].received_at
    assert failed.observations[0].transport_health == "offline"
    assert failed.events[-1].kind == "observation-gap"
    fail["now"] = False
    recovered = await ha.snapshot()
    assert recovered.events[-1].payload["reconciled_after_gap"]
    assert recovered.states[0].quality == "unknown"
    assert len({first.version, failed.version, recovered.version}) == 3
    await ha.close()


@pytest.mark.asyncio
async def test_execute_and_refresh_do_not_write():
    ha, _, requests, _, _ = adapter()
    assert (await ha.execute({"target": "light.any"}))["status"] == "unauthorized"
    assert not requests
    assert (await ha.refresh("sensor.simulated_temperature"))["status"] == "cached"
    assert (await ha.refresh("unknown"))["status"] == "unknown"
    assert all(r.method == "GET" for r in requests)
    await ha.close()


@pytest.mark.asyncio
async def test_unavailable_never_becomes_zero_or_healthy():
    ha, *_ = adapter(unavailable=True)
    snap = await ha.snapshot()
    assert snap.states[0].quality == "faulty" and snap.states[0].value is None
    await ha.close()


@pytest.mark.asyncio
async def test_missing_registry_reports_provisional_identity():
    ha, *_ = adapter(deny=True)
    snap = await ha.snapshot()
    assert ":provisional:" in snap.entities[0].id
    assert not ha.health()["registry_available"]
    assert "provisional_identity_registry_unavailable" in snap.states[0].reasons
    await ha.close()


@pytest.mark.asyncio
async def test_diagnostics_are_bounded():
    ha, _, _, _, fail = adapter()
    fail["now"] = True
    for _ in range(135):
        snap = await ha.snapshot()
    assert len(snap.events) == 128
    assert not snap.entities
    await ha.close()


@pytest.mark.parametrize("url", ["ftp://home", "http://user:secret@home", "http://home?token=x", "http://home#secret"])
def test_reject_credential_urls(url):
    with pytest.raises(ValueError):
        HAReadOnlyAdapter(url, "synthetic", "test")


@pytest.mark.asyncio
async def test_version_tracks_evidence_not_poll_count():
    ha, fixture, _, _, fail = adapter()
    first = await ha.snapshot()
    assert (await ha.snapshot()).version == first.version
    fixture["state"] = "22.5"
    changed = await ha.snapshot()
    assert changed.version != first.version
    fail["now"] = True
    outage = await ha.snapshot()
    assert outage.states[0].value is None and outage.states[0].selected_source is None
    assert outage.observations[0].value == 22.5
    assert (await ha.snapshot()).version == outage.version
    fail["now"] = False
    recovered = await ha.snapshot()
    assert recovered.version != changed.version
    assert (await ha.snapshot()).version == recovered.version
    await ha.close()


@pytest.mark.asyncio
async def test_unavailable_has_no_selected_source():
    ha, *_ = adapter(unavailable=True)
    snap = await ha.snapshot()
    assert snap.states[0].selected_source is None
    assert snap.observations[0].value == "unavailable"
    await ha.close()


@pytest.mark.asyncio
async def test_explicit_contract_and_no_hub_source_merging():
    ha, *_ = adapter()
    assert ha.capabilities()["state_changed_subscription"] == "supported"
    assert ha.health()["subscription"]["status"] == "not_started"
    assert (await ha.discover())["status"] == "unsupported"
    assert (await ha.connect())["transport"] == "online"
    entities = await ha.inventory()
    assert entities[0].physical_source_id == entities[0].id
    assert any(b.source == "ha-device-registry" and b.external_id == "physical-1" for b in entities[0].bindings)
    await ha.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b'[{"entity_id":3}]', b'[{"entity_id":"sensor.a","state":12}]',
                                     b'[{"entity_id":"sensor.a","state":"1","attributes":[]}]',
                                     b'{"value":NaN}', b' ' * (4 * 1024 * 1024 + 1)])
async def test_invalid_or_oversized_protocol_fails_closed(content):
    ha = HAReadOnlyAdapter("http://simulated.invalid", "synthetic", "test",
                          transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content)))
    snap = await ha.snapshot()
    assert not snap.entities and snap.events[-1].kind == "observation-gap"
    await ha.close()


@pytest.mark.asyncio
async def test_environment_proxy_is_disabled(monkeypatch):
    from homeplane import ha as module
    original = module.httpx.AsyncClient
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    ha, *_ = adapter()
    assert captured["trust_env"] is False
    await ha.close()


@pytest.mark.asyncio
async def test_core_dry_run_does_not_conflict_just_because_it_polls_again(tmp_path):
    from datetime import UTC, datetime, timedelta

    from homeplane.contracts import ActionRequest
    from homeplane.core import HomeCore
    from homeplane.store import TraceStore

    ha, *_ = adapter()
    snap = await ha.snapshot()
    store = TraceStore(tmp_path / "simulated-traces.sqlite3")
    trace = await HomeCore(ha, store).dry_run(ActionRequest(
        target=snap.entities[0].id, desired={"power": "on"}, expected_version=snap.version,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    ))
    assert trace.code == "unsupported"  # Correct rejection: read-only sensor, not stale version.
    assert not trace.real_write_attempted
    store.connection.close()
    await ha.close()
