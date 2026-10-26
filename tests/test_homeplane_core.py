import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeplane.api import create_app
from homeplane.contracts import ActionRequest, Capability, Observation, Snapshot
from homeplane.quality import choose_primary, greater_than, resolve
from homeplane.rules import minimum_light_action, preserve_external, remove_action
from homeplane.simulator import FIXTURE, Simulator
from homeplane.store import TraceStore


def observation(**changes):
    now = datetime.now(UTC)
    fields = dict(entity_id="temp", capability="temperature", value=25, physical_source_id="physical:1",
                  ingest_source="ha", measured_at=now, received_at=now, freshness_basis="device_sample",
                  clock_uncertainty_seconds=1, transport_health="online", simulated=True)
    fields.update(changes)
    return Observation(**fields)


def test_last_reported_never_becomes_new_measurement():
    now = datetime.now(UTC)
    o = observation(measured_at=None, integration_written_at=now, freshness_basis="cache_write")
    state = resolve(o, now)
    assert state.quality == "unknown" and state.value is None
    assert o.measured_at is None


def test_cache_cannot_restore_stale_temperature():
    now = datetime.now(UTC)
    o = observation(measured_at=now-timedelta(hours=8), cached=True, integration_written_at=now)
    state = resolve(o, now)
    assert state.quality == "stale" and state.value is None
    assert greater_than(state, 24, now) == "unknown"


def test_unchanged_latched_state_is_not_periodic_sensor():
    now = datetime.now(UTC)
    assert resolve(observation(measured_at=now-timedelta(days=2)), now).quality == "stale"
    assert resolve(observation(measured_at=None, received_at=now, semantics="latched_state", value="closed"), now).quality == "healthy"


def test_healthy_equal_values_not_frozen_by_last_changed():
    o = observation()
    assert resolve(o, o.received_at).quality == "healthy"


def test_same_physical_source_cannot_act_as_fallback():
    now = datetime.now(UTC)
    old = observation(measured_at=now-timedelta(hours=3))
    forwarded = observation(ingest_source="geek")
    assert choose_primary([old, forwarded], now, canonical_id="room.temp").quality == "unknown"
    alternative = observation(physical_source_id="physical:2")
    assert choose_primary([old, alternative], now, canonical_id="room.temp").selected_source == "physical:2"


def test_unknown_clock_and_future_sample_rejected():
    now = datetime.now(UTC)
    assert resolve(observation(clock_uncertainty_seconds=None), now).quality == "unknown"
    assert resolve(observation(measured_at=now+timedelta(hours=1)), now).quality == "unknown"


def test_preserve_unknown_graph_bytes():
    raw = ' { "unknown": [1, {"port":"opaque"}] }\n'
    record = preserve_external("g", "geek", raw, simulated=True)
    assert record.raw == raw and record.normalization == "opaque"
    assert record.fingerprint == hashlib.sha256(raw.encode()).hexdigest()


def test_remove_arrival_ac_preserves_hall_light():
    rule = json.loads(FIXTURE.read_text())["arrival_rule"]
    fingerprint = hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()
    result = remove_action(rule, target="climate.living", capability="climate.set", expected_fingerprint=fingerprint)
    assert result["actions"] == [rule["actions"][0]]
    assert len(rule["actions"]) == 2
    with pytest.raises(ValueError, match="source_conflict"):
        remove_action(rule, target="climate.living", capability="climate.set", expected_fingerprint="old")


def test_minimum_never_assumed_five_percent_or_separate_power_then_dim():
    cap = Capability(name="light.set", access="write", minimum=2, maximum=254,
                     minimum_verified=True, atomic_power_brightness=True,
                     no_bright_flash_verified=True, verification="simulated")
    assert minimum_light_action(cap, simulated=True) == {"power":"on", "brightness":2}
    for field in ("minimum_verified", "atomic_power_brightness", "no_bright_flash_verified"):
        with pytest.raises(ValueError):
            minimum_light_action(cap.model_copy(update={field:False}), simulated=True)
    with pytest.raises(ValueError, match="hardware_verification_required"):
        minimum_light_action(cap, simulated=False)


def test_local_db_network_path_rejected():
    with pytest.raises(ValueError):
        TraceStore(Path('/Volumes/NAS/home.sqlite'))


def request(version, **changes):
    fields = dict(target="light.hall", desired={"power":"on", "brightness":2}, actor="test",
                  expected_version=version, expires_at=(datetime.now(UTC)+timedelta(seconds=30)).isoformat())
    fields.update(changes)
    return fields


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "traces.sqlite")) as client:
        yield client


def test_e2e_fixture_quality_action_trace(client):
    snapshot = Snapshot.model_validate(client.get('/api/snapshot').json())
    assert snapshot.mode == "simulator" and all(e.simulated for e in snapshot.entities)
    stale = client.post('/api/simulator/scenario/stale-temperature').json()
    assert next(s for s in stale['states'] if s['id'].startswith('sensor.living'))['quality'] == 'stale'
    assert stale['rules'][1]['normalization'] == 'opaque'
    trace = client.post('/api/actions/dry-run', json=request(stale['version'])).json()
    assert trace['status'] == 'planned' and not trace['real_write_attempted']
    assert client.get('/api/traces/'+trace['action_id']).json() == trace
    assert any(e['kind']=='action-result' for e in client.get('/api/snapshot').json()['events'])
    assert client.get('/static/app.js').status_code == 200
    assert client.post('/api/actions/execute', json={}).status_code == 403
    # A dry-run ID cannot become an executable plan, even with a valid origin.
    response = client.post('/api/actions/execute', json={"plan_id":trace['action_id']}, headers={"origin":"http://testserver"})
    assert response.status_code == 404


def test_duplicate_is_idempotent_and_conflicting_reuse_rejected(client):
    version = client.get('/api/snapshot').json()['version']
    body = request(version, idempotency_key='same')
    first = client.post('/api/actions/dry-run', json=body).json()
    assert client.post('/api/actions/dry-run', json=body).json() == first
    body['desired'] = {'power':'off'}
    assert client.post('/api/actions/dry-run', json=body).status_code == 409


@pytest.mark.parametrize('changes,code', [
    ({'expires_at':(datetime.now(UTC)-timedelta(seconds=5)).isoformat()},'expired_request'),
    ({'expected_version':'old'},'source_conflict'),
    ({'target':'lock.door'},'ambiguous_target'),
    ({'desired':{'brightness':1}},'unsupported_parameters'),
])
def test_action_policy(client, changes, code):
    version = client.get('/api/snapshot').json()['version']
    trace = client.post('/api/actions/dry-run', json=request(version, **changes)).json()
    assert trace['code'] == code and not trace['real_write_attempted']


def test_gap_rejects_actions_and_recovery_is_snapshot(client):
    snapshot = client.post('/api/simulator/scenario/transport-gap').json()
    trace = client.post('/api/actions/dry-run', json=request(snapshot['version'])).json()
    assert trace['code'] == 'stale_input'
    recovered = client.post('/api/simulator/scenario/recovered').json()
    assert recovered['events'][-1]['kind'] != 'state-transition'
    assert next(s for s in recovered['states'] if s['id'].startswith('sensor.living'))['quality'] == 'suspect'


def test_cross_origin_and_developer_do_not_authorize(client):
    assert client.post('/api/simulator/scenario/baseline', headers={'Origin':'https://evil.test'}).status_code == 403
    assert client.get('/api/doctor').json()['real_writes'] == 'disabled'


def test_schema_and_timezone_contract(client):
    assert client.get('/api/contracts').json()['schema_version'] == 1
    with pytest.raises(ValidationError):
        ActionRequest(**request('x', expires_at='2026-09-29T01:00:00'))


def test_bounded_trace_retention(tmp_path):
    store = TraceStore(tmp_path / 'bound.sqlite', max_rows=2)
    import asyncio

    from homeplane.core import HomeCore
    core = HomeCore(Simulator(),store)
    async def run():
        version = (await core.snapshot()).version
        for _ in range(4):
            await core.dry_run(ActionRequest(**request(version)))
    asyncio.run(run())
    assert len(store.recent()) == 2
    store.close()


def test_invalid_payload_never_enters_journal(client):
    version = client.get('/api/snapshot').json()['version']
    for desired in ({'brightness': True}, {'token': 'test-secret'}, {'power': {'nested': 'bad'}}):
        response = client.post('/api/actions/dry-run', json=request(version, desired=desired))
        assert response.status_code == 422
    assert client.app.state.core.store.recent() == []


def test_events_persist_across_core_restart(tmp_path):
    import asyncio

    from homeplane.core import HomeCore
    path = tmp_path / 'restart.sqlite'
    store = TraceStore(path)
    core = HomeCore(Simulator(), store)
    snapshot = asyncio.run(core.snapshot())
    first_event = snapshot.events[0].event_id
    store.close()
    store = TraceStore(path)
    core = HomeCore(Simulator(), store)
    assert first_event in {e.event_id for e in asyncio.run(core.snapshot()).events}
    store.close()
