"""Original JavaScript editor -> typed CoreRules -> mock HA effects, no real services."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from homeplane.rule_compiler import compile_rule, evaluate_rule
from homeplane.rule_contracts import RuleIR
from homeplane.simulator import Simulator
from tests.test_homeplane_core_rules import cleanup, enable, event, fixture, proposal
from tests.test_homeplane_rule_compiler import rule, trial

PROJECT = Path(__file__).resolve().parents[1]


def original_editor(mode, options):
    result = subprocess.run(
        ["node", str(PROJECT / "tests/automation_editor_journey_fixture.cjs")],
        input=json.dumps(dict(mode=mode, options=options)),
        text=True,
        capture_output=True,
        cwd=PROJECT,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.asyncio
async def test_editor_sequence_survives_original_review_persistence_and_registered_trigger(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    try:
        original = await proposal(core, manager)
        shown = original_editor(
            "sequence",
            dict(
                existing=False,
                controlId=original.rule.actions[0].target,
                sensorId=original.rule.trigger.entity,
                triggerCapability=original.rule.trigger.capability,
            ),
        )
        drafted = RuleIR.model_validate(shown["request"]["rule"])
        assert [step.type for step in drafted.actions] == ["device.set", "delay", "device.set"]
        assert drafted.actions[1].milliseconds == 50
        assert drafted.execution.max_event_age_ms == 3050
        assert "等待 0.05 秒" in shown["review"] and not calls
        accepted = await enable(manager, original.model_copy(update={"rule": drafted}))
        assert accepted.status == "verified" and not calls
        assert manager.read(accepted.new.id).rule == drafted
        await runtime.set_mode("awake")
        incoming = event(core)
        assert (await runtime.ingest(core.adapter, incoming))["accepted"]
        assert [call.url.path for call in calls] == ["/api/services/light/turn_on"]
        await asyncio.sleep(0.06)
        await runtime.tick()
        assert [call.url.path for call in calls] == [
            "/api/services/light/turn_on",
            "/api/services/light/turn_off",
        ]
        assert runtime.status()["runs"][0]["status"] == "completed"
        assert not runtime.status()["runs"][0]["physically_confirmed"]
        await runtime.ingest(core.adapter, incoming)
        assert len(calls) == 2
    finally:
        await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_renamed_editor_action_reaches_original_minimum_and_atomic_capability_checks():
    action = dict(
        type="light.set",
        id="night",
        target="light.hall",
        power="on",
        brightness="minimum_verified",
        require_no_bright_flash=True,
    )
    shown = original_editor("rename", dict(controlId="light.hall", rule=dict(actions=[action])))
    assert shown["request"]["rule"]["actions"] == [action]
    retained = rule(
        actions=shown["request"]["rule"]["actions"],
        conditions=[dict(type="mode", mode="home.sleep", equals=True)],
        safety=dict(allow_during_sleep=True),
    )
    snapshot = await Simulator().snapshot()
    now = datetime.now(UTC)
    sample = trial(snapshot, now)
    sample.modes["home.sleep"].value = True
    evaluated = evaluate_rule(retained, snapshot, sample, now=now)
    assert evaluated.outcome == "matched"
    assert evaluated.commands[0]["desired"] == dict(power="on", brightness=2)
    snapshot.entities[0].capabilities[0].atomic_power_brightness = False
    assert compile_rule(retained, snapshot).status == "unsupported"
    snapshot.mode = "ha-read-only"
    assert compile_rule(retained.model_copy(update={"simulated": False}), snapshot).status == "unsupported"


@pytest.mark.asyncio
async def test_missing_editor_detail_preserves_brightness_for_the_actual_capability_guard():
    action = dict(
        type="device.set",
        id="dim",
        target="light.hall",
        operation="light.set",
        desired=dict(power="on", brightness=2),
    )
    shown = original_editor("rename", dict(controlId="light.hall", rule=dict(actions=[action])))
    assert shown["request"]["rule"]["actions"] == [action]
    retained = rule(actions=shown["request"]["rule"]["actions"], conditions=[])
    snapshot = await Simulator().snapshot()
    now = datetime.now(UTC)
    evaluated = evaluate_rule(retained, snapshot, trial(snapshot, now), now=now)
    assert evaluated.outcome == "matched"
    assert evaluated.commands[0]["desired"] == dict(power="on", brightness=2)
    snapshot.entities[0].capabilities[0].maximum = None
    assert compile_rule(retained, snapshot).status == "unsupported"
