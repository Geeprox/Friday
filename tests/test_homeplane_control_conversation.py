"""Decisive controlled-domain probes, without services, devices or credentials."""

import asyncio
import time

import pytest
from test_homeplane_actions import close, plan, rig

from homeplane.control_conversation import FencedAwait, InteractionInvalidated


async def test_original_console_snapshot_await_is_fenced_before_send_claim(tmp_path):
    core, _, _, posts, _, _, _ = rig(tmp_path)
    trace = await plan(core)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = core.snapshot
    live = [True]

    async def blocked():
        entered.set()
        await resume.wait()
        return await original()

    core.snapshot = blocked

    async def execute():
        return await FencedAwait(core.actions.execute(trace.action_id), lambda: live[0])

    task = asyncio.create_task(execute())
    await entered.wait()
    live[0] = False
    resume.set()
    with pytest.raises(InteractionInvalidated):
        await task
    assert not posts
    saved = core.store.light_action(trace.action_id)
    assert saved.status == "planned" and not saved.real_write_attempted
    # The original lock was released by cancellation; no stranded alternate owner.
    core.snapshot = original
    await asyncio.wait_for(core.actions.reconcile(trace.action_id), timeout=1)
    await close(core)


async def test_cancel_after_original_sent_claim_keeps_unknown_and_does_not_resend(tmp_path):
    core, _, _, posts, fault, _, _ = rig(tmp_path)
    trace = await plan(core)
    fault["send"] = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await FencedAwait(core.actions.execute(trace.action_id), lambda: True)
    saved = core.store.light_action(trace.action_id)
    assert saved.status == "unknown_outcome" and saved.real_write_attempted
    assert saved.physically_confirmed is False and len(posts) == 1
    assert await core.actions.execute(trace.action_id) == saved and len(posts) == 1
    await close(core)


def request(*, session="server-console-A", operation="command-1", turn="turn-1", **changes):
    from homeplane.home_flow_contracts import FlowRequest
    values = dict(session_id=session, operation_id=operation, turn_id=turn,
                  idempotency_key=operation, lane="control")
    values.update(changes)
    return FlowRequest(**values)


def flow_rig(tmp_path):
    from datetime import UTC, datetime, timedelta

    from homeplane.actions import HADeviceWriter
    from homeplane.control_conversation import ControlConversation
    from homeplane.home_flow_contracts import FlowAbort, TrustedFlowContext
    from homeplane.home_flow_ports import ActionPorts, bind_guarded_action_executor
    from homeplane.query_contracts import ReadScope

    core, row, rows, posts, fault, path, authority = rig(tmp_path)
    row["attributes"]["friendly_name"] = "客厅灯"
    scope = ReadScope(subject="synthetic-console-boundary", revision="scope-1",
                      expires_at=datetime.now(UTC) + timedelta(minutes=2), boundary="loopback_console",
                      all_home=True, permitted=True)
    state = {"source": "source-1", "authority": "authority-1", "map": "map-1", "cancelled": False,
             "scope": scope, "clock": time.monotonic, "rows": rows}
    contexts = {}
    flow_holder = []

    def context(session="server-console-A", **changes):
        values = dict(input_kind="console_text", session_id=session, source_revision=state["source"],
                      authority_revision=state["authority"], boot_id="fixture-boot", generation=1,
                      deadline_mono=time.monotonic() + 60, camera_id="console", map_revision=state["map"],
                      check_current=lambda ctx: ctx.source_revision == state["source"]
                      and ctx.authority_revision == state["authority"] and ctx.map_revision == state["map"],
                      cancelled=lambda: state["cancelled"], current_scope=lambda: state["scope"],
                      monotonic=lambda: state["clock"]())
        values.update(changes)
        result = TrustedFlowContext(**values)
        contexts[session] = result
        return result

    async def snapshot(ctx):
        scope = ctx.read_scope()
        result = await core.snapshot()
        ctx.read_scope()
        return result.model_copy(update={"entities": [e for e in result.entities
                               if scope.all_home or e.id in scope.entities or e.room in scope.rooms]})

    async def current(target, expected, ctx):
        snap = await snapshot(ctx)
        entity = next((e for e in snap.entities if e.id == target), None)
        if entity is None or entity.state_version != expected:
            raise FlowAbort("flow_context_changed")
        binding = core.actions.binding_fingerprint(entity)
        reported = await HADeviceWriter(core.adapter, entity, "light.set").readback()
        later = await snapshot(ctx)
        current_entity = next((e for e in later.entities if e.id == target), None)
        if current_entity is None or current_entity.state_version != expected:
            raise FlowAbort("flow_context_changed")
        return dict(reported=reported, received_at=reported["received_at"], expected_state_version=expected,
                    target_binding_fingerprint=binding, physically_confirmed=False)

    async def read_trace(action_id, ctx):
        ctx.read_scope()
        flow = flow_holder[0]
        owned = next((op for key, op in flow._operations.items()
                      if key[0] == ctx.session_id and op.context_key == ctx.focus_key
                      and op.trace and op.trace.action_id == action_id), None)
        if owned is None:
            raise FlowAbort("flow_read_scope_unavailable")
        value = core.store.light_action(action_id)
        ctx.read_scope()
        return value

    flow = ControlConversation(ActionPorts(core.actions, snapshot, read_trace, current,
                                           guarded_execute=bind_guarded_action_executor(core.actions)))
    flow_holder.append(flow)
    return flow, context, state, core, row, posts, fault, path


async def test_missing_guarded_execute_port_holds_without_consuming_approval(tmp_path):
    from homeplane.home_flow_ports import ActionPorts

    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    preview = await flow.plan(request(text="打开客厅灯"), ctx)
    qualified = flow.ports
    flow.ports = ActionPorts(qualified.controller, qualified.snapshot, qualified.read_trace,
                             qualified.read_current)
    approval = request(kind="approve", plan_id=preview.plan_id, approval_digest=preview.approval_digest)
    for _ in range(2):
        held = await flow.approve(approval, ctx)
        assert held.phase == "unavailable" and held.code == "control_dispatch_guard_unavailable"
        assert held.domain["status"] == "planned" and not held.domain["real_write_attempted"]
        assert not flow._operations[(ctx.session_id, approval.operation_id)].approved
        assert not posts and flow.sessions.latest is None
    flow.ports = qualified
    result = await flow.approve(approval, ctx)
    assert result.phase == "result" and result.domain["status"] == "reported_match"
    assert len(posts) == 1 and result.domain["physically_confirmed"] is False
    await flow.close()
    await close(core)


@pytest.mark.parametrize("change", ["scope", "operation_expiry", "focus"])
async def test_immediate_execute_snapshot_operation_guard_blocks_dispatch(tmp_path, change):
    from dataclasses import replace

    flow, context, state, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    preview = await flow.plan(request(text="打开客厅灯", ttl_seconds=10), ctx)
    operation = flow._operations[(ctx.session_id, "command-1")]
    original = core.snapshot

    async def immediate_snapshot():
        result = await original()
        if change == "scope":
            state["scope"] = replace(state["scope"], revision="scope-changed")
        elif change == "operation_expiry":
            expired = operation.expires_mono + 0.01
            assert expired < ctx.deadline_mono
            state["clock"] = lambda: expired
        else:
            flow.sessions.invalidate(session_id=ctx.session_id)
        return result

    core.snapshot = immediate_snapshot
    try:
        result = await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                                            approval_digest=preview.approval_digest), ctx)
        assert result.phase == "cancelled" and not posts and flow.sessions.latest is None
        saved = core.store.light_action(preview.plan_id)
        assert saved.status == "planned" and not saved.real_write_attempted
        assert saved.physically_confirmed is False
    finally:
        core.snapshot = original
        await flow.close()
        await close(core)


async def test_real_domain_console_choice_preview_approval_then_brightness_followup(tmp_path):
    flow, context, _, core, row, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    selected = await flow.plan(request(text="灯打开"), ctx)
    # A uniquely registered lamp can be named; explicit device selection is also verified.
    assert selected.phase == "preview" and not posts and flow.sessions.latest is None
    plan_id, digest = selected.plan_id, selected.approval_digest
    assert selected.domain == core.store.light_action(plan_id).model_dump(mode="json")
    approved = await flow.approve(request(kind="approve", plan_id=plan_id, approval_digest=digest), ctx)
    assert approved.phase == "result" and approved.domain["status"] == "reported_match"
    assert approved.domain["physically_confirmed"] is False and len(posts) == 1
    assert approved.input_kind == "console_text" and approved.asr_admission == "unavailable"
    # Change the actual controlled current report after execution: followup must read it now.
    row["attributes"]["brightness"] = 80
    follow = await flow.plan(request(operation="command-2", turn="turn-2", text="亮一点"), ctx)
    assert follow.phase == "preview" and follow.domain["request"]["desired"]["brightness"] == 131
    assert len(posts) == 1  # Followup remains a fresh explicit preview.
    await flow.approve(request(operation="command-2", turn="turn-3", kind="approve",
                               plan_id=follow.plan_id, approval_digest=follow.approval_digest), ctx)
    assert len(posts) == 2 and row["attributes"]["brightness"] == 131
    assert follow.native_graph == "unavailable" and follow.nas_lease == "closed"
    await flow.close()
    await close(core)


async def test_console_second_session_cannot_inherit_focus_or_use_other_plan(tmp_path):
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    a, b = context(), context("server-console-B")
    preview = await flow.plan(request(text="打开客厅灯"), a)
    await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                              approval_digest=preview.approval_digest), a)
    follow = await flow.plan(request(session=b.session_id, operation="command-2", text="亮一点"), b)
    assert follow.phase == "select" and follow.code == "target_ambiguous"
    wrong = await flow.approve(request(session=b.session_id, kind="approve", plan_id=preview.plan_id,
                                      approval_digest=preview.approval_digest), b)
    assert wrong.phase == "refused" and len(posts) == 1
    await flow.close()
    await close(core)


async def test_selection_is_not_execution_and_exact_approval_cannot_change_target(tmp_path):
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    choice = await flow.plan(request(text="亮一点"), ctx)
    assert choice.phase == "select" and choice.choices and not posts and flow.sessions.latest is None
    preview = await flow.plan(request(turn="turn-2", text="", candidate_id=choice.choices[0].id), ctx)
    # The original current state is off: selecting an entity cannot fabricate brightness success.
    assert preview.phase == "clarify" and preview.code == "brightness_state_unavailable"
    assert not posts and flow.sessions.latest is None
    await flow.close()
    await close(core)


@pytest.mark.parametrize("stage", ["read", "plan", "execute"])
@pytest.mark.parametrize("change", ["source", "authority", "map", "cancelled", "scope", "deadline"])
async def test_every_domain_await_stale_context_blocks_send_and_late_focus(tmp_path, stage, change):
    from dataclasses import replace
    flow, context, state, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    original = core.snapshot
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = [0]
    block_at = 1 if stage == "read" else 2 if stage == "plan" else 3

    async def blocked():
        calls[0] += 1
        if calls[0] == block_at:
            entered.set()
            await resume.wait()
        return await original()

    core.snapshot = blocked
    if stage == "execute":
        preview = await flow.plan(request(text="打开客厅灯"), ctx)
        assert preview.phase == "preview"
        task = asyncio.create_task(flow.approve(request(kind="approve", plan_id=preview.plan_id,
                                                       approval_digest=preview.approval_digest), ctx))
    else:
        task = asyncio.create_task(flow.plan(request(text="打开客厅灯"), ctx))
    await asyncio.wait_for(entered.wait(), 1)
    if change == "cancelled":
        state[change] = True
    elif change == "scope":
        state["scope"] = replace(state["scope"], revision="revoked", all_home=False)
    elif change == "deadline":
        deadline = ctx.deadline_mono
        state["clock"] = lambda: deadline + 1
    else:
        state[change] = "changed"
    resume.set()
    result = await task
    assert result.phase == "cancelled" and not posts and flow.sessions.latest is None
    core.snapshot = original
    await flow.close()
    await close(core)


async def test_cancelled_unknown_keeps_original_trace_and_explicit_reconcile_is_read_only(tmp_path):
    flow, context, _, core, row, posts, fault, _ = flow_rig(tmp_path)
    ctx = context()
    preview = await flow.plan(request(text="打开客厅灯"), ctx)
    fault["send"] = "cancel"
    result = await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                                       approval_digest=preview.approval_digest), ctx)
    assert result.phase == "cancelled" and len(posts) == 1
    cancelled = await flow.cancel(request(kind="cancel"), ctx)
    assert cancelled.domain["status"] == "unknown_outcome" and cancelled.code == "control_cancelled_after_send"
    assert cancelled.domain["physically_confirmed"] is False and flow.sessions.latest is None
    fault["send"] = None
    row["state"] = "on"
    reconciled = await flow.approve(request(kind="reconcile", plan_id=preview.plan_id), ctx)
    assert reconciled.domain["status"] == "reported_match" and len(posts) == 1
    assert "causality" in reconciled.domain["reported"] and flow.sessions.latest is None
    await flow.close()
    await close(core)


async def test_original_authority_refusal_and_unknown_never_become_focus(tmp_path):
    from pathlib import Path
    flow, context, _, core, _, posts, _, authority_path = flow_rig(tmp_path)
    ctx = context()
    Path(authority_path).unlink()  # Synthetic private fixture grant only.
    result = await flow.plan(request(text="打开客厅灯"), ctx)
    assert result.domain["status"] == "rejected" and result.domain["code"] == "unauthorized"
    assert result.phase == "result" and not posts and flow.sessions.latest is None
    await flow.close()
    await close(core)


async def test_duplicate_or_stale_approval_and_restart_never_reexecute(tmp_path):
    from homeplane.control_conversation import ControlConversation
    flow, context, _, core, row, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    preview = await flow.plan(request(text="打开客厅灯"), ctx)
    wrong = await flow.approve(request(kind="approve", plan_id=preview.plan_id, approval_digest="0" * 64), ctx)
    assert wrong.code == "control_approval_mismatch" and not posts
    replay = await flow.plan(request(text="打开客厅灯", turn="turn-2", ttl_seconds=60), ctx)
    assert replay.expires_at == preview.expires_at and replay.domain == preview.domain
    row["attributes"]["brightness"] = 50  # Domain CAS changes, original approval may not refresh it.
    approved = await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                                        approval_digest=preview.approval_digest), ctx)
    assert approved.domain["code"] == "source_conflict" and not posts
    repeated = await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                                        approval_digest=preview.approval_digest), ctx)
    assert repeated.phase == "result" and not posts
    restarted = ControlConversation(flow.ports)
    result = await restarted.approve(request(kind="approve", plan_id=preview.plan_id,
                                             approval_digest=preview.approval_digest), ctx)
    assert result.phase == "refused" and not posts
    await restarted.close()
    await flow.close()
    await close(core)


@pytest.mark.parametrize("text", ["不要打开客厅灯", "他说打开客厅灯", "打开客厅灯然后开门", "有点暗"])
async def test_unsupported_chat_or_mixed_intent_never_calls_domain(tmp_path, text):
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    result = await flow.plan(request(text=text), context())
    assert result.phase == "refused" and result.code == "unsupported_command"
    assert not posts and core._diagnostic_snapshot is None
    await flow.close()
    await close(core)


async def test_public_request_cannot_mint_trust_and_missing_private_port_stays_unavailable(tmp_path):
    from homeplane.control_conversation import ControlConversation
    from homeplane.home_flow_contracts import FlowRequest
    for extra in ("subject_ref", "track_ref", "trusted", "producer_ref", "authority", "scope"):
        with pytest.raises(ValueError):
            FlowRequest(**request(text="打开客厅灯").model_dump(), **{extra: True})
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    held = await ControlConversation().plan(request(text="打开客厅灯"), context())
    assert held.phase == "unavailable" and held.code == "control_port_unavailable" and not posts
    await flow.close()
    await close(core)


async def test_first_session_deadline_cannot_be_extended_by_new_context_or_retry(tmp_path):
    from dataclasses import replace
    flow, context, state, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    assert (await flow.plan(request(text="打开客厅灯"), ctx)).phase == "preview"
    old_deadline = flow._session_deadlines[ctx.session_id]
    state["clock"] = lambda: old_deadline + 1
    refreshed = replace(ctx, deadline_mono=old_deadline + 60)
    result = await flow.plan(request(operation="command-2", text="打开客厅灯"), refreshed)
    assert result.phase == "cancelled" and result.code == "flow_expired" and not posts
    await flow.close()
    await close(core)


async def test_focus_expiry_during_followup_snapshot_does_not_become_fresh_named_plan(tmp_path):
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    preview = await flow.plan(request(text="打开客厅灯"), ctx)
    await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                              approval_digest=preview.approval_digest), ctx)
    original = core.snapshot

    async def expires():
        flow.sessions.invalidate(session_id=ctx.session_id)
        return await original()

    core.snapshot = expires
    follow = await flow.plan(request(operation="command-2", text="亮一点"), ctx)
    assert follow.phase == "cancelled" and len(posts) == 1 and flow.sessions.latest is None
    core.snapshot = original
    await flow.close()
    await close(core)


async def test_missing_current_reader_holds_brightness_instead_of_reusing_old_trace(tmp_path):
    from dataclasses import replace
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    preview = await flow.plan(request(text="打开客厅灯"), ctx)
    await flow.approve(request(kind="approve", plan_id=preview.plan_id,
                              approval_digest=preview.approval_digest), ctx)
    flow.ports = replace(flow.ports, read_current=None)
    result = await flow.plan(request(operation="command-2", text="亮一点"), ctx)
    assert result.phase == "unavailable" and result.code == "brightness_state_unavailable" and len(posts) == 1
    await flow.close()
    await close(core)


async def test_trusted_fixture_ambient_can_preview_soft_gaze_but_does_not_inherit_console_authority(tmp_path):
    from test_homeplane_interaction_sessions import admitted, bound

    from homeplane.control_conversation import ControlConversation
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    binding = bound(session_id="server-console-A", room_id="Simulated Room", camera_id="fixture-camera",
                    boot_id="fixture-boot")
    admission = admitted(binding, pointing_admitted=False, gaze="not_looking")
    ctx = context(input_kind="simulated_fixture", room_id=binding.room_id, camera_id=binding.camera_id,
                  subject_ref=binding.subject_ref, track_ref=binding.track_ref, track_epoch=binding.track_epoch,
                  producer_ref=admission.producer_ref, interaction=binding, admission=admission)
    injected = ControlConversation(flow.ports, allow_simulated_admission=True)
    preview = await injected.plan(request(text="打开客厅灯"), ctx)
    assert preview.phase == "preview" and preview.input_kind == "simulated_fixture" and not posts
    held = await injected.approve(request(kind="approve", plan_id=preview.plan_id,
                                         approval_digest=preview.approval_digest), ctx)
    assert held.phase == "unavailable" and held.code == "ambient_control_authority_unavailable"
    assert not posts and injected.sessions.latest is None
    await injected.close()
    await flow.close()
    await close(core)


async def test_same_operation_concurrency_is_refused_and_cancel_cleans_its_single_owned_task(tmp_path):
    flow, context, _, core, _, posts, _, _ = flow_rig(tmp_path)
    ctx = context()
    entered, resume = asyncio.Event(), asyncio.Event()
    original = core.snapshot

    async def blocked():
        entered.set()
        await resume.wait()
        return await original()

    core.snapshot = blocked
    first = asyncio.create_task(flow.plan(request(text="打开客厅灯"), ctx))
    await asyncio.wait_for(entered.wait(), 1)
    second = await flow.plan(request(text="打开客厅灯", turn="turn-2"), ctx)
    assert second.phase == "refused" and second.code == "control_operation_in_progress"
    stopped = await flow.cancel(request(kind="cancel"), ctx)
    assert stopped.code == "control_cancelled_before_dispatch"
    assert (await first).phase == "cancelled" and not posts
    assert all(op.task is None for op in flow._operations.values())
    core.snapshot = original
    await flow.close()
    await close(core)


async def test_ambiguous_registry_device_selection_continues_original_domain_preview(tmp_path):
    import copy
    flow, context, state, core, row, posts, _, _ = flow_rig(tmp_path)
    other = copy.deepcopy(row)
    other["entity_id"] = "light.simulated_other"
    other["attributes"]["friendly_name"] = "落地灯"
    state["rows"].append(other)
    ctx = context()
    choice = await flow.plan(request(text="灯打开"), ctx)
    assert choice.phase == "select" and len(choice.choices) == 2 and not posts
    selected = next(c.id for c in choice.choices if c.id.endswith(":stable-registry-id"))
    preview = await flow.plan(request(turn="turn-2", text="", candidate_id=selected), ctx)
    assert preview.phase == "preview" and preview.domain["request"]["target"] == selected
    assert not posts and flow.sessions.latest is None
    result = await flow.approve(request(turn="turn-3", kind="approve", plan_id=preview.plan_id,
                                       approval_digest=preview.approval_digest), ctx)
    assert result.domain["status"] == "reported_match" and len(posts) == 1
    await flow.close()
    await close(core)
