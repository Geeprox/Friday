"""Simulated HA protocol frames, independent from real household validation."""
import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeplane.ha import HAReadOnlyAdapter


def stamp(seconds=0):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def state(entity="light.simulated", value="off", updated=None, **attrs):
    return {"entity_id": entity, "state": value, "last_updated": updated or stamp(-1),
            "last_reported": stamp(-1), "attributes": attrs}


class Session:
    def __init__(self, server):
        self.server, self.sent, self.closed, self.subscriptions = server, [], False, {}
        self.queue = asyncio.Queue()
        self.queue.put_nowait({"type": "auth_required"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def recv(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        kind = message["type"]
        if kind == "auth":
            self.queue.put_nowait({"type": "auth_invalid"} if self.server.reject_auth else
                                 {"type": "auth_ok", "ha_version": "SIMULATED-ha"})
            return
        assert kind in {"subscribe_events", "get_states", "get_services", "config/entity_registry/list",
                        "config/device_registry/list", "config/area_registry/list"}
        if kind == "subscribe_events":
            self.subscriptions[message["event_type"]] = message["id"]
            result = None
        elif kind == "get_states":
            result = list(copy.deepcopy(self.server.rows).values())
        elif kind == "get_services":
            result = {"light": {"turn_on": {"fields": {"brightness": {}}}}}
        else:
            result = copy.deepcopy(self.server.registry[kind])
        if self.server.inject:
            self.server.inject(self, message)
        self.queue.put_nowait({"type": "result", "id": message["id"], "success": True, "result": result})

    def event(self, old, new, *, fired=None, external=None):
        self.queue.put_nowait({"type": "event", "id": self.subscriptions["state_changed"], "event": {
            "event_type": "state_changed", "time_fired": fired or stamp(),
            "context": {"user_id": "SIMULATED-private-user", "id": "SIMULATED-context"},
            "data": {"entity_id": external or (new or old)["entity_id"], "old_state": old, "new_state": new}}})

    def registry_event(self, kind="entity"):
        event_type = kind + "_registry_updated"
        self.queue.put_nowait({"type": "event", "id": self.subscriptions[event_type], "event": {
            "event_type": event_type, "time_fired": stamp(), "data": {"action": "update"}}})


class Server:
    def __init__(self):
        self.rows = {"light.simulated": state(), "sensor.simulated": state("sensor.simulated", "21.5",
                      device_class="temperature", unit_of_measurement="°C")}
        self.registry = {
            "config/entity_registry/list": [{"entity_id": k, "id": "stable-" + k.split('.')[0],
                "device_id": "SIMULATED-device"} for k in self.rows],
            "config/device_registry/list": [{"id": "SIMULATED-device", "area_id": "SIMULATED-area"}],
            "config/area_registry/list": [{"area_id": "SIMULATED-area", "name": "Simulated Room"}]}
        self.sessions, self.requests, self.inject = [], [], None
        self.reject_auth = False

    def connect(self, *_args, **kwargs):
        assert kwargs.get("proxy") is None and kwargs["max_size"] == 4 * 1024 * 1024
        session = Session(self)
        self.sessions.append(session)
        return session

    def http(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=list(self.rows.values()) if request.url.path.endswith("states") else [])

    def adapter(self, **kwargs):
        return HAReadOnlyAdapter("http://simulated.invalid", "SIMULATED-secret", "simulated-instance",
                                 ws_connect=self.connect, transport=httpx.MockTransport(self.http), **kwargs)

    def change(self, value="on", *, entity="light.simulated", fired=None, **attrs):
        old = copy.deepcopy(self.rows[entity])
        new = state(entity, value, **(old["attributes"] | attrs))
        new["last_updated"] = stamp()
        self.rows[entity] = new
        self.sessions[-1].event(old, copy.deepcopy(new), fired=fired)
        return old, new


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.fixture
async def connected():
    server = Server()
    adapter = server.adapter()
    assert (await adapter.subscribe())["status"] == "connected"
    try:
        yield adapter, server
    finally:
        await adapter.close()
        assert all(s.closed for s in server.sessions)


@pytest.mark.asyncio
async def test_subscribe_before_snapshot_read_only_and_no_hardware_sample(connected):
    ha, server = connected
    snap = await ha.snapshot()
    commands = [m["type"] for m in server.sessions[0].sent]
    assert commands[0] == "auth" and commands[1:5] == ["subscribe_events"] * 4
    assert commands.index("get_states") > 4
    assert not server.requests
    assert ha.health()["continuous_subscription"] and ha.health()["subscription"]["global_order"] == "unavailable"
    sensor = next(o for o in snap.observations if o.capability == "temperature")
    assert sensor.measured_at is None and sensor.cached and sensor.freshness_basis == "cache_write"
    assert next(s for s in snap.states if s.id.endswith(":temperature")).quality == "unknown"
    assert all(e.kind == "state-snapshot" and not e.payload["trigger_eligible"] for e in snap.events)
    assert "SIMULATED-secret" not in snap.model_dump_json()


@pytest.mark.asyncio
async def test_live_change_journaled_without_poll_and_unchanged_poll_is_not_new_sample(connected, tmp_path):
    from homeplane.store import TraceStore
    ha, server = connected
    store = TraceStore(tmp_path / "events.sqlite")
    try:
        ha._event_sink = store.append_events
        initial = await ha.snapshot()
        server.change()
        await until(lambda: any(e.kind == "state-transition" for e in store.recent_events(simulated=False)))
        event = next(e for e in store.recent_events(simulated=False) if e.kind == "state-transition")
        assert event.payload["entity_id"] == "ha:simulated-instance:stable-light"
        assert event.payload["from"] == "off" and event.payload["to"] == "on" and event.payload["trigger_eligible"]
        assert event.source_seq and "SIMULATED-private-user" not in event.model_dump_json()
        assert event.time_basis == "upstream_event" and event.received_at is not None
        assert event.wall_time <= event.received_at
        first, second = await ha.snapshot(), await ha.snapshot()
        assert first.version != initial.version and first.version == second.version
        assert first.observations[0].received_at == second.observations[0].received_at
        assert len([e for e in second.events if e.kind == "state-transition"]) == 1 and not server.requests
    finally:
        ha._event_sink = None
        store.close()


@pytest.mark.asyncio
async def test_initial_buffer_merges_state_without_historical_edge():
    server = Server()
    def inject(session, message):
        if message["type"] == "get_states":
            server.inject = None
            old, new = server.change()
            assert old["state"] == "off" and new["state"] == "on"
    server.inject = inject
    ha = server.adapter()
    try:
        assert (await ha.subscribe())["status"] == "connected"
        snap = await ha.snapshot()
        assert next(s for s in snap.states if s.id.endswith(":light.state")).value == "on"
        assert not any(e.kind == "state-transition" for e in snap.events)
        assert ha.health()["subscription"]["counts"]["reconciled_deltas"] == 1
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_duplicate_attributes_unavailable_and_late_reports_do_not_trigger(connected):
    ha, server = connected
    old, new = server.change("on")
    await until(lambda: ha._stream.counts["events_received"] == 1)
    server.sessions[-1].event(old, new)
    server.change("on", brightness=20)
    server.change("off", fired=stamp(-20))
    server.change("unavailable")
    server.change("on")
    await until(lambda: ha._stream.counts["events_received"] == 6)
    snap = await ha.snapshot()
    assert len([e for e in snap.events if e.kind == "state-transition"]) == 1
    assert ha._stream.counts["duplicates"] == 1
    assert any(e.payload.get("reason") == "late_report" for e in snap.events)


@pytest.mark.asyncio
async def test_reconnect_snapshot_never_invents_arrival_or_replays(connected):
    ha, server = connected
    ha._stream.retry_seconds = .01
    original_boot = ha._boot
    old_version = (await ha.snapshot()).version
    server.sessions[-1].queue.put_nowait(ConnectionError("SIMULATED-private-secret"))
    server.rows["light.simulated"] = state(value="on", updated=stamp())
    await until(lambda: len(server.sessions) == 2 and ha._stream.ready)
    snap = await ha.snapshot()
    assert snap.version != old_version and ha._boot == original_boot
    assert any(e.kind == "observation-gap" for e in snap.events)
    assert snap.events[-1].kind == "state-snapshot" and snap.events[-1].payload["reconciled_after_gap"]
    assert not any(e.kind == "state-transition" for e in snap.events)
    assert "SIMULATED-private-secret" not in snap.model_dump_json()


@pytest.mark.asyncio
async def test_registry_rename_and_area_change_reconcile_stable_identity(connected):
    ha, server = connected
    first = await ha.snapshot()
    row = server.rows.pop("light.simulated")
    row["entity_id"] = "light.simulated_renamed"
    server.rows[row["entity_id"]] = row
    server.registry["config/entity_registry/list"][0]["entity_id"] = row["entity_id"]
    server.registry["config/area_registry/list"][0]["name"] = "Changed Simulated Room"
    server.sessions[-1].registry_event()
    await until(lambda: ha._stream.counts["reconciles"] == 2)
    second = await ha.snapshot()
    lamp = next(e for e in second.entities if e.id == first.entities[0].id)
    assert lamp.room == "Changed Simulated Room" and first.version != second.version
    assert lamp.bindings[0].external_id == "light.simulated_renamed"
    assert not any(e.kind == "state-transition" for e in second.events)


@pytest.mark.asyncio
async def test_discontinuous_delta_gaps_then_reconciles_without_trigger(connected):
    ha, server = connected
    ha._stream.retry_seconds = .01
    wrong_old = state(value="on")
    new = state(value="off", updated=stamp())
    server.rows["light.simulated"] = new
    server.sessions[-1].event(wrong_old, new)
    await until(lambda: len(server.sessions) == 2 and ha._stream.ready)
    snap = await ha.snapshot()
    assert any(e.payload.get("reason") == "state_delta_discontinuity" for e in snap.events)
    assert not any(e.kind == "state-transition" for e in snap.events)


@pytest.mark.asyncio
async def test_buffer_overflow_auth_failure_and_close_are_bounded():
    server = Server()
    def inject(session, message):
        if message["type"] == "get_states":
            row = server.rows["light.simulated"]
            for _ in range(257):
                session.event(row, row)
    server.inject = inject
    ha = server.adapter()
    assert (await ha.subscribe())["status"] == "reconnecting"
    assert ha._events[-1].payload["reason"] == "event_buffer_overflow"
    assert len(ha._stream.buffer) == 256 and not ha._stream.ready
    await ha.close()
    assert all(s.closed for s in server.sessions)
    with pytest.raises(ValueError, match="adapter_closed"):
        await ha.subscribe()
    server = Server()
    server.reject_auth = True
    ha = server.adapter()
    try:
        assert (await ha.subscribe())["status"] == "reconnecting"
        assert not ha.health()["authentication_available"]
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_unregistered_new_entity_never_generates_guessed_trigger(connected):
    ha, server = connected
    row = state("light.simulated_new", "on", stamp())
    server.rows[row["entity_id"]] = row
    server.sessions[-1].event(None, row)
    await until(lambda: ha._stream.counts["events_received"] == 1)
    snap = await ha.snapshot()
    entity = next(e for e in snap.entities if e.bindings[0].external_id == row["entity_id"])
    assert ":provisional:" in entity.id
    assert not any(e.kind == "state-transition" for e in snap.events)


@pytest.mark.asyncio
async def test_worker_only_allows_verified_read_commands(connected):
    ha, server = connected
    with pytest.raises(ValueError, match="read_only_command_required"):
        await ha._stream.command(server.sessions[-1], "call_service", domain="light", service="turn_on")
    assert not any(m["type"] == "call_service" for m in server.sessions[-1].sent)


@pytest.mark.asyncio
async def test_cached_ui_reads_cannot_extend_latch_validity(connected):
    ha, _ = connected
    receipts = dict.fromkeys(ha._stream.published_rows, datetime.now(UTC) - timedelta(seconds=31))
    ha._stream.published_receipts = receipts
    snap = await ha.snapshot()
    latch = next(s for s in snap.states if s.id.endswith(":light.state"))
    assert latch.quality != "healthy" and latch.value is None


@pytest.mark.asyncio
async def test_source_switch_lifespan_stops_worker_and_no_event_dispatch(tmp_path):
    from fastapi.testclient import TestClient

    from homeplane.api import create_app
    server = Server()
    ha = server.adapter()
    # TestClient owns a separate loop; its lifecycle starts/stops the worker itself.
    app = create_app(ha, db_path=tmp_path / "events.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app, base_url="http://127.0.0.1:8740") as client:
        assert client.get("/api/snapshot").json()["mode"] == "ha-read-only"
        assert ha._stream.ready
        result = client.post("/api/settings/source/simulator", headers={"origin": "http://127.0.0.1:8740"})
        assert result.status_code == 200 and ha._closed and ha._stream.task is None
    assert all(s.closed for s in server.sessions)


@pytest.mark.asyncio
async def test_future_state_clock_does_not_become_live_trigger(connected):
    ha, server = connected
    old = copy.deepcopy(server.rows["light.simulated"])
    new = state(value="on", updated=stamp(60))
    server.rows["light.simulated"] = new
    server.sessions[-1].event(old, new)
    await until(lambda: ha._stream.counts["events_received"] == 1)
    assert not any(e.kind == "state-transition" for e in (await ha.snapshot()).events)


@pytest.mark.asyncio
async def test_out_of_order_older_state_is_not_reinstalled(connected):
    ha, server = connected
    old, new = server.change()
    await until(lambda: ha._stream.counts["events_received"] == 1)
    server.sessions[-1].event(new, old)
    await until(lambda: ha._stream.counts["events_received"] == 2)
    snap = await ha.snapshot()
    assert next(s for s in snap.states if s.id.endswith(":light.state")).value == "on"
    assert len([e for e in snap.events if e.kind == "state-transition"]) == 1


@pytest.mark.asyncio
async def test_removed_entity_is_data_change_not_trigger(connected):
    ha, server = connected
    row = server.rows.pop("light.simulated")
    server.sessions[-1].event(row, None)
    await until(lambda: ha._stream.counts["events_received"] == 1)
    assert not any(e.id.endswith("stable-light") for e in (await ha.snapshot()).entities)
    assert ha._events[-1].kind == "source-report" and not ha._events[-1].payload["trigger_eligible"]


@pytest.mark.asyncio
async def test_reconnect_refreshes_credentials_without_exposing_them():
    server = Server()
    calls = []
    async def token():
        calls.append(1)
        return "SIMULATED-token-" + str(len(calls))
    ha = server.adapter(token_provider=token)
    try:
        await ha.subscribe()
        ha._stream.retry_seconds = .01
        server.sessions[-1].queue.put_nowait(ConnectionError())
        await until(lambda: len(server.sessions) == 2 and ha._stream.ready)
        assert len(calls) == 2
        assert server.sessions[-1].sent[0]["access_token"] == "SIMULATED-token-2"
        assert "SIMULATED-token" not in (await ha.snapshot()).model_dump_json()
    finally:
        await ha.close()


@pytest.mark.asyncio
async def test_event_journal_failure_is_reported_without_model_or_action_dependency(connected):
    ha, server = connected
    def failed_journal(_events):
        raise OSError("SIMULATED-private-path")
    ha._event_sink = failed_journal
    server.change()
    await until(lambda: ha._stream.counts["events_received"] == 1)
    assert not ha.health()["diagnostic_journal_available"]
    assert ha.health()["continuous_subscription"]
    assert "SIMULATED-private-path" not in (await ha.snapshot()).model_dump_json()


@pytest.mark.asyncio
async def test_periodic_reconciliation_is_read_not_transition(connected):
    ha, server = connected
    ha._stream.refresh_seconds = .01
    # Existing deadline is 20 s; a registry event moves it to the test interval.
    server.sessions[-1].registry_event("area")
    await until(lambda: ha._stream.counts["reconciles"] >= 3)
    snap = await ha.snapshot()
    assert not any(e.kind == "state-transition" for e in snap.events)
    assert all(m["type"] in {"auth", "subscribe_events", "get_states", "get_services", "config/entity_registry/list",
                             "config/device_registry/list", "config/area_registry/list"} for m in server.sessions[-1].sent)


def test_snapshot_api_timeout_cancels_read_and_can_recover(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from homeplane import api
    from homeplane.simulator import Simulator
    class SlowSource(Simulator):
        slow = True
        cancelled = False
        async def snapshot(self):
            if self.slow:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return await super().snapshot()
    source = SlowSource()
    monkeypatch.setattr(api, "SNAPSHOT_TIMEOUT_SECONDS", .02)
    app = api.create_app(source, db_path=tmp_path / "events.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app) as client:
        failed = client.get("/api/snapshot")
        assert failed.status_code == 503 and failed.json()["code"] == "offline_dependency"
        assert source.cancelled
        source.slow = False
        assert client.get("/api/snapshot").status_code == 200
        assert not app.state.core.store.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0]
