"""All rule inputs and events are explicitly simulated; no device API is used."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homeplane.contracts import Event
from homeplane.rule_compiler import compile_rule, digest, evaluate_rule, remove_instruction
from homeplane.rule_contracts import ModeLease, RuleIR, RuleTrial
from homeplane.simulator import Simulator

RULE = Path(__file__).resolve().parents[1] / "fixtures/homeplane/managed-arrival.json"


def rule(**changes):
    body = json.loads(RULE.read_text())
    body.update(changes)
    return RuleIR.model_validate(body)


def trial(snapshot, now, **changes):
    event = Event(
        source="SIMULATED-door",
        kind="state-transition",
        correlation_id="SIMULATED-correlation",
        wall_time=now,
        monotonic_time=0,
        boot_id=snapshot.events[-1].boot_id,
        payload={"entity_id": "binary_sensor.door", "capability": "contact", "from": "closed", "to": "open"},
        simulated=True,
    )
    modes = {
        name: ModeLease(
            value=False, issued_at=now - timedelta(seconds=1), valid_until=now + timedelta(seconds=30)
        )
        for name in ("home.sleep", "home.manual")
    }
    return RuleTrial(event=event, modes=modes, **changes)


@pytest.mark.asyncio
async def test_typed_ac_only_change_preserves_unrequested_rule_parts():
    old = rule()
    new = remove_instruction(old, "air-conditioner-on", digest(old))
    assert new.actions == [old.actions[0]] and len(old.actions) == 2
    assert new.trigger == old.trigger and new.conditions == old.conditions and new.safety == old.safety
    assert new.revision == 2
    compilation = compile_rule(new, await Simulator().snapshot())
    assert compilation.status == "supported" and compilation.simulated
    assert (
        not compilation.core_independent
        and not compilation.wan_independent
        and compilation.native_artifact is None
    )
    with pytest.raises(ValueError, match="source_conflict"):
        remove_instruction(old, "air-conditioner-on", "SIMULATED-stale-fingerprint")
    with pytest.raises(ValueError):
        remove_instruction(new, "hall-light-on", digest(new))


@pytest.mark.asyncio
async def test_data_and_event_ports_never_interchange_or_infer_sleep_from_off():
    snapshot = await Simulator().snapshot()
    now = datetime.now(UTC)
    request = trial(snapshot, now)
    assert evaluate_rule(rule(), snapshot, request, now=now).outcome == "matched"
    request.event.kind = "state-snapshot"
    assert evaluate_rule(rule(), snapshot, request, now=now).outcome == "not_triggered"
    request.event.kind = "state-transition"
    request.modes = {}
    unknown = evaluate_rule(rule(), snapshot, request, now=now)
    assert unknown.outcome == "suppressed" and "guard_unknown:home.sleep" in unknown.reasons
    assert not unknown.commands and not unknown.real_write_attempted
    snapshot.entities[-1].capabilities = [
        c for c in snapshot.entities[-1].capabilities if c.access != "event"
    ]
    assert compile_rule(rule(), snapshot).status == "unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["replay", "expired", "future", "foreign_boot", "manual", "sleep", "stale_guard"]
)
async def test_replay_expiry_boot_and_explicit_protection_suppress(kind):
    snapshot = await Simulator().snapshot()
    now = datetime.now(UTC)
    request = trial(snapshot, now)
    if kind == "replay":
        request.replay = True
    elif kind in {"expired", "future"}:
        request.event.wall_time += timedelta(seconds=10 if kind == "future" else -10)
    elif kind == "foreign_boot":
        request.event.boot_id = "SIMULATED-previous-boot"
    elif kind == "stale_guard":
        request.modes["home.manual"].valid_until = now
    else:
        request.modes["home." + kind].value = True
    result = evaluate_rule(rule(), snapshot, request, now=now)
    assert result.outcome == "suppressed" and not result.commands


@pytest.mark.asyncio
async def test_numeric_quality_three_values_and_units_are_not_false_or_averaged():
    sim = Simulator()
    r = rule(
        conditions=[
            {
                "type": "numeric",
                "entity": "sensor.living.temperature",
                "capability": "temperature",
                "operator": "gt",
                "value": 20,
                "unit": "°C",
            }
        ]
    )
    snapshot = await sim.snapshot()
    now = datetime.now(UTC)
    assert evaluate_rule(r, snapshot, trial(snapshot, now), now=now).outcome == "matched"
    sim.set_scenario("stale-temperature")
    snapshot = await sim.snapshot()
    now = datetime.now(UTC)
    unknown = evaluate_rule(r, snapshot, trial(snapshot, now), now=now)
    assert unknown.outcome == "suppressed" and unknown.reasons == [
        "unknown_numeric_quality:sensor.living.temperature"
    ]
    body = r.model_dump(mode="json")
    body["conditions"][0]["unit"] = "°F"
    assert (
        "unit_mismatch:sensor.living.temperature"
        in compile_rule(RuleIR.model_validate(body), snapshot).reasons
    )


@pytest.mark.asyncio
async def test_night_minimum_uses_verified_level_and_real_scope_cannot_use_fixture_proof():
    snapshot = await Simulator().snapshot()
    night = rule(
        conditions=[{"type": "mode", "mode": "home.sleep", "equals": True}],
        actions=[
            {
                "type": "light.set",
                "id": "night",
                "target": "light.hall",
                "power": "on",
                "brightness": "minimum_verified",
                "require_no_bright_flash": True,
            }
        ],
        safety={"allow_during_sleep": True},
    )
    now = datetime.now(UTC)
    request = trial(snapshot, now)
    request.modes["home.sleep"].value = True
    result = evaluate_rule(night, snapshot, request, now=now)
    assert result.outcome == "matched" and result.commands[0]["desired"] == {"power": "on", "brightness": 2}
    request.modes["home.manual"].value = True
    assert evaluate_rule(night, snapshot, request, now=now).outcome == "suppressed"
    snapshot.entities[0].capabilities[0].atomic_power_brightness = False
    assert compile_rule(night, snapshot).status == "unsupported"
    snapshot.mode = "ha-read-only"
    assert compile_rule(night.model_copy(update={"simulated": False}), snapshot).status == "unsupported"
    unsafe = rule(
        conditions=[{"type": "mode", "mode": "home.sleep", "equals": True}],
        safety={"allow_during_sleep": True},
    )
    assert (
        "sleep_exception_requires_qualified_minimum_lighting"
        in compile_rule(unsafe, await Simulator().snapshot()).reasons
    )


@pytest.mark.asyncio
async def test_laya_cloud_output_and_native_unknown_never_mark_gateway_independent():
    snapshot = await Simulator().snapshot()
    decision = rule(
        conditions=[
            {"type": "decision", "provider": "laya_mlx", "task": "home_intent", "equals": "lighting"}
        ],
        execution={"preferred_backend": "home_core"},
    )
    result = compile_rule(decision, snapshot)
    assert result.status == "requires_core" and not result.core_independent
    for backend in ("ha", "mijia_geek"):
        assert compile_rule(rule(execution={"preferred_backend": backend}), snapshot).status == "unsupported"
    cloud = rule(
        actions=[
            {
                "type": "feedback.speak",
                "id": "ack",
                "endpoint": "SIMULATED-room",
                "route": "cloud_tts",
                "text": "SIMULATED ACK",
            }
        ],
        execution={"preferred_backend": "simulator", "require_wan_independent": True},
    )
    assert "wan_independence_unproven" in compile_rule(cloud, snapshot).reasons
    independent = rule(execution={"preferred_backend": "simulator", "require_core_independent": True})
    assert compile_rule(independent, snapshot).status == "unsupported"


@pytest.mark.asyncio
async def test_cross_midnight_window_and_delayed_stale_action():
    snapshot = await Simulator().snapshot()
    r = rule(
        conditions=[{"type": "time_window", "start": "23:00", "end": "07:00", "timezone": "Asia/Shanghai"}]
    )
    now = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
    # Explicitly align the synthetic target evidence and mode leases to the test clock.
    for state in snapshot.states:
        state.as_of = now - timedelta(hours=13)
        state.valid_until = now + timedelta(hours=1)
    assert evaluate_rule(r, snapshot, trial(snapshot, now), now=now).outcome == "matched"
    midday = now.replace(hour=4)
    assert "outside_time_window" in evaluate_rule(r, snapshot, trial(snapshot, midday), now=midday).reasons
    delayed = rule(
        actions=[
            {"type": "delay", "id": "wait", "milliseconds": 4000},
            {"type": "light.set", "id": "on", "target": "light.hall", "power": "on"},
        ]
    )
    assert (
        "delayed_action_would_be_stale"
        in evaluate_rule(delayed, snapshot, trial(snapshot, now), now=now).reasons
    )


@pytest.mark.parametrize(
    "change",
    [
        {
            "trigger": {
                "type": "snapshot",
                "entity": "binary_sensor.door",
                "capability": "contact",
                "value": "open",
            }
        },
        {
            "conditions": [
                {"type": "mode", "mode": "home.sleep", "equals": True, "require_valid_lease": False}
            ]
        },
        {"actions": [{"type": "shell", "id": "x", "script": "SIMULATED-do-not-execute"}]},
        {"actions": [{"type": "light.set", "id": "x", "target": "light.hall", "power": "toggle"}]},
        {
            "actions": [
                {"type": "light.set", "id": "x", "target": "light.hall", "power": "on", "brightness": True}
            ]
        },
        {
            "conditions": [
                {
                    "type": "numeric",
                    "entity": "sensor.living.temperature",
                    "capability": "temperature",
                    "operator": "gt",
                    "value": float("nan"),
                }
            ]
        },
    ],
)
def test_invalid_ast_and_nonfinite_values_are_rejected(change):
    with pytest.raises(ValueError):
        rule(**change)
