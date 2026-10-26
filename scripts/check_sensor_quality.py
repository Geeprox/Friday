"""Reproducible simulated measurement-to-rule check; no HA or ARL access."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from homeplane.contracts import Event
from homeplane.rule_compiler import compile_rule, evaluate_rule
from homeplane.rule_contracts import ModeLease, RuleIR, RuleTrial
from homeplane.simulator import Simulator

CASES = [
    ("baseline",24,"matched"),("stale-temperature",24,"suppressed"),
    ("calibrated-fallback",24.6,"suppressed"),("calibrated-fallback",24,"matched"),
    ("frozen-temperature",27.9,"suppressed"),("frozen-temperature",27,"matched"),
    ("peer-disagreement",20,"suppressed"),("all-sources-bad",20,"suppressed"),
    ("recovered",20,"suppressed"),("cache-recovery",20,"suppressed"),
    ("watch-window",20,"suppressed"),("wrong-ac-mode",20,"suppressed"),
    ("calibration-expired",20,"suppressed"),("recovery-hold",24.6,"suppressed"),
    ("battery-stale",24,"matched"),("transport-gap",20,"suppressed"),
]


async def check():
    root = Path(__file__).resolve().parents[1]
    report = {"observed_at":datetime.now(UTC).isoformat(),"simulated":True,
        "hardware_verified":False,"household_calibration_verified":False,"real_write_attempted":False,
        "model_used":False,"cases":[]}
    sim = Simulator()
    for scenario, threshold, outcome in CASES:
        sim.set_scenario(scenario)
        snapshot = await sim.snapshot()
        now = datetime.now(UTC)
        body = json.loads((root/"fixtures/homeplane/managed-arrival.json").read_text())
        body["conditions"] = [{"type":"numeric","entity":"room.living","capability":"temperature",
                               "operator":"gt","value":threshold,"unit":"°C"}]
        rule = RuleIR.model_validate(body)
        compilation = compile_rule(rule,snapshot)
        assert compilation.status == "supported" and not compilation.core_independent
        event = Event(source="SIMULATED-check",kind="state-transition",correlation_id="SIMULATED-check",
            wall_time=now,monotonic_time=0,boot_id=sim.boot_id,simulated=True,
            payload={"entity_id":"binary_sensor.door","capability":"contact","from":"closed","to":"open"})
        trial = RuleTrial(event=event,modes={name:ModeLease(value=False,issued_at=now-timedelta(seconds=1),
            valid_until=now+timedelta(seconds=30)) for name in ("home.sleep","home.manual")})
        evaluation = evaluate_rule(rule,snapshot,trial,now=now)
        assert evaluation.outcome == outcome and not evaluation.real_write_attempted
        if outcome == "suppressed":
            assert "unknown_numeric_quality:room.living" in evaluation.reasons and not evaluation.commands
        canonical = next(s for s in snapshot.states if s.id=='room.living:temperature')
        again = await sim.snapshot()
        assert [o.measured_at for o in again.observations] == [o.measured_at for o in snapshot.observations]
        assert [s.distinct_sample_count for s in again.quality_sources] == [s.distinct_sample_count for s in snapshot.quality_sources]
        report["cases"].append({"scenario":scenario,"threshold":threshold,"canonical":canonical.model_dump(mode="json"),
            "sources":[s.model_dump(mode="json") for s in snapshot.quality_sources],
            "evaluation":evaluation.model_dump(mode="json"),"refresh_creates_sample":False})
    report["status"] = "PASS_SIMULATED_MEASUREMENT_RULE_ONLY"
    return report


def main():
    report = asyncio.run(check())
    output = Path("docs/evidence/sensor-quality-roundtrip.json")
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"status":report["status"],"cases":len(report["cases"]),"hardware_verified":False}))


if __name__ == "__main__":
    main()
