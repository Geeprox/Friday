"""Synthetic registered HA ingress + MockTransport effects, no household access.

These exercise the production adapter/authority path with explicitly SIMULATED
data; native protocol/hardware compatibility is not established by these tests.
"""

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeplane.action_contracts import CoreRuleEffect, DevicePlanRequest
from homeplane.api import create_app
from homeplane.core_rules import CoreRuleApproval, CoreRuleProposal, CoreRuleRuntime, CoreRules
from homeplane.ha_control import HADeviceWriter
from homeplane.ha_stream import HAStream
from homeplane.rule_contracts import RuleIR
from homeplane.rule_deployment import RuleManager
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import CASES, rig


async def fixture(tmp_path):
    core, rows, calls, fault, path, grant = rig(tmp_path)
    now = datetime.now(UTC)
    rows.append(
        {
            "entity_id": "binary_sensor.simulated_contact",
            "state": "off",
            "attributes": {"friendly_name": "SIMULATED trigger"},
            "last_updated": now.isoformat(),
        }
    )
    await core.snapshot()
    stream = HAStream(core.adapter)
    stream.ready = True
    stream.connection_id = "SIMULATED-connection"
    stream.first_attempt.set()
    stream.rows = {r["entity_id"]: r for r in rows}
    stream.published_rows = dict(stream.rows)
    stream.receipts = {r["entity_id"]: now for r in rows}
    stream.published_receipts = dict(stream.receipts)
    core.adapter._stream = stream
    owner = RuleManager(core)
    manager = CoreRules(core, owner)
    runtime = CoreRuleRuntime(manager)
    runtime.start = lambda: None
    return core, rows, calls, fault, path, grant, owner, manager, runtime


async def proposal(
    core, manager, operation="light.set", desired=None, *, actions=None, conditions=None, old=None
):
    snapshot = await core.snapshot()
    target = next(e for e in snapshot.entities if e.kind == operation.split(".")[0])
    trigger = next(e for e in snapshot.entities if e.kind == "binary_sensor")
    rule = RuleIR(
        id=old.rule.id if old else "SIMULATED-core-policy",
        revision=old.rule.revision + 1 if old else 1,
        intent="SIMULATED owned policy",
        trigger={
            "type": "state_transition",
            "entity": trigger.id,
            "capability": "binary_sensor.state",
            "from_value": "off",
            "to_value": "on",
        },
        actions=actions
        or [
            {
                "id": "SIMULATED-effect",
                "type": "device.set",
                "target": target.id,
                "operation": operation,
                "desired": desired or {"power": "on"},
            }
        ],
        conditions=conditions or [],
        execution={"preferred_backend": "home_core", "max_event_age_ms": 3000},
        simulated=False,
    )
    return CoreRuleProposal(
        rule=rule,
        expected_context=manager.context()["fingerprint"],
        idempotency_key=str(uuid4()),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        record_id=old.id if old else None,
        expected_fingerprint=old.fingerprint if old else None,
    )


async def enable(manager, request):
    plan = await manager.plan(request)
    plan = await manager.trial(plan.id)
    assert plan.status == "awaiting_approval", plan.trial
    assert plan.trial.simulated and not plan.trial.real_write_attempted
    return await manager.apply(CoreRuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest))


def event(core, to="on", *, age=0, registered=True):
    now = datetime.now(UTC)
    trigger = next(e for e in core.adapter._last.entities if e.kind == "binary_sensor")
    core.adapter._event(
        "state-transition",
        {
            "entity_id": trigger.id,
            "capability": "binary_sensor.state",
            "from": "off" if to == "on" else "on",
            "to": to,
            "trigger_eligible": True,
        },
        now - timedelta(seconds=age),
        source_seq="SIMULATED-connection:" + str(uuid4()),
        received_at=now,
    )
    value = core.adapter._events[-1]
    if not registered:
        core.adapter._events.pop()
    return value


async def cleanup(core, owner, runtime):
    await runtime.close()
    owner.close()
    await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,desired,domain,service", CASES)
async def test_reviewed_core_rule_dispatches_only_fixed_absolute_operation_once(
    tmp_path, operation, desired, domain, service
):
    core, rows, calls, fault, path, grant, owner, manager, runtime = await fixture(tmp_path)
    plan = await enable(manager, await proposal(core, manager, operation, desired))
    assert plan.status == "verified" and not plan.simulated and calls == []
    assert plan.compilation.status == "requires_core" and not plan.compilation.core_independent
    await runtime.set_mode("awake")
    incoming = event(core)
    assert (await runtime.ingest(core.adapter, incoming))["accepted"]
    run = runtime.get(runtime.status()["runs"][0]["id"])
    assert run.status == "completed" and run.real_write_attempted and not run.physically_confirmed
    assert len(calls) == 1 and calls[0].url.path == "/api/services/" + domain + "/" + service
    trace = core.store.light_action(run.effects[0]["action_id"])
    assert isinstance(trace.request, CoreRuleEffect) and trace.request.source_event_id == incoming.event_id
    assert trace.request.run_id == run.id and trace.status == "reported_match"
    await runtime.ingest(core.adapter, incoming)
    assert len(calls) == 1
    with pytest.raises(ValueError, match="managed_guard_required"):
        await core.actions.execute(trace.action_id)
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unknown", "sleep", "manual"])
async def test_protection_and_recovery_never_replay_old_event(tmp_path, mode):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode(mode)
    incoming = event(core)
    await runtime.ingest(core.adapter, incoming)
    assert calls == []
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, incoming)
    assert calls == []
    await runtime.ingest(core.adapter, event(core))
    assert len(calls) == 1
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_atomic_switch_retains_old_body_and_rollback_does_not_compensate_devices(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    first = await enable(manager, await proposal(core, manager))
    old = manager.read(first.new.id)
    await runtime.set_mode("awake")
    second = await enable(manager, await proposal(core, manager, desired={"power": "off"}, old=old))
    assert manager.read(old.id).rule == old.rule and not manager.read(old.id).enabled
    assert sum(r.enabled for r in manager.records()) == 1
    await runtime.ingest(core.adapter, event(core))
    assert calls == [], "configuration change invalidates old mode window"
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, event(core))
    assert len(calls) == 1
    result = await manager.rollback(second.id)
    assert result.status == "rolled_back" and manager.read(old.id) == old
    assert not manager.read(second.new.id).enabled and len(calls) == 1
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_kind", ["timeout", "mismatch", "cancel"])
async def test_send_loss_or_cancellation_is_durable_unknown_and_never_resends(tmp_path, fault_kind):
    core, _, calls, fault, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")
    if fault_kind == "mismatch":
        fault["mismatch"] = True
    else:
        fault["send"] = fault_kind
    incoming = event(core)
    if fault_kind == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runtime.ingest(core.adapter, incoming)
        await runtime.invalidate("cancelled_test")
    else:
        await runtime.ingest(core.adapter, incoming)
    assert len(calls) == 1
    run = runtime.get(runtime.status()["runs"][0]["id"])
    assert run.status == "unknown_outcome" and run.real_write_attempted
    fault.update({"send": None, "mismatch": False})
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, incoming)
    await runtime.tick()
    assert len(calls) == 1
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["authority", "gap", "binding", "clock", "record"])
async def test_waiting_actions_drop_on_context_loss_and_recovery(tmp_path, loss):
    core, rows, calls, _, path, grant, owner, manager, runtime = await fixture(tmp_path)
    snapshot = await core.snapshot()
    target = next(e for e in snapshot.entities if e.kind == "light").id
    actions = [
        {
            "type": "device.set",
            "id": "first",
            "target": target,
            "operation": "light.set",
            "desired": {"power": "on"},
        },
        {"type": "delay", "id": "pause", "milliseconds": 50},
        {
            "type": "device.set",
            "id": "second",
            "target": target,
            "operation": "light.set",
            "desired": {"power": "off"},
        },
    ]
    plan = await enable(manager, await proposal(core, manager, actions=actions))
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, event(core))
    assert len(calls) == 1 and runtime.pending
    saved = runtime.mode_deadline
    if loss == "authority":
        core.actions.pause()
    if loss == "gap":
        core.adapter._gap = True
    if loss == "binding":
        core.adapter._registry["light.simulated"]["device_id"] = "SIMULATED-rebound"
    if loss == "clock":
        runtime.mode_deadline = time.monotonic() - 1
    if loss == "record":
        await manager.rollback(plan.id)
    await runtime.tick()
    assert not runtime.pending
    if loss == "authority":
        path.write_text(grant.model_dump_json())
        path.chmod(0o600)
    if loss == "gap":
        core.adapter._gap = False
    if loss == "binding":
        core.adapter._registry["light.simulated"]["device_id"] = "device-light.simulated"
    runtime.mode_deadline = saved
    await asyncio.sleep(0.06)
    await runtime.tick()
    assert len(calls) == 1
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_registered_event_ttl_foreign_boot_and_unregistered_replay_are_rejected(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")
    for incoming in [event(core, age=40), event(core, age=-1), event(core, registered=False)]:
        assert not (await runtime.ingest(core.adapter, incoming))["accepted"]
    incoming = event(core).model_copy(update={"boot_id": "SIMULATED-foreign"})
    assert not (await runtime.ingest(core.adapter, incoming))["accepted"]
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_review_source_conflict_request_expiry_budget_and_foreign_sources(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    request = await proposal(core, manager)
    plan = await manager.plan(request)
    assert (await manager.plan(request)).id == plan.id and calls == []
    with pytest.raises(ValueError, match="source_conflict"):
        await manager.plan(
            request.model_copy(
                update={"rule": request.rule.model_copy(update={"intent": "SIMULATED changed"})}
            )
        )
    with pytest.raises(ValueError, match="ttl"):
        await manager.plan(
            request.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        )
    with pytest.raises(ValidationError):
        CoreRuleProposal(
            **{**request.model_dump(), "rule": request.rule.model_copy(update={"simulated": True})}
        )
    with pytest.raises(ValueError, match="unsupported_core_compilation"):
        await manager.plan(
            request.model_copy(
                update={
                    "idempotency_key": str(uuid4()),
                    "rule": request.rule.model_copy(
                        update={
                            "execution": request.rule.execution.model_copy(
                                update={"require_core_independent": True}
                            )
                        }
                    ),
                }
            )
        )
    await manager.trial(plan.id)
    core.actions.pause()
    with pytest.raises(ValueError, match="authority"):
        await manager.apply(CoreRuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest))
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_stale_temperature_never_controls_or_passes_trial(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    snapshot = await core.snapshot()
    sensor = next(e for e in snapshot.entities if e.kind == "sensor")
    request = await proposal(
        core,
        manager,
        conditions=[
            {
                "type": "numeric",
                "entity": sensor.id,
                "capability": "temperature",
                "operator": "gt",
                "value": 20,
                "unit": "°C",
            }
        ],
    )
    plan = await manager.plan(request)
    trial = await manager.trial(plan.id)
    assert trial.status == "validated" and "unknown_numeric_quality" in trial.trial.reasons[0]
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_cycle_ambiguous_climate_and_broad_service_shapes_do_not_compile(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    request = await proposal(core, manager)
    body = request.rule.model_dump(mode="json")
    body["trigger"]["entity"] = body["actions"][0]["target"]
    body["trigger"]["capability"] = "light.state"
    with pytest.raises(ValueError, match="cycle"):
        await manager.plan(request.model_copy(update={"rule": RuleIR.model_validate(body)}))
    body = request.rule.model_dump(mode="json")
    climate = next(e for e in (await core.snapshot()).entities if e.kind == "climate")
    body["actions"] = [
        {"type": "climate.set", "id": "SIMULATED-ambiguous", "target": climate.id, "power": "on"}
    ]
    with pytest.raises(ValueError, match="unsupported_core_compilation"):
        await manager.plan(request.model_copy(update={"rule": RuleIR.model_validate(body)}))
    for desired in [{"power": "on", "service": "reload"}, {"power": "on", "brightness": True}]:
        with pytest.raises(ValidationError):
            DevicePlanRequest(
                target="SIMULATED",
                operation="light.set",
                desired=desired,
                expected_state_version="SIMULATED",
                expires_at=datetime.now(UTC) + timedelta(seconds=1),
                idempotency_key="SIMULATED",
            )
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_restart_discards_delay_and_process_bound_approval(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    snapshot = await core.snapshot()
    target = next(e for e in snapshot.entities if e.kind == "light").id
    request = await proposal(
        core,
        manager,
        actions=[
            {"type": "delay", "id": "pause", "milliseconds": 50},
            {
                "type": "device.set",
                "id": "off",
                "target": target,
                "operation": "light.set",
                "desired": {"power": "off"},
            },
        ],
    )
    plan = await enable(manager, request)
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, event(core))
    assert runtime.pending and calls == []
    await runtime.close()
    new_manager = CoreRules(core, owner)
    new_runtime = CoreRuleRuntime(new_manager)
    new_runtime.start = lambda: None
    assert new_runtime.pending == {} and new_runtime.status()["runs"][0]["status"] == "cancelled"
    assert new_runtime.modes == {} and calls == []
    with pytest.raises(ValueError, match="expired_core_preview"):
        new_manager._live(plan)
    assert (await new_manager.rollback(plan.id)).status == "rolled_back"
    assert not new_manager.read(plan.new.id).enabled
    await cleanup(core, owner, new_runtime)


@pytest.mark.asyncio
async def test_http_review_lifecycle_never_accepts_internal_actor_or_raw_ingress(tmp_path):
    core, _, calls, _, path, _, owner, manager, runtime = await fixture(tmp_path)
    request = await proposal(core, manager)
    await runtime.close()
    owner.close()
    core.actions.close()
    core.store.close()
    app = create_app(
        core.adapter,
        db_path=tmp_path / "SIMULATED-api.sqlite",
        settings_path=tmp_path / "SIMULATED-settings.json",
        authority_path=path,
    )
    with TestClient(app, base_url="http://127.0.0.1:8740") as client:
        origin = {"Origin": "http://127.0.0.1:8740"}
        ctx = client.get("/api/rules/core/context").json()
        assert ctx["available"]
        body = {**request.model_dump(mode="json"), "expected_context": ctx["fingerprint"]}
        assert (
            client.post(
                "/api/rules/core/plans", json=body, headers={"Origin": "https://foreign.invalid"}
            ).status_code
            == 403
        )
        response = client.post("/api/rules/core/plans", json=body, headers=origin)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["status"] == "validated"
        plan = client.post("/api/rules/core/plans/trial", json={"plan_id": plan["id"]}, headers=origin).json()
        assert plan["status"] == "awaiting_approval"
        plan = client.post(
            "/api/rules/core/plans/apply",
            json={"plan_id": plan["id"], "approval_digest": plan["approval_digest"]},
            headers=origin,
        ).json()
        assert plan["status"] == "verified" and calls == []
        response = client.post(
            "/api/actions/plan",
            json={
                "target": "SIMULATED",
                "actor": "core_rule",
                "operation": "light.set",
                "desired": {"power": "on"},
                "expected_state_version": "SIMULATED",
                "expires_at": datetime.now(UTC).isoformat(),
                "idempotency_key": "SIMULATED",
            },
            headers=origin,
        )
        assert response.status_code == 422
        assert client.post(
            "/api/rules/core/runtime/event", json={"event_id": "SIMULATED"}, headers=origin
        ).status_code in {404, 405}
        client.post("/api/rules/core/runtime/mode", json={"mode": "awake"}, headers=origin)
        snapshot = client.get("/api/snapshot").json()
        target = next(e for e in snapshot["entities"] if e["kind"] == "light")
        manual = client.post(
            "/api/actions/plan",
            json={
                "target": target["id"],
                "operation": "light.set",
                "desired": {"power": "off"},
                "expected_state_version": target["state_version"],
                "idempotency_key": "SIMULATED-manual",
                "expires_at": (datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
            },
            headers=origin,
        ).json()
        assert manual["status"] == "planned" and calls == []
        executed = client.post(
            "/api/actions/execute", json={"plan_id": manual["action_id"]}, headers=origin
        ).json()
        assert executed["status"] == "reported_match" and len(calls) == 1
        assert client.get("/api/rules/core/runtime").json()["modes"] == {}
        assert (
            client.post(
                "/api/rules/core/plans/rollback", json={"plan_id": plan["id"]}, headers=origin
            ).json()["status"]
            == "rolled_back"
        )
    await core.adapter.close()


@pytest.mark.asyncio
async def test_numeric_quality_is_rechecked_after_gateway_snapshot_io(tmp_path, monkeypatch):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    sensor = next(e for e in (await core.snapshot()).entities if e.kind == "sensor")
    original_snapshot = core.snapshot
    degraded = False

    async def snapshot():
        value = await original_snapshot()
        now = datetime.now(UTC)
        states = [
            s.model_copy(
                update={
                    "value": 24,
                    "unit": "°C",
                    "quality": "stale" if degraded else "healthy",
                    "as_of": now,
                    "valid_until": now + timedelta(seconds=60),
                    "simulated": False,
                    "reasons": ["SIMULATED controlled quality fixture"],
                }
            )
            if s.id == sensor.id + ":temperature"
            else s
            for s in value.states
        ]
        return value.model_copy(update={"states": states})

    monkeypatch.setattr(core, "snapshot", snapshot)
    await enable(
        manager,
        await proposal(
            core,
            manager,
            conditions=[
                {
                    "type": "numeric",
                    "entity": sensor.id,
                    "capability": "temperature",
                    "operator": "gt",
                    "value": 20,
                    "unit": "°C",
                }
            ],
        ),
    )
    original_plan = core.actions.plan

    async def degrade_before_gateway_io(*args, **kwargs):
        nonlocal degraded
        degraded = True
        return await original_plan(*args, **kwargs)

    monkeypatch.setattr(core.actions, "plan", degrade_before_gateway_io)
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, event(core))
    assert calls == [] and not runtime.pending
    run = runtime.get(runtime.status()["runs"][0]["id"])
    trace = core.store.light_action(run.effects[0]["action_id"])
    assert trace.status == "rejected" and "unknown_numeric_quality" in trace.code
    assert trace.correlation_id == run.id
    assert trace.events[0].causation_id == run.event_id
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_operator_stop_during_gateway_io_prevents_unsent_effect(tmp_path, monkeypatch):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")
    reached, release = asyncio.Event(), asyncio.Event()
    original_plan = core.actions.plan

    async def blocked(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original_plan(*args, **kwargs)

    monkeypatch.setattr(core.actions, "plan", blocked)
    task = asyncio.create_task(runtime.ingest(core.adapter, event(core)))
    await asyncio.wait_for(reached.wait(), 1)
    stopped = asyncio.create_task(runtime.set_mode("unknown"))
    await asyncio.sleep(0)
    assert runtime.modes == {}, "stop revokes the window before waiting for the runtime lock"
    release.set()
    await asyncio.wait_for(asyncio.gather(task, stopped), 1)
    assert calls == [] and runtime.pending == {}
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_database_send_claim_failure_stops_worker_without_rpc(tmp_path, monkeypatch):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")

    original_save = core.store.save_light_action

    def fail(trace, **kwargs):
        if trace.status == "sent":
            raise sqlite3.OperationalError("SIMULATED disk failure at send claim")
        return original_save(trace, **kwargs)

    monkeypatch.setattr(core.store, "save_light_action", fail)
    runtime.enqueue(core.adapter, [event(core)])
    runtime.worker = asyncio.create_task(runtime._worker())
    await asyncio.wait_for(runtime.worker, 1)
    assert runtime.failure == "core_worker_failed" and calls == []
    assert not runtime.pending and not runtime.queue
    with pytest.raises(ValueError, match="runtime_unavailable"):
        await runtime.set_mode("awake")
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_configuration_insert_failure_restores_old_rule_atomically(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    first = await enable(manager, await proposal(core, manager))
    old = manager.read(first.new.id)
    plan = await manager.plan(await proposal(core, manager, desired={"power": "off"}, old=old))
    plan = await manager.trial(plan.id)
    core.store.connection.execute(
        "CREATE TEMP TRIGGER simulated_insert_failure BEFORE INSERT ON core_owned_rules BEGIN SELECT RAISE(ABORT,'SIMULATED insertion fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="insertion fault"):
        await manager.apply(CoreRuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest))
    assert manager.records() == [old] and manager.read(old.id).enabled and calls == []
    assert manager.plan_get(plan.id).status == "awaiting_approval"
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_old_connection_event_is_never_admitted_after_reconnect(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")
    previous = event(core)
    core.adapter._stream.connection_id = "SIMULATED-new-connection"
    assert not (await runtime.ingest(core.adapter, previous))["accepted"]
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_diagnostic_retention_keeps_old_rules_and_active_rollback_receipt(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    active = await enable(manager, await proposal(core, manager))
    draft = await manager.plan(await proposal(core, manager, old=manager.read(active.new.id)))
    past = datetime.now(UTC) - timedelta(days=8)
    with core.store.connection:
        manager._save_plan(active.model_copy(update={"created_at": past}))
        manager._save_plan(draft.model_copy(update={"created_at": past}))
    manager.prune()
    assert manager.plan_get(active.id).status == "verified"
    with pytest.raises(ValueError, match="not_found"):
        manager.plan_get(draft.id)
    assert manager.read(active.new.id).enabled and calls == []
    await manager.rollback(active.id)
    assert manager.read(active.new.id) is not None, "configuration versions are never pruned as diagnostics"
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_operator_stop_and_configuration_rollback_work_while_ha_is_offline(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    plan = await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")
    core.adapter._gap = True
    assert not manager.available()
    stopped = await runtime.set_mode("unknown")
    assert stopped["modes"] == {}
    restored = await manager.rollback(plan.id)
    assert restored.status == "rolled_back" and not manager.read(plan.new.id).enabled
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_gap_during_in_flight_effect_keeps_send_evidence_and_stops_remaining_effects(
    tmp_path, monkeypatch
):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    target = next(e for e in (await core.snapshot()).entities if e.kind == "light").id
    await enable(
        manager,
        await proposal(
            core,
            manager,
            actions=[
                {
                    "id": "SIMULATED-first",
                    "type": "device.set",
                    "target": target,
                    "operation": "light.set",
                    "desired": {"power": "on"},
                },
                {
                    "id": "SIMULATED-second",
                    "type": "device.set",
                    "target": target,
                    "operation": "light.set",
                    "desired": {"power": "off"},
                },
            ],
        ),
    )
    await runtime.set_mode("awake")
    sending, release = asyncio.Event(), asyncio.Event()
    original_send = HADeviceWriter.send

    async def pending_send(self, desired):
        sending.set()
        await release.wait()
        return await original_send(self, desired)

    monkeypatch.setattr(HADeviceWriter, "send", pending_send)
    job = asyncio.create_task(runtime.ingest(core.adapter, event(core)))
    await asyncio.wait_for(sending.wait(), 1)
    core.adapter._gap = True
    core.adapter._event("observation-gap", {"reason": "SIMULATED transport gap"}, datetime.now(UTC))
    runtime.enqueue(core.adapter, [core.adapter._events[-1]])
    release.set()
    await asyncio.wait_for(job, 1)
    run = runtime.get(runtime.status()["runs"][0]["id"])
    assert len(calls) == 1 and not runtime.pending
    assert run.status == "cancelled" and run.real_write_attempted and len(run.effects) == 1
    assert core.store.light_action(run.effects[0]["action_id"]).status == "reported_match"
    core.adapter._gap = False
    await runtime.tick()
    assert len(calls) == 1
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_second_owner_and_queue_overflow_fail_closed_without_rpc(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    other = RuleManager(core)
    second = CoreRules(core, other)
    assert not other.owned and not second.available()
    with pytest.raises(ValueError, match="owner_unavailable"):
        await second.rollback(manager.listing()["plans"][0]["id"])
    other.close()
    await runtime.set_mode("awake")
    runtime.enqueue(core.adapter, [event(core) for _ in range(33)])
    assert runtime.failure == "live_event_queue_overflow" and runtime.queue == [] and calls == []
    assert not (await runtime.ingest(core.adapter, event(core)))["accepted"]
    assert (await runtime.set_mode("unknown"))["modes"] == {}
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_source_switch_during_preview_io_creates_no_stale_plan(tmp_path, monkeypatch):
    from homeplane.simulator import Simulator

    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    request = await proposal(core, manager)
    adapter, snapshot = core.adapter, core.snapshot

    async def switch():
        value = await snapshot()
        core.adapter = Simulator()
        return value

    monkeypatch.setattr(core, "snapshot", switch)
    with pytest.raises(ValueError, match="source_conflict"):
        await manager.plan(request)
    assert core.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0] == 0
    assert calls == []
    core.adapter = adapter
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_bounded_preview_journal_refuses_more_plans_without_actuation(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    plan = await manager.plan(await proposal(core, manager))
    with core.store.connection:
        for i in range(255):
            receipt = plan.model_copy(update={"id": f"SIMULATED-capacity-{i}"})
            core.store.connection.execute(
                "INSERT INTO core_rule_plans VALUES (?,?,?,?,?)",
                (
                    receipt.id,
                    receipt.id,
                    "SIMULATED",
                    receipt.created_at.isoformat(),
                    receipt.model_dump_json(),
                ),
            )
    with pytest.raises(ValueError, match="core_plan_capacity"):
        await manager.plan(await proposal(core, manager))
    assert calls == []
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_reactivated_same_ha_identity_invalidates_old_preview(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    plan = await manager.plan(await proposal(core, manager))
    previous = core.adapter._boot
    core.adapter._boot = "SIMULATED-reactivated-source-boot"
    with pytest.raises(ValueError, match="source_conflict"):
        await manager.trial(plan.id)
    assert manager.records() == [] and calls == []
    core.adapter._boot = previous
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_buffered_transition_from_before_mode_window_cannot_act(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    await enable(manager, await proposal(core, manager))
    await runtime.set_mode("awake")
    # Fresh receipt/monotonic clock, but upstream transition predates the window.
    await runtime.ingest(core.adapter, event(core, age=1))
    assert calls == [] and not runtime.pending
    assert runtime.status()["runs"][0]["code"] == "event_precedes_current_mode_window"
    await cleanup(core, owner, runtime)
