"""Persistent fake-backend Saga tests. Real adapters are never mutated."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.core import HomeCore
from homeplane.rule_deployment import RemoveRuleAction, RuleApproval, RuleManager, record
from homeplane.simulator import Simulator
from homeplane.store import TraceStore


def rig(tmp_path):
    core = HomeCore(Simulator(), TraceStore(tmp_path / "SIMULATED-deployments.sqlite"))
    return core, RuleManager(core)


async def planned(manager):
    old = manager.backend.records()[0]
    return await manager.plan_remove(
        RemoveRuleAction(
            record_id=old.id,
            action_id="air-conditioner-on",
            expected_fingerprint=old.fingerprint,
            idempotency_key="SIMULATED-plan-key",
        )
    )


def approval(plan):
    return RuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest)


def close(core, manager):
    manager.close()
    core.store.close()


@pytest.mark.asyncio
async def test_plan_simulate_explicit_approve_deploy_verify_rollback_without_deleting_old(tmp_path):
    core, manager = rig(tmp_path)
    plan = await planned(manager)
    assert plan.status == "validated" and manager.backend.records()[0].enabled
    assert (await manager.apply(approval(plan))).status == "validated", "no approval before simulation"
    simulated = await manager.simulate(plan.id)
    assert simulated.status == "awaiting_approval" and len(simulated.simulation.commands) == 1
    deployed = await manager.apply(approval(simulated))
    assert (
        deployed.status == "verified"
        and not deployed.physically_confirmed
        and not deployed.real_write_attempted
    )
    assert [h["status"] for h in deployed.history] == [
        "validated",
        "awaiting_approval",
        "staging",
        "staged_disabled",
        "pausing_old",
        "old_paused",
        "activating",
        "activated",
        "verified",
    ]
    assert manager.backend.read(plan.old.id).rule == plan.old.rule
    assert not manager.backend.read(plan.old.id).enabled and manager.backend.read(plan.new_id).enabled
    assert sum(r.enabled for r in manager.backend.records()) == 1
    assert await manager.apply(approval(simulated)) == deployed, "duplicate approval never switches again"
    rolled = await manager.rollback(plan.id)
    assert rolled.status == "rolled_back" and manager.backend.read(plan.old.id) == plan.old
    assert len(manager.backend.records()) == 2 and not manager.backend.read(plan.new_id).enabled
    assert len(manager.backend.read(plan.old.id).rule.actions) == 2
    assert manager.listing()["real_rule_writes"] == "unsupported"
    close(core, manager)


@pytest.mark.asyncio
async def test_source_edit_after_preview_or_after_staging_is_never_overwritten(tmp_path):
    core, manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)
    old = plan.old
    modified = old.rule.model_copy(update={"intent": "SIMULATED independently edited source"})
    replacement = record(old.id, modified, True)
    with core.store.connection:
        core.store.connection.execute(
            "UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=?",
            (replacement.fingerprint, replacement.model_dump_json(), old.id),
        )
    result = await manager.apply(approval(plan))
    assert result.status == "failed" and result.code == "source_conflict"
    assert manager.backend.read(old.id) == replacement and manager.backend.read(plan.new_id) is None
    close(core, manager)


@pytest.mark.asyncio
async def test_staged_source_edit_preserves_edit_and_leaves_candidate_disabled(tmp_path, monkeypatch):
    core, manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)
    original = manager.backend.create_disabled
    replacement = record(plan.old.id, plan.old.rule.model_copy(update={"revision": 9}), True)

    async def stage_then_external_edit(*args):
        result = await original(*args)
        with core.store.connection:
            core.store.connection.execute(
                "UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=?",
                (replacement.fingerprint, replacement.model_dump_json(), replacement.id),
            )
        return result

    monkeypatch.setattr(manager.backend, "create_disabled", stage_then_external_edit)
    result = await manager.apply(approval(plan))
    assert result.status == "needs_reconcile" and result.code == "source_edited_no_automatic_overwrite"
    assert manager.backend.read(plan.old.id) == replacement and not manager.backend.read(plan.new_id).enabled
    close(core, manager)


@pytest.mark.asyncio
async def test_failure_between_pause_and_enable_restores_old_configuration_only(tmp_path):
    core, manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)
    manager.backend.failpoint = "enable_new"
    result = await manager.apply(approval(plan))
    assert result.status == "rolled_back" and manager.backend.read(plan.old.id) == plan.old
    assert not manager.backend.read(plan.new_id).enabled
    assert result.reconciled["physical_compensation"] is False
    close(core, manager)


@pytest.mark.asyncio
async def test_interrupted_pause_restart_reads_before_mutation_never_replays_apply(tmp_path, monkeypatch):
    core, manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)
    toggle = manager.backend.toggle

    async def interrupt_after_pause(identifier, expected, enabled):
        result = await toggle(identifier, expected, enabled)
        if identifier == plan.old.id and not enabled:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(manager.backend, "toggle", interrupt_after_pause)
    with pytest.raises(asyncio.CancelledError):
        await manager.apply(approval(plan))
    assert manager.get(plan.id).status == "needs_reconcile"
    assert not manager.backend.read(plan.old.id).enabled and not manager.backend.read(plan.new_id).enabled
    manager.close()
    manager = RuleManager(core)
    assert (await manager.apply(approval(plan))).status == "needs_reconcile"
    reconciled = await manager.reconcile(plan.id)
    assert reconciled.status == "needs_reconcile"
    assert not manager.backend.read(plan.old.id).enabled and not manager.backend.read(plan.new_id).enabled
    assert (await manager.rollback(plan.id)).status == "rolled_back"
    assert manager.backend.read(plan.old.id).enabled
    close(core, manager)


@pytest.mark.asyncio
async def test_expiry_owner_source_switch_and_duplicate_plan_are_safe(tmp_path):
    core, manager = rig(tmp_path)
    plan = await planned(manager)
    assert (await planned(manager)).id == plan.id
    plan = await manager.simulate(plan.id)
    other = RuleManager(core)
    assert not other.listing()["available"] and manager.get(plan.id).status == "awaiting_approval"
    with pytest.raises(ValueError):
        await other.apply(approval(plan))
    other.close()
    manager.deadlines[plan.id] = 0
    assert (await manager.apply(approval(plan))).status == "expired"
    assert manager.backend.read(plan.new_id) is None
    manager.close()
    manager = RuleManager(core)
    assert manager.backend.records()[0] == plan.old
    core.adapter = object()
    with pytest.raises(ValueError):
        await manager.rollback(plan.id)
    close(core, manager)


@pytest.mark.asyncio
async def test_storage_failure_prevents_backend_staging(tmp_path, monkeypatch):
    core, manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)

    def full(*_a, **_kw):
        raise OSError("SIMULATED disk full")

    monkeypatch.setattr(manager, "_save", full)
    with pytest.raises(OSError):
        await manager.apply(approval(plan))
    assert manager.backend.read(plan.new_id) is None and manager.backend.read(plan.old.id).enabled
    close(core, manager)


@pytest.mark.asyncio
async def test_user_edit_after_pause_prevents_new_activation_and_auto_restore(tmp_path,monkeypatch):
    core,manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)
    toggle = manager.backend.toggle
    edited = record(plan.old.id,plan.old.rule.model_copy(update={'intent':'SIMULATED user edit during switch'}),True)
    async def pause_then_user_edits(identifier,expected,enabled):
        result = await toggle(identifier,expected,enabled)
        if identifier == plan.old.id and not enabled:
            with core.store.connection:
                core.store.connection.execute('UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=?',
                    (edited.fingerprint,edited.model_dump_json(),edited.id))
        return result
    monkeypatch.setattr(manager.backend,'toggle',pause_then_user_edits)
    result = await manager.apply(approval(plan))
    assert result.status == 'needs_reconcile' and manager.backend.read(plan.old.id) == edited
    assert not manager.backend.read(plan.new_id).enabled
    close(core,manager)


@pytest.mark.asyncio
async def test_detect_verified_configuration_drift_without_toggling_or_losing_pending_history(tmp_path):
    core,manager = rig(tmp_path)
    plan = await manager.simulate((await planned(manager)).id)
    deployed = await manager.apply(approval(plan))
    assert deployed.status == 'verified'
    edited = record(deployed.new_id,deployed.new_rule.model_copy(update={'revision':10}),True)
    with core.store.connection:
        core.store.connection.execute('UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=?',
                                     (edited.fingerprint,edited.model_dump_json(),edited.id))
    observed = manager.listing()['deployments'][0]
    assert observed['status'] == 'needs_reconcile' and observed['code'] == 'configuration_changed_since_verify'
    assert manager.backend.read(deployed.new_id) == edited
    assert (await manager.rollback(deployed.id)).status == 'needs_reconcile'
    assert not manager.backend.read(deployed.old.id).enabled
    assert len(manager.backend.records()) == 2
    close(core,manager)


def test_api_simulator_rule_diff_approval_roundtrip_and_real_source_boundary(tmp_path):
    import httpx

    from homeplane.ha import HAReadOnlyAdapter

    app = create_app(db_path=tmp_path / "SIMULATED-api.sqlite",settings_path=tmp_path / "SIMULATED-settings.json")
    headers = {"Origin":"http://testserver"}
    with TestClient(app) as client:
        before = client.get('/api/snapshot').json()['rules']
        old = client.get('/api/rules/managed').json()['records'][0]
        request = {'record_id':old['id'],'action_id':'air-conditioner-on','expected_fingerprint':old['fingerprint'],
                   'idempotency_key':'SIMULATED-ui-preview'}
        assert client.post('/api/rules/plans/remove-action',json=request).status_code == 403
        assert client.post('/api/rules/plans/remove-action',headers={'Origin':'http://foreign.invalid'},json=request).status_code == 403
        plan = client.post('/api/rules/plans/remove-action',headers=headers,json=request).json()
        assert plan['status'] == 'validated' and len(plan['new_rule']['actions']) == 1
        assert plan['old']['rule']['actions'][0] == plan['new_rule']['actions'][0]
        ref = {'plan_id':plan['id']}
        assert client.post('/api/rules/plans/apply',headers=headers,json={**ref,'approval_digest':'0'*64}).status_code == 200
        # Before simulation, even the correct approval cannot switch configuration.
        assert client.get('/api/rules/managed').json()['records'] == [old]
        simulated = client.post('/api/rules/plans/simulate',headers=headers,json=ref).json()
        assert simulated['simulation']['real_write_attempted'] is False and simulated['status'] == 'awaiting_approval'
        assert client.post('/api/rules/plans/apply',headers=headers,json={**ref,'approval_digest':'0'*64}).status_code == 409
        result = client.post('/api/rules/plans/apply',headers=headers,json={**ref,'approval_digest':plan['approval_digest']}).json()
        assert result['status'] == 'verified' and result['real_write_attempted'] is False
        assert client.get('/api/rules/plans/'+plan['id']).json() == result
        assert client.get('/api/snapshot').json()['rules'] == before, 'Opaque vendor-style fixtures stay byte-for-byte intact'
        assert client.post('/api/rules/plans/rollback',headers=headers,json=ref).json()['status'] == 'rolled_back'
        invalid = client.post('/api/rules/compile',headers=headers,json={**old['rule'],'script':'SIMULATED-secret-do-not-echo'})
        assert invalid.status_code == 422 and 'secret' not in invalid.text
        app.state.core.adapter = HAReadOnlyAdapter('http://simulated.invalid','SIMULATED-token','SIMULATED-instance',
            transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        assert client.get('/api/rules/managed').json()['available'] is False
        assert client.post('/api/rules/plans/remove-action',headers=headers,json=request).status_code == 403
        assert client.post('/api/rules/plans/rollback',headers=headers,json=ref).status_code == 403
        assert client.get('/api/rules/plans/'+plan['id']).json()['simulated'] is True
