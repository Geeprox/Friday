"""API contracts and Rule IR consuming explicitly simulated canonical measurements."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.contracts import Event, Snapshot
from homeplane.rule_compiler import evaluate_rule
from homeplane.rule_contracts import ModeLease, RuleIR, RuleTrial
from homeplane.simulator import Simulator

RULE = Path(__file__).resolve().parents[1]/"fixtures/homeplane/managed-arrival.json"


def numeric_rule(threshold):
    body = json.loads(RULE.read_text())
    body["conditions"] = [{"type":"numeric","entity":"room.living","capability":"temperature",
                           "operator":"gt","value":threshold,"unit":"°C"}]
    return RuleIR.model_validate(body)


def request(snapshot, now):
    event = Event(source="SIMULATED-test",kind="state-transition",correlation_id="SIMULATED-test",
        wall_time=now,monotonic_time=0,boot_id="SIMULATED-test-boot",simulated=True,
        payload={"entity_id":"binary_sensor.door","capability":"contact","from":"closed","to":"open"})
    # A poll alone creates no edge. Supply an explicit fresh simulated source event
    # for these numeric-condition trials; runtime admission has separate tests.
    snapshot.events.append(event)
    return RuleTrial(event=event,modes={m:ModeLease(value=False,issued_at=now-timedelta(seconds=1),
        valid_until=now+timedelta(seconds=30)) for m in ("home.manual","home.sleep")})


@pytest.mark.parametrize("scenario,threshold,outcome", [
    ("baseline",24,"matched"),("stale-temperature",24,"suppressed"),
    ("calibrated-fallback",24.6,"suppressed"),("calibrated-fallback",24,"matched"),
    ("frozen-temperature",27.9,"suppressed"),("frozen-temperature",27,"matched"),
    ("peer-disagreement",20,"suppressed"),("all-sources-bad",20,"suppressed"),
    ("recovered",20,"suppressed"),("cache-recovery",20,"suppressed"),
    ("watch-window",20,"suppressed"),("wrong-ac-mode",20,"suppressed"),
    ("calibration-expired",20,"suppressed"),("recovery-hold",24.6,"suppressed"),
])
def test_api_snapshot_compile_and_interval_rule_suppression(tmp_path, scenario, threshold, outcome):
    app = create_app(db_path=tmp_path/"SIMULATED.sqlite",settings_path=tmp_path/"settings.json")
    with TestClient(app,base_url="http://127.0.0.1:8740") as client:
        data = client.post('/api/simulator/scenario/'+scenario).json()
        snapshot = Snapshot.model_validate(data)
        assert snapshot.simulator_scenario == scenario
        rule = numeric_rule(threshold)
        response = client.post('/api/rules/compile',json=rule.model_dump(mode="json"),
                               headers={"origin":"http://127.0.0.1:8740"})
        assert response.status_code == 200, response.json()
        compiled = response.json()
        assert compiled['status'] == 'supported' and compiled['simulated']
        assert 'core_canonical_quality:room.living' in compiled['reasons']
        assert not compiled['core_independent'] and not compiled['wan_independent']
        now = datetime.now(UTC)
        result = evaluate_rule(rule,snapshot,request(snapshot,now),now=now)
        assert result.outcome == outcome and not result.real_write_attempted and not result.physically_confirmed
        if outcome == "suppressed":
            assert 'unknown_numeric_quality:room.living' in result.reasons and not result.commands
        else:
            assert {c['target'] for c in result.commands} == {'light.hall','climate.living'}
        assert len(snapshot.devices) == 4 and len(snapshot.derived_entities) == 1
        assert all(o.simulated for o in snapshot.observations) and all(p.simulated for p in snapshot.quality_sources)
        assert any(e.kind=='quality-change' for e in snapshot.events)


@pytest.mark.asyncio
async def test_snapshot_refresh_never_adds_sample_or_repeats_quality_event():
    sim = Simulator()
    sim.set_scenario('calibrated-fallback')
    first = await sim.snapshot()
    second = await sim.snapshot()
    assert [p.distinct_sample_count for p in first.quality_sources] == [p.distinct_sample_count for p in second.quality_sources]
    assert [o.measured_at for o in first.observations] == [o.measured_at for o in second.observations]
    assert len(first.events) == len(second.events)
    assert next(s for s in second.states if s.id=='room.living:temperature').calibration_version == 'SIMULATED-relative-v1'


@pytest.mark.asyncio
async def test_expiration_never_replays_a_previous_matched_numeric_action():
    snapshot = await Simulator().snapshot()
    state = next(s for s in snapshot.states if s.id=='room.living:temperature')
    # Each event is fresh at evaluation time; only the retained canonical sample expires.
    for seconds in (-.5,0,.5,5,60,300):
        now = state.valid_until+timedelta(seconds=seconds)
        result = evaluate_rule(numeric_rule(20),snapshot,request(snapshot,now),now=now)
        assert result.outcome == ('matched' if seconds < 0 else 'suppressed')
        if seconds >= 0:
            assert 'unknown_numeric_quality:room.living' in result.reasons and not result.commands
