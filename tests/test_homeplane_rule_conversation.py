"""Actual offline Simulator rule transactions; no models, network or devices."""

import asyncio
import itertools
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from homeplane.core import HomeCore
from homeplane.home_flow_contracts import FlowRequest, TrustedFlowContext
from homeplane.home_flow_ports import RulePorts
from homeplane.query_contracts import ReadScope
from homeplane.rule_contracts import TimeCondition
from homeplane.rule_conversation import RuleConversation
from homeplane.rule_deployment import RuleManager, record
from homeplane.rule_text import RuleTextChanges
from homeplane.simulator import Simulator
from homeplane.store import TraceStore

PHRASE = "以后我回家不要自动开空调"
sequence = itertools.count()


def request(context, *, operation="SIMULATED-operation", kind="turn", **fields):
    number = next(sequence)
    return FlowRequest(
        session_id=context.session_id, turn_id=f"turn:{number}", operation_id=operation,
        idempotency_key=f"key:{number}", kind=kind, lane="rules", **fields,
    )


@pytest.fixture
def rig(tmp_path):
    core = HomeCore(Simulator(), TraceStore(tmp_path / "SIMULATED-rule-conversation.sqlite"))
    manager = RuleManager(core)
    text = RuleTextChanges(manager, None)
    conversation = RuleConversation(RulePorts(text, manager))
    state = {"current": True, "cancelled": False, "mono": time.monotonic()}
    state["scope"] = ReadScope(
        subject="console", revision="SIMULATED-read-v1", boundary="loopback_console",
        expires_at=datetime.now(UTC) + timedelta(minutes=10), permitted=True, all_home=True,
    )
    context = TrustedFlowContext(
        input_kind="console_text", session_id="SIMULATED-session", source_revision="SIMULATED-source-v1",
        authority_revision="SIMULATED-authority-v1", boot_id="SIMULATED-flow-boot", generation=1,
        deadline_mono=state["mono"] + 60, check_current=lambda _context: state["current"],
        cancelled=lambda: state["cancelled"], current_scope=lambda: state["scope"],
        monotonic=lambda: state["mono"],
    )
    yield core, manager, text, conversation, context, state
    assert conversation.pending_count == 0, "all owned original-domain tasks must have returned"
    manager.close()
    core.store.close()


async def preview(conversation, context):
    result = await conversation.turn(request(context, text=PHRASE), context)
    assert result.phase == "preview" and result.plan_id and result.approval_digest
    return result


def approval(context, result, **fields):
    return request(context, kind="approve", plan_id=result.plan_id, approval_digest=result.approval_digest,
                   expected_context=result.context_token, **fields)


@pytest.mark.asyncio
async def test_original_phrase_ambiguous_choice_exact_diff_explicit_apply_readback_and_rollback(rig):
    core, manager, _, conversation, context, _ = rig
    old = manager.backend.records()[0]
    seeded = record(old.id, old.rule.model_copy(update={
        "conditions": [TimeCondition(start="00:01", end="23:59", timezone="Asia/Shanghai")],
        "actions": [old.rule.actions[0].model_copy(update={"brightness": 11}), old.rule.actions[1]],
    }), True)
    with core.store.connection:
        core.store.connection.execute("UPDATE simulated_rules SET fingerprint=?,body=? WHERE id=?",
                                      (seeded.fingerprint, seeded.model_dump_json(), seeded.id))
    other = await manager.backend.create_disabled("SIMULATED-other-arrival", seeded.rule.model_copy(update={"id": "SIMULATED-other-arrival"}))
    await manager.backend.toggle(other.id, other.fingerprint, True)
    other = manager.backend.read(other.id)
    first = await conversation.turn(request(context, text=PHRASE), context)
    assert first.phase == "select" and len(first.choices) == 2
    assert first.domain["status"] == "clarification" and len(manager.backend.records()) == 2
    second = await conversation.turn(request(context, candidate_id=first.choices[1].id,
                                             expected_context=first.context_token), context)
    plan = manager.get(second.plan_id)
    assert second.phase == "preview" and plan.old.id == other.id
    assert plan.old.fingerprint == other.fingerprint
    assert [item.id for item in plan.new_rule.actions] == ["hall-light-on"]
    assert plan.new_rule.actions == [other.rule.actions[0]]
    assert plan.new_rule.model_dump(exclude={"revision", "intent", "actions"}) == other.rule.model_dump(exclude={"revision", "intent", "actions"})
    assert plan.status == "awaiting_approval" and second.domain["plan"]["simulation"]
    assert second.expires_at == first.expires_at and second.expires_at < plan.expires_at
    assert manager.backend.read(other.id).enabled, "preview does not deploy"
    done = await conversation.approve(approval(context, second), context)
    assert done.domain == manager.get(plan.id).model_dump(mode="json")
    assert done.domain["status"] == "verified" and done.domain["simulated"]
    assert not done.domain["physically_confirmed"] and not done.domain["real_write_attempted"]
    assert manager.backend.read(seeded.id) == seeded, "unselected arrival rule untouched"
    rolled = await conversation.turn(request(context, kind="rollback", plan_id=plan.id), context)
    assert rolled.domain["status"] == "rolled_back" and manager.backend.read(other.id).enabled
    assert rolled.domain["reconciled"]["physical_compensation"] is False
    assert "不撤销过去的物理动作" in rolled.caption
    assert done.input_kind == "console_text" and done.asr_admission == "unavailable"
    assert done.native_graph == "unavailable" and done.nas_lease == "closed"
    await conversation.close()


@pytest.mark.asyncio
async def test_candidate_and_exact_approval_cannot_cross_sessions_or_operations(rig):
    _, manager, _, conversation, context, _ = rig
    old = manager.backend.records()[0]
    other = await manager.backend.create_disabled("SIMULATED-other", old.rule.model_copy(update={"id": "SIMULATED-other"}))
    await manager.backend.toggle(other.id, other.fingerprint, True)
    choices = await conversation.turn(request(context, text=PHRASE), context)
    foreign = replace(context, session_id="SIMULATED-other-session")
    denied = await conversation.turn(request(foreign, candidate_id=choices.choices[0].id,
                                             expected_context=choices.context_token), foreign)
    assert denied.phase == "unavailable" and denied.domain is None
    result = await conversation.turn(request(context, candidate_id=choices.choices[0].id,
                                             expected_context=choices.context_token), context)
    wrong = await conversation.approve(approval(context, result, operation="SIMULATED-other-operation"), context)
    assert wrong.domain is None and wrong.code == "rule_plan_unavailable"
    assert len(manager.backend.records()) == 2 and all(item.enabled for item in manager.backend.records())
    await conversation.close()


@pytest.mark.asyncio
async def test_source_changes_after_choice_or_preview_cannot_deploy_old_diff(rig):
    _, manager, _, conversation, context, _ = rig
    result = await preview(conversation, context)
    old = manager.backend.records()[0]
    await manager.backend.toggle(old.id, old.fingerprint, False)
    before = manager.backend.records()
    refused = await conversation.approve(approval(context, result), context)
    assert refused.code == "rule_context_changed" and manager.backend.records() == before
    assert manager.get(result.plan_id).status == "awaiting_approval"
    await conversation.close()


@pytest.mark.asyncio
async def test_cached_preview_rechecks_source_and_original_plan_expiry(rig):
    _, manager, _, conversation, context, _ = rig
    original = request(context, text=PHRASE)
    result = await conversation.turn(original, context)
    old = manager.backend.records()[0]
    await manager.backend.toggle(old.id, old.fingerprint, False)
    changed = await conversation.turn(original, context)
    assert changed.code == "rule_context_changed" and changed.approval_digest is None
    assert manager.get(result.plan_id).status == "awaiting_approval"
    await conversation.close()


@pytest.mark.asyncio
async def test_original_plan_expiry_can_only_shorten_flow_window(rig):
    _, manager, _, conversation, context, _ = rig
    original = request(context, text=PHRASE)
    response = await conversation.turn(original, context)
    plan = manager.get(response.plan_id)
    shortened = plan.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    manager._save(shortened, previous=plan.status)  # original durable-domain CAS expiry injection
    replay = await conversation.turn(original, context)
    assert replay.code == "flow_expired" and replay.approval_digest is None
    assert len(manager.backend.records()) == 1
    await conversation.close()


@pytest.mark.asyncio
async def test_duplicate_pending_turn_does_not_create_or_cancel_a_second_domain_call(rig):
    core, manager, _, conversation, context, _ = rig
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = core.snapshot
    calls = []

    async def held():
        calls.append("snapshot")
        entered.set()
        await release.wait()
        return await snapshot()

    core.snapshot = held
    original = request(context, text=PHRASE)
    task = asyncio.create_task(conversation.turn(original, context))
    await asyncio.wait_for(entered.wait(), 1)
    duplicate = await conversation.turn(original, context)
    assert duplicate.code == "rule_operation_busy" and duplicate.domain is None
    assert not task.done() and conversation.pending_count == 1 and calls == ["snapshot"]
    release.set()
    result = await task
    assert result.phase == "preview" and len(manager.backend.records()) == 1
    await conversation.close()


@pytest.mark.asyncio
async def test_stale_candidate_fingerprint_requires_new_original_preview(rig):
    _, manager, _, conversation, context, _ = rig
    old = manager.backend.records()[0]
    extra = await manager.backend.create_disabled("SIMULATED-other", old.rule.model_copy(update={"id": "SIMULATED-other"}))
    await manager.backend.toggle(extra.id, extra.fingerprint, True)
    choices = await conversation.turn(request(context, text=PHRASE), context)
    await manager.backend.toggle(old.id, old.fingerprint, False)
    response = await conversation.turn(request(context, candidate_id=choices.choices[0].id,
                                               expected_context=choices.context_token), context)
    assert response.code == "rule_context_changed" and response.plan_id is None
    assert len(manager.backend.records()) == 2
    await conversation.close()


@pytest.mark.asyncio
async def test_expiry_cancel_duplicate_approval_and_restart_never_replay(rig):
    _, manager, text, conversation, context, state = rig
    result = await preview(conversation, context)
    calls = []
    apply = manager.apply

    async def counted(value):
        calls.append(value.plan_id)
        return await apply(value)

    manager.apply = counted
    done = await conversation.approve(approval(context, result), context)
    again = await conversation.approve(approval(context, result), context)
    assert done.domain == again.domain and calls == [result.plan_id]
    assert again.code == "rule_approval_already_consumed"
    restarted = RuleConversation(RulePorts(text, manager))
    lost = await restarted.approve(approval(context, result), context)
    assert lost.code == "rule_session_unavailable" and calls == [result.plan_id]
    await restarted.close()
    state["mono"] += 61
    expired = await conversation.approve(approval(context, result), context)
    assert expired.code == "flow_expired" and calls == [result.plan_id]
    await conversation.close()


@pytest.mark.asyncio
async def test_cancelled_preview_cannot_be_approved_even_with_original_digest(rig):
    _, manager, _, conversation, context, _ = rig
    result = await preview(conversation, context)
    cancelled = await conversation.cancel(request(context, kind="cancel"), context)
    assert cancelled.phase == "cancelled" and cancelled.domain["status"] == "awaiting_approval"
    refused = await conversation.approve(approval(context, result), context)
    assert refused.code == "flow_cancelled" and len(manager.backend.records()) == 1
    await conversation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pre_dispatch", "staged_disabled"])
async def test_owned_task_invalidation_preserves_original_cancel_boundary_and_no_replay(rig, stage):
    core, manager, _, conversation, context, _ = rig
    result = await preview(conversation, context)
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "pre_dispatch":
        snapshot = core.snapshot

        async def held():
            entered.set()
            await release.wait()
            return await snapshot()

        core.snapshot = held
    else:
        create = manager.backend.create_disabled

        async def held(*args):
            candidate = await create(*args)
            entered.set()
            await release.wait()
            return candidate

        manager.backend.create_disabled = held
    turn = asyncio.create_task(conversation.approve(approval(context, result), context))
    await asyncio.wait_for(entered.wait(), 1)
    cancelled = await conversation.cancel(request(context, kind="cancel"), context)
    late = await turn
    expected = "awaiting_approval" if stage == "pre_dispatch" else "needs_reconcile"
    assert cancelled.domain["status"] == expected and late.domain["status"] == expected
    assert manager.backend.read(result.domain["plan"]["old"]["id"]).enabled
    assert all(not item.enabled for item in manager.backend.records()[1:])
    count = len(manager.backend.records())
    duplicate = await conversation.approve(approval(context, result), context)
    assert duplicate.domain["status"] == expected and len(manager.backend.records()) == count
    if stage == "staged_disabled":
        reconciled = await conversation.turn(request(context, kind="reconcile", plan_id=result.plan_id), context)
        assert reconciled.domain["status"] == "needs_reconcile"
        assert reconciled.domain["reconciled"]["physical_compensation"] is False
    await conversation.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "scope", "identity", "expired"])
async def test_snapshot_await_change_or_late_result_cannot_create_preview(rig, change):
    core, manager, _, conversation, context, state = rig
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = core.snapshot

    async def held():
        entered.set()
        await release.wait()
        return await snapshot()

    core.snapshot = held
    pending = asyncio.create_task(conversation.turn(request(context, text=PHRASE), context))
    await asyncio.wait_for(entered.wait(), 1)
    if change == "scope":
        state["scope"] = replace(state["scope"], permitted=False)
    elif change == "expired":
        state["mono"] += 61
    elif change == "source":
        state["current"] = False
    else:
        conversation.invalidate(context.session_id)
    late = await asyncio.wait_for(pending, 1)
    assert late.phase in {"refused", "unavailable"} and late.plan_id is None and late.domain is None
    assert len(manager.backend.records()) == 1
    assert core.store.connection.execute("SELECT count(*) FROM rule_deployments").fetchone()[0] == 0
    await conversation.close()


@pytest.mark.asyncio
async def test_opaque_native_partial_scope_and_absent_ambient_authority_are_held(rig):
    core, manager, _, conversation, context, state = rig
    state["scope"] = replace(state["scope"], all_home=False, rooms=frozenset({"living"}))
    partial = await conversation.turn(request(context, text=PHRASE), context)
    assert partial.code == "rule_source_scope_unavailable" and partial.domain is None
    state["scope"] = replace(state["scope"], all_home=True)
    ambient = replace(context, input_kind="qualified_ambient", session_id="SIMULATED-ambient")
    withheld = await conversation.turn(request(ambient, text=PHRASE), ambient)
    assert withheld.code == "rule_write_authority_unavailable" and withheld.domain is None

    class OpaqueNative:
        pass

    core.adapter = OpaqueNative()  # isolated unqualified-source injection, no connection
    native = await conversation.turn(request(context, text=PHRASE), context)
    assert native.domain["status"] == "unsupported" and native.domain["code"] == "native_rule_source_unavailable"
    assert native.native_graph == "opaque" and native.domain["context"]["available"] is False
    assert not native.domain["real_write_attempted"] and native.plan_id is None
    assert len(manager.backend.records()) == 1
    await conversation.close()


@pytest.mark.asyncio
async def test_original_backend_failure_is_shown_as_actual_configuration_rollback(rig):
    _, manager, _, conversation, context, _ = rig
    result = await preview(conversation, context)
    toggle = manager.backend.toggle

    async def fail_old(identifier, fingerprint, enabled):
        if identifier == result.domain["plan"]["old"]["id"] and enabled is False:
            raise OSError("SIMULATED controlled failure")
        return await toggle(identifier, fingerprint, enabled)

    manager.backend.toggle = fail_old
    outcome = await conversation.approve(approval(context, result), context)
    assert outcome.domain["status"] == "rolled_back"
    assert outcome.domain == manager.get(result.plan_id).model_dump(mode="json")
    assert manager.backend.read(result.domain["plan"]["old"]["id"]).enabled
    await conversation.close()


@pytest.mark.asyncio
async def test_idempotency_conflict_turn_budget_and_stop_remain_bounded(rig):
    _, manager, _, conversation, context, _ = rig
    original = request(context, text=PHRASE)
    result = await conversation.turn(original, context)
    same = await conversation.turn(original, context)
    assert same.plan_id == result.plan_id and len(manager.backend.records()) == 1
    conflict = original.model_copy(update={"text": "以后回家不开灯"})
    assert (await conversation.turn(conflict, context)).code == "flow_idempotency_conflict"
    # Stop may always fence even when all display-turn receipts have been used.
    for number in range(16):
        await conversation.turn(request(context, kind="status", plan_id=result.plan_id), context)
    stopped = await conversation.cancel(request(context, kind="cancel"), context)
    assert stopped.phase == "cancelled" and len(conversation.sessions[context.session_id].turns) == 16
    assert len(manager.backend.records()) == 1
    await conversation.close()


def test_public_flow_json_cannot_mint_identity_permission_or_submit_a_result():
    base = dict(session_id="SIMULATED-session", turn_id="turn:1", operation_id="op:1", idempotency_key="key:1", text=PHRASE)
    for field in ("subject_ref", "track_ref", "producer", "person", "trusted", "grant", "authority", "scope", "result", "success", "input_kind"):
        with pytest.raises(ValidationError):
            FlowRequest.model_validate({**base, field: True})
