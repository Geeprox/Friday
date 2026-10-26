"""Real original runtimes/authority paths over offline, explicitly synthetic data."""
import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError

from homeplane.actions import ActionController
from homeplane.feedback_contracts import RoomEndpoint, VerifiedFeedback
from homeplane.home_flow_ports import ModePorts
from homeplane.local_audio_output import MacLocalOutput
from homeplane.response_router import ResponseRouter
from homeplane.sleep_status import SleepStatus
from tests.test_homeplane_core_rules import cleanup, enable, event, fixture, proposal
from tests.test_homeplane_response_router import context, request
from tests.test_homeplane_rule_runtime import close, delayed, rig


def ports_for(runtime, *, pause=None, calls=None, permitted=None, backend="simulator"):
    calls = calls if calls is not None else []

    async def set_mode(mode, *, ttl_seconds, context):
        context.check()
        calls.append((mode, ttl_seconds))
        if backend == "simulator":
            assert ttl_seconds in {None, 30}, "Original Simulator does not accept a ttl argument."
            return await runtime.set_mode(mode)
        return await runtime.set_mode(mode, ttl=ttl_seconds if ttl_seconds is not None else 60)

    def status(context):
        context.check()
        return runtime.status()

    return ModePorts(backend, permitted or (lambda *_: True), set_mode, status, pause,
                     30 if backend == "simulator" else None)


@pytest.mark.asyncio
async def test_explicit_sleep_actual_lease_expiry_repeat_boot_and_source_never_renew(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    state = {"current": True, "cancelled": False, "source": "source-1", "authority": "authority-1"}
    ctx, calls = context(state=state, clock=clock), []
    ports = ports_for(runtime, calls=calls)
    flow = SleepStatus(ports)
    result = await flow.change(request(ctx), ctx)
    assert result.input_kind == "console_text" and result.asr_admission == "unavailable"
    assert result.domain["runtime"] == runtime.status() and result.domain["runtime"]["simulated"] is True
    assert result.domain["effective"]["mode"] == "sleep" and result.domain["effective"]["remaining_seconds"] == 30
    assert result.domain["effective"]["coverage"] == {
        "governed_only": True, "external_rules_intercepted": False,
        "core_independent": False, "wan_independent": "unverified",
    }
    original = runtime.status()["modes"]
    clock.advance(31)
    expired = await flow.status(request(ctx, kind="status"), ctx)
    assert expired.domain["runtime"]["modes"] == original and original["home.sleep"]["value"] is True
    assert expired.domain["effective"]["mode"] == "unknown" and expired.domain["effective"]["status"] == "expired"
    assert expired.domain["effective"]["remaining_seconds"] == 0
    duplicate = await flow.change(request(ctx, turn="turn-b"), ctx)
    assert duplicate.code == "mode_duplicate_no_renewal" and len(calls) == 1
    assert runtime.status()["modes"] == original
    restarted_helper = await SleepStatus(ports).status(request(ctx, kind="status"), ctx)
    assert restarted_helper.domain["effective"]["mode"] == "unknown"
    state["source"] = "source-2"
    next_ctx = replace(ctx, source_revision="source-2")
    assert (await flow.status(request(next_ctx, kind="status"), next_ctx)).domain["effective"]["mode"] == "unknown"
    assert len(calls) == 1 and not runtime.status()["worker_running"]
    await flow.close()
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_long_window_requires_bound_choice_consumes_actual_mode_without_hidden_downgrade(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    ctx, calls = context(clock=clock), []
    flow = SleepStatus(ports_for(runtime, calls=calls))
    offered = await flow.change(request(ctx, text="我要睡觉一整晚"), ctx)
    assert offered.phase == "clarify" and offered.context_token and offered.choices and not calls
    assert runtime.status()["modes"] == {}
    chosen = request(ctx, text="", key="choice-key", turn="turn-b", candidate_id=offered.choices[0].id,
                     expected_context=offered.context_token)
    result = await flow.change(chosen, ctx)
    assert result.domain["effective"]["remaining_seconds"] == 30 and calls == [("sleep", 30)]
    assert (await flow.change(chosen, ctx)).code == "mode_window_choice_stale" and len(calls) == 1
    another = await flow.change(request(ctx, text="进入手动模式", ttl_seconds=60, key="manual-key"), ctx)
    assert another.phase == "clarify" and not runtime.status()["modes"]["home.manual"]["value"]
    await flow.cancel(request(ctx, kind="cancel"), ctx)
    stale = request(ctx, text="", candidate_id=another.choices[0].id, expected_context=another.context_token)
    assert (await flow.change(stale, ctx)).code == "mode_window_choice_stale"
    with pytest.raises(ValidationError):
        request(ctx, ttl_seconds=1801)
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_unknown_off_mixed_intent_and_missing_private_permission_cannot_create_sleep(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    ctx, calls = context(clock=clock), []
    flow = SleepStatus(ports_for(runtime, calls=calls))
    assert (await flow.change(request(ctx, text="关闭灯，然后我要睡觉"), ctx)).phase == "clarify"
    assert (await flow.change(request(ctx, text="灯已经关闭了"), ctx)).phase == "clarify"
    assert (await flow.status(request(ctx, kind="status"), ctx)).domain["effective"]["mode"] == "unknown"
    assert not runtime.status()["modes"] and not calls
    denied = SleepStatus(ports_for(runtime, calls=calls, permitted=lambda *_: False))
    assert (await denied.change(request(ctx), ctx)).code == "mode_permission_required" and not calls
    absent = await SleepStatus(ModePorts()).change(request(ctx), ctx)
    assert absent.phase == "unavailable" and absent.domain is None
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_original_manual_and_pause_paths_cancel_real_simulator_pending_without_replay(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    core.actions = ActionController(core, tmp_path / "SIMULATED-no-device-grant.json")
    delayed(manager)
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter.contact_event("open"))
    assert runtime.pending and len(core.adapter.managed_actions) == 1

    async def pause(context):
        context.check()
        result = core.actions.pause()
        await runtime.invalidate("operator_revoked_actions", reset_modes=True)
        return result

    ctx = context(clock=clock)
    flow = SleepStatus(ports_for(runtime, pause=pause))
    manual = await flow.change(request(ctx, text="进入手动模式"), ctx)
    assert manual.domain["effective"]["mode"] == "manual" and not runtime.pending
    assert runtime.status()["runs"][0]["status"] == "cancelled"
    stopped = await flow.change(request(ctx, text="暂停自动操作", key="pause-key"), ctx)
    assert stopped.domain["pause"] == {"status": "paused", "action_targets": []}
    assert stopped.domain["runtime"]["modes"] == {} and stopped.domain["effective"]["mode"] == "unknown"
    clock.advance(1)
    await runtime.tick()
    assert len(core.adapter.managed_actions) == 1 and not runtime.pending
    core.actions.close()
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_synchronous_cancel_while_original_lock_awaited_blocks_mode_effect_and_late_commit(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    ctx = context(clock=clock)
    flow = SleepStatus(ports_for(runtime))
    await manager.lock.acquire()
    pending = asyncio.create_task(flow.change(request(ctx), ctx))
    for _ in range(20):
        if any(entry.task for entry in flow._operations.entries.values()):
            break
        await asyncio.sleep(0)
    stopped = await flow.cancel(request(ctx, kind="cancel"), ctx)
    manager.lock.release()
    late = await pending
    assert stopped.phase == late.phase == "cancelled" and runtime.status()["modes"] == {}
    assert flow._origin is None and not any(e.task and not e.task.done() for e in flow._operations.entries.values())
    assert (await flow.change(request(ctx), ctx)).phase == "cancelled", "Old operation cannot resume."
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_home_core_original_ttl_revision_and_authority_pause_results_stay_distinct(tmp_path):
    # Existing HA fixture uses MockTransport/temporary grants, never a household.
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    ctx = context()
    mode_calls = []

    async def pause(context):
        context.check()
        original = core.actions.pause()
        await runtime.invalidate("operator_revoked_actions")
        return original

    flow = SleepStatus(ports_for(runtime, backend="home_core", pause=pause, calls=mode_calls))
    sleep = await flow.change(request(ctx, ttl_seconds=1), ctx)
    assert sleep.domain["runtime"] == runtime.status() and sleep.domain["runtime"]["simulated"] is False
    assert sleep.domain["effective"]["mode"] == "sleep" and 0 < sleep.domain["effective"]["remaining_seconds"] <= 1
    await asyncio.sleep(1.05)
    expired = await flow.status(request(ctx, kind="status"), ctx)
    assert expired.domain["runtime"]["modes"]["home.sleep"]["value"] is True
    assert expired.domain["effective"]["status"] == "expired"
    await flow.change(request(ctx, ttl_seconds=1800, key="bounded-key"), ctx)
    assert 1799 < (await flow.status(request(ctx, kind="status"), ctx)).domain["effective"]["remaining_seconds"] <= 1800
    manager.generation += 1  # Synthetic original revision loss; no renewed lease.
    invalid = await flow.status(request(ctx, kind="status"), ctx)
    assert invalid.domain["effective"]["mode"] == "unknown" and invalid.domain["runtime"]["mode_revision_valid"] is False
    paused = await flow.change(request(ctx, text="暂停自动操作", key="pause-key"), ctx)
    assert paused.domain["pause"]["status"] == "paused" and paused.phase == "unavailable"
    assert core.actions.policy_report()["enabled"] is False and not runtime.pending and not runtime.queue
    refused = await flow.change(request(ctx, key="no-grant-key"), ctx)
    assert refused.code == "core_rule_source_or_authority_unavailable" and refused.phase == "refused"
    assert (await flow.change(request(ctx, key="no-grant-key"), ctx)).code == refused.code
    assert mode_calls == [("sleep", 1), ("sleep", 1800), ("sleep", None)] and calls == []
    await flow.close()
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_pause_preserves_original_sent_effect_evidence_without_claiming_physical_undo(tmp_path):
    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    target = next(e.id for e in (await core.snapshot()).entities if e.kind == "light")
    actions = [
        {"type": "device.set", "id": "first", "target": target, "operation": "light.set", "desired": {"power": "on"}},
        {"type": "delay", "id": "wait", "milliseconds": 200},
        {"type": "device.set", "id": "last", "target": target, "operation": "light.set", "desired": {"power": "off"}},
    ]
    await enable(manager, await proposal(core, manager, actions=actions))
    await runtime.set_mode("awake")
    await runtime.ingest(core.adapter, event(core))
    assert runtime.pending and len(calls) == 1

    async def pause(context):
        context.check()
        result = core.actions.pause()
        await runtime.invalidate("operator_revoked_actions")
        return result

    ctx = context()
    flow = SleepStatus(ports_for(runtime, backend="home_core", pause=pause))
    result = await flow.change(request(ctx, text="暂停自动操作"), ctx)
    assert result.domain["pause"]["external_rules_changed"] is False
    original_run = runtime.status()["runs"][0]
    assert original_run["real_write_attempted"] is True and original_run["physically_confirmed"] is False
    assert original_run["status"] in {"cancelled", "unknown_outcome"} and not runtime.pending
    await runtime.tick()
    assert len(calls) == 1, "Pause must not undo or resend the first original sent action."
    await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_real_offline_text_runtime_feedback_flow_keeps_output_held_through_expiry_and_pause(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    core.actions = ActionController(core, tmp_path / "SIMULATED-offline-authority.json")

    async def pause(context):
        context.check()
        result = core.actions.pause()
        await runtime.invalidate("operator_revoked_actions", reset_modes=True)
        return result

    ctx = context(clock=clock)
    flow = SleepStatus(ports_for(runtime, pause=pause))
    local = MacLocalOutput(monotonic=lambda: clock.mono)  # Real adapter; no installation, so no spawn.
    router = ResponseRouter({ctx.room_id: RoomEndpoint(ctx.room_id, "mac-a", "map-1")}, {"mac-a": local},
                            verify_endpoint=lambda endpoint, current: endpoint.room_id == current.room_id)

    async def deliver(actual):
        canonical = actual.model_dump(mode="json")
        verified = VerifiedFeedback.from_response(actual, ctx, verify_report=lambda candidate, current:
                                                 candidate.model_dump(mode="json") == canonical
                                                 and candidate.domain["runtime"] == runtime.status())
        return await router.deliver(verified, ctx)

    asleep = await deliver(await flow.change(request(ctx), ctx))
    assert asleep.domain["report"]["runtime"]["simulated"] is True
    assert asleep.domain["report"]["effective"]["mode"] == "sleep"
    assert asleep.domain["output"]["code"] == "local_output_installation_unqualified"
    assert not asleep.domain["output"]["attempted"] and asleep.output_audibility == "unavailable"
    clock.advance(31)
    expired = await deliver(await flow.status(request(ctx, kind="status", turn="turn-b", operation="operation-b"), ctx))
    assert expired.domain["report"]["runtime"]["modes"]["home.sleep"]["value"] is True
    assert expired.domain["report"]["effective"]["status"] == "expired" and "剩余 0" in expired.caption
    paused = await deliver(await flow.change(request(ctx, text="暂停自动操作", key="pause-key",
                                                    turn="turn-c", operation="operation-c"), ctx))
    assert paused.domain["report"]["pause"]["status"] == "paused"
    assert paused.domain["report"]["runtime"]["modes"] == {} and local._active is None
    assert paused.asr_admission == "unavailable" and paused.native_graph == "unavailable" and paused.nas_lease == "closed"
    await router.close()
    await flow.close()
    core.actions.close()
    await close(core, manager, runtime)


@pytest.mark.asyncio
async def test_unconfirmed_pause_and_oversized_original_status_cannot_be_presented_as_success(tmp_path):
    core, manager, runtime, clock = rig(tmp_path)
    ctx = context(clock=clock)

    async def unconfirmed(context):
        context.check()
        return {"status": "unknown_outcome", "code": "SIMULATED-pause-unconfirmed"}

    flow = SleepStatus(ports_for(runtime, pause=unconfirmed))
    failed = await flow.change(request(ctx, text="暂停自动操作"), ctx)
    assert failed.domain["pause"]["status"] == "unknown_outcome"
    assert "未确认" in failed.caption and "pending 已进入" not in failed.caption
    ports = ports_for(runtime)
    oversized = replace(ports, status=lambda context: {**runtime.status(), "SIMULATED-oversized": "x" * 70000})
    held = await SleepStatus(oversized).status(request(ctx, kind="status"), ctx)
    assert held.code == "flow_response_budget" and held.phase == "refused" and held.domain is None
    assert "不重复执行" in held.caption and runtime.status()["modes"] == {}
    await close(core, manager, runtime)
