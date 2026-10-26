"""Actual fake-actuator state transitions. All data and effects are simulated."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.core import HomeCore
from homeplane.rule_deployment import RemoveRuleAction, RuleApproval, RuleManager, record
from homeplane.rule_runtime import RuleRuntime
from homeplane.simulator import Simulator
from homeplane.store import TraceStore


class Clock:
    def __init__(self):
        self.wall = datetime.now(UTC)
        self.mono = 1000.0

    def advance(self, seconds, *, wall=True):
        self.mono += seconds
        if wall:
            self.wall += timedelta(seconds=seconds)


def rig(tmp_path, *, live=False):
    clock = Clock()
    kwargs = {} if live else {"wall_now": lambda: clock.wall, "monotonic": lambda: clock.mono}
    core = HomeCore(Simulator(**kwargs), TraceStore(tmp_path / "SIMULATED-runtime.sqlite"))
    manager = RuleManager(core)
    runtime = RuleRuntime(manager, **kwargs)
    if not live:
        runtime.start = lambda: None
    return core, manager, runtime, clock


def delayed(manager, *, condition=None, duration=500):
    original = manager.backend.records()[0]
    body = original.rule.model_dump(mode="json")
    body["actions"] = [
        {
            "type": "light.set",
            "id": "minimum-on",
            "target": "light.hall",
            "power": "on",
            "brightness": "minimum_verified",
            "require_no_bright_flash": True,
        },
        {"type": "delay", "id": "wait", "milliseconds": duration},
        {"type": "cancel_pending", "id": "cancel-own-only"},
        {"type": "light.set", "id": "off", "target": "light.hall", "power": "off"},
    ]
    body["conditions"] = condition or []
    updated = record(original.id, type(original.rule).model_validate(body), True)
    with manager.store.connection:
        manager.store.connection.execute(
            "UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=?",
            (updated.fingerprint, updated.model_dump_json(), updated.id),
        )
    return updated


async def close(core, manager, runtime):
    await runtime.close()
    manager.close()
    core.store.close()


@pytest.mark.asyncio
async def test_deployed_remove_ac_changes_actual_simulator_effects_and_keeps_other_actions(tmp_path):
    core, manager, runtime, _ = rig(tmp_path)
    old = manager.backend.records()[0]
    plan = await manager.plan_remove(
        RemoveRuleAction(
            record_id=old.id,
            action_id="air-conditioner-on",
            expected_fingerprint=old.fingerprint,
            idempotency_key="SIMULATED-remove-ac",
        )
    )
    plan = await manager.simulate(plan.id)
    assert core.adapter.managed_actions == [], "preview is not execution"
    assert (
        await manager.apply(RuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest))
    ).status == "verified"
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    assert (await runtime.ingest(event))["admitted_count"] == 1
    assert [a["command"]["type"] for a in core.adapter.managed_actions] == ["light.set"]
    snapshot = await core.snapshot()
    assert next(s.value for s in snapshot.states if s.id == "light.hall:power") == "on"
    trace = runtime.get(runtime.status()["runs"][0]["id"])
    assert trace.status == "completed" and trace.simulated and not trace.real_write_attempted
    assert manager.backend.read(old.id).rule == old.rule, "old version never deleted"
    assert len(manager.backend.read(plan.new_id).rule.actions) == 1
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_delay_has_real_minimum_state_then_off_and_ignores_duplicates(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    assert runtime.status()["pending_count"] == 1
    assert core.adapter.managed_values[("light.hall", "power")] == "on"
    assert core.adapter.managed_values[("light.hall", "brightness")] == 2
    assert not any(a["command"]["desired"].get("brightness", 2) > 2 for a in core.adapter.managed_actions)
    clock.advance(0.2)
    await runtime.ingest(event)
    await runtime.tick()
    assert len(core.adapter.managed_actions) == 1
    clock.advance(0.31)
    await runtime.tick()
    assert core.adapter.managed_values[("light.hall", "power")] == "off"
    assert runtime.status()["runs"][0]["status"] == "completed"
    assert len(core.adapter.managed_actions) == 2
    assert "cancel_scope_same_rule_only" in {h["code"] for h in runtime.status()["runs"][0]["history"]}
    assert runtime.status()["external_rules_intercepted"] is False
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_new_nonmatching_door_event_cancels_delay_and_duplicate_cannot_cancel_new_run(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    first = core.adapter.contact_event("open")
    await runtime.ingest(first)
    clock.advance(0.1)
    await runtime.ingest(core.adapter.contact_event("closed"))
    assert not runtime.pending
    await runtime.ingest(core.adapter.contact_event("open"))
    assert runtime.pending
    await runtime.ingest(first)
    assert runtime.pending, "old duplicate cannot interrupt the new delay"
    clock.advance(0.6)
    await runtime.tick()
    assert len(core.adapter.managed_actions) == 3
    await close(core, manager, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unknown", "sleep", "manual"])
async def test_guard_suppression_does_not_replay_when_awake(tmp_path, mode):
    core, manager, runtime, _ = rig(tmp_path)
    await runtime.set_mode(mode)
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    assert core.adapter.managed_actions == []
    await runtime.set_mode("awake")
    await runtime.ingest(event)
    assert core.adapter.managed_actions == []
    await runtime.ingest(core.adapter.contact_event("closed"))
    await runtime.ingest(core.adapter.contact_event("open"))
    assert len(core.adapter.managed_actions) == 2
    await close(core, manager, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["gap", "numeric", "manual", "paused", "source"])
async def test_pending_context_loss_is_terminal_and_recovery_cannot_revive(tmp_path, loss):
    core, manager, runtime, clock = rig(tmp_path)
    condition = (
        [
            {
                "type": "numeric",
                "entity": "sensor.living.temperature",
                "capability": "temperature",
                "operator": "gt",
                "value": 20,
                "unit": "°C",
            }
        ]
        if loss == "numeric"
        else []
    )
    item = delayed(manager, condition=condition)
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter.contact_event("open"))
    assert len(core.adapter.managed_actions) == 1
    if loss in {"gap", "numeric"}:
        core.adapter.set_scenario("transport-gap" if loss == "gap" else "stale-temperature")
    elif loss == "manual":
        await runtime.set_mode("manual")
    elif loss == "paused":
        await manager.backend.toggle(item.id, item.fingerprint, False)
    else:
        core.adapter = Simulator(wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    await runtime.tick()
    assert not runtime.pending
    assert any(r["status"] == "cancelled" for r in runtime.status()["runs"])
    clock.advance(0.6)
    if loss in {"gap", "numeric"}:
        core.adapter.set_scenario("recovered")
    elif loss == "manual":
        await runtime.set_mode("awake")
    elif loss == "paused":
        await manager.backend.toggle(item.id, manager.backend.read(item.id).fingerprint, True)
    await runtime.tick()
    assert len(core.adapter.managed_actions) == (0 if loss == "source" else 1)
    await close(core, manager, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_change", ["resume", "wall_backward", "monotonic_only"])
async def test_expiry_and_clock_discontinuity_prevent_late_dispatch(tmp_path, clock_change):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter.contact_event("open"))
    if clock_change == "wall_backward":
        clock.wall -= timedelta(seconds=1)
    else:
        clock.advance(4, wall=clock_change != "monotonic_only")
    await runtime.tick()
    assert len(core.adapter.managed_actions) == 1 and not runtime.pending
    clock.advance(1)
    await runtime.tick()
    assert len(core.adapter.managed_actions) == 1
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_restarted_runtime_recovers_only_trace_not_timer_or_command(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    # Model a process death after the durable waiting claim, without close.
    manager.close()
    second = RuleManager(core)
    resumed = RuleRuntime(second, wall_now=lambda: clock.wall, monotonic=lambda: clock.mono)
    resumed.start = lambda: None
    assert resumed.status()["runs"][0]["code"] == "restart_discards_pending_never_replays"
    clock.advance(0.6)
    await resumed.set_mode("awake")
    await resumed.ingest(event)
    await resumed.tick()
    assert len(core.adapter.managed_actions) == 1
    runtime.pending.clear()
    await close(core, second, resumed)


@pytest.mark.asyncio
async def test_unknown_actuator_outcome_is_not_retried(tmp_path, monkeypatch):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    calls = []

    def failed(command, **kwargs):
        calls.append(command)
        raise OSError("SIMULATED response lost after send")

    monkeypatch.setattr(core.adapter, "apply_managed", failed)
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    await runtime.ingest(event)
    clock.advance(1)
    await runtime.tick()
    assert len(calls) == 1 and not runtime.pending
    assert runtime.status()["runs"][0]["status"] == "unknown_outcome"
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_real_async_scheduler_runs_delay_without_another_http_or_tick(tmp_path):
    core, manager, runtime, _ = rig(tmp_path, live=True)
    delayed(manager, duration=150)
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter.contact_event("open"))
    assert core.adapter.managed_values[("light.hall", "power")] == "on"

    async def wait_completed():
        while runtime.pending:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait_completed(), 1)
    assert core.adapter.managed_values[("light.hall", "power")] == "off"
    assert runtime.worker and not runtime.worker.done()
    await close(core, manager, runtime)
    assert runtime.worker is None


def test_api_requires_local_origin_and_explicit_simulated_modes_and_returns_trace(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED-api.sqlite", settings_path=tmp_path / "settings.json")
    origin = "http://127.0.0.1:8740"
    with TestClient(app, base_url=origin) as client:
        assert client.post("/api/rules/runtime/mode", json={"mode": "awake"}).status_code == 403
        assert (
            client.post(
                "/api/rules/runtime/contact",
                json={"contact": "open", "event_id": "manufactured"},
                headers={"origin": origin},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/rules/runtime/mode", json={"mode": "awake"}, headers={"origin": origin}
            ).status_code
            == 200
        )
        response = client.post(
            "/api/rules/runtime/contact", json={"contact": "open"}, headers={"origin": origin}
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["event_created"] and report["runtime"]["runs"][0]["status"] == "completed"
        run = client.get("/api/rules/runs/" + report["runtime"]["runs"][0]["id"]).json()
        assert len(run["effects"]) == 2 and run["real_write_attempted"] is False
        assert (
            client.post(
                "/api/rules/runtime/contact", json={"contact": "open"}, headers={"origin": origin}
            ).json()["event_created"]
            is False
        )


@pytest.mark.asyncio
async def test_replay_foreign_and_manufactured_events_never_reach_actuator(tmp_path):
    core, manager, runtime, _ = rig(tmp_path)
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    assert not (await runtime.ingest(event, replay=True))["accepted"]
    for changes in ({"boot_id": "foreign"}, {"event_id": "manufactured"}, {"simulated": False}):
        assert not (await runtime.ingest(event.model_copy(update=changes)))["accepted"]
    assert not core.adapter.managed_actions
    assert (await runtime.ingest(event))["admitted_count"] == 1
    assert len(core.adapter.managed_actions) == 2
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_expiring_mode_lease_cancels_live_delay_and_renewal_never_replays(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    clock.advance(29.8)
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    assert len(core.adapter.managed_actions) == 1
    clock.advance(0.6)
    await runtime.tick()
    assert not runtime.pending
    assert runtime.status()["runs"][0]["status"] == "cancelled"
    await runtime.set_mode("awake")
    await runtime.ingest(event)
    assert len(core.adapter.managed_actions) == 1
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_same_event_cannot_execute_again_after_new_rule_version(tmp_path):
    core, manager, runtime, _ = rig(tmp_path)
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    original = manager.backend.records()[0]
    plan = await manager.plan_remove(
        RemoveRuleAction(
            record_id=original.id,
            action_id="air-conditioner-on",
            expected_fingerprint=original.fingerprint,
            idempotency_key="SIMULATED-new-version",
        )
    )
    plan = await manager.simulate(plan.id)
    assert (
        await manager.apply(RuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest))
    ).status == "verified"
    assert (await runtime.ingest(event))["admitted_count"] == 0
    assert len(core.adapter.managed_actions) == 2
    await runtime.ingest(core.adapter.contact_event("closed"))
    await runtime.ingest(core.adapter.contact_event("open"))
    assert len(core.adapter.managed_actions) == 3
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_snapshot_failure_cancels_delay_and_cannot_recover_old_action(tmp_path, monkeypatch):
    core, manager, runtime, clock = rig(tmp_path)
    delayed(manager)
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    original_snapshot = core.snapshot

    async def failed():
        raise TimeoutError("SIMULATED snapshot deadline")

    monkeypatch.setattr(core, "snapshot", failed)
    await runtime.tick()
    assert runtime.status()["available"] is False
    assert runtime.worker_failure == "runtime_snapshot_failed" and not runtime.pending
    monkeypatch.setattr(core, "snapshot", original_snapshot)
    clock.advance(0.6)
    await runtime.tick()
    assert len(core.adapter.managed_actions) == 1
    assert (await runtime.ingest(event))["code"] == "runtime_unavailable"
    with pytest.raises(ValueError, match="runtime_unavailable"):
        await runtime.set_mode("awake")
    assert (await core.snapshot()).mode == "simulator", "ordinary data/control remain available"
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_second_runtime_owner_cannot_consume_or_change_modes(tmp_path):
    core, manager, runtime, _ = rig(tmp_path)
    second = RuleManager(core)
    other = RuleRuntime(second)
    assert not second.owned and not other.status()["available"]
    event = core.adapter.contact_event("open")
    with pytest.raises(ValueError, match="simulation_only_or_owner_unavailable"):
        await other.ingest(event)
    with pytest.raises(ValueError, match="simulation_only_or_owner_unavailable"):
        await other.set_mode("awake")
    assert not core.adapter.managed_actions
    await other.close()
    second.close()
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_completed_diagnostics_expire_without_new_trigger(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    await runtime.set_mode("awake")
    event = core.adapter.contact_event("open")
    await runtime.ingest(event)
    run = runtime.status()["runs"][0]
    journal = core.store.recent_events(simulated=True)
    assert any(e.causation_id == event.event_id and e.correlation_id == run["id"] for e in journal)
    clock.advance(8 * 86400)
    assert not runtime.status()["runs"]
    with pytest.raises(ValueError, match="not_found"):
        runtime.get(run["id"])
    assert not (await runtime.ingest(event))["accepted"]
    assert len(core.adapter.managed_actions) == 2
    await close(core, manager, runtime)
