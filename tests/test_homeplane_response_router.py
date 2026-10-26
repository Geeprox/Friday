"""Private report/room lifecycle and actual output adapter with controlled children."""
import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from homeplane.feedback_contracts import RoomEndpoint, VerifiedFeedback
from homeplane.home_flow_contracts import FlowRequest, FlowResponse, TrustedFlowContext
from homeplane.local_audio_output import MacLocalOutput
from homeplane.response_router import ResponseRouter
from tests.test_homeplane_local_audio_output import Child, Spawn, installation
from tests.test_homeplane_local_audio_output import restore_afplay as restore_afplay


def context(*, session="session-a", room="room-a", state=None, clock=None):
    state = state if state is not None else {"current": True, "cancelled": False, "source": "source-1", "authority": "authority-1"}
    mono = (lambda: clock.mono) if clock else time.monotonic
    wall = (lambda: clock.wall) if clock else (lambda: datetime.now(UTC))
    return TrustedFlowContext(
        input_kind="console_text", session_id=session, source_revision=state["source"],
        authority_revision=state["authority"], boot_id="SIMULATED-boot", generation=1,
        deadline_mono=mono() + 60, room_id=room, map_revision="map-1",
        check_current=lambda ctx: state["current"] and state["source"] == ctx.source_revision and state["authority"] == ctx.authority_revision,
        cancelled=lambda: state["cancelled"], current_scope=lambda: None,
        monotonic=mono, wall_now=wall,
    )


def request(ctx, *, kind="turn", text="我要睡觉", key="key-a", turn="turn-a", operation="operation-a", **kwargs):
    return FlowRequest(session_id=ctx.session_id, turn_id=turn, operation_id=operation,
                       idempotency_key=key, kind=kind, text=text, lane="mode", **kwargs)


def report(ctx, *, phase="result", status="failed", operation="operation-a", turn="turn-a", expires_at=None):
    return FlowResponse(
        session_id=ctx.session_id, turn_id=turn, operation_id=operation, phase=phase,
        code="SIMULATED-domain-result", input_kind=ctx.input_kind, caption="模拟领域结果。",
        expires_at=expires_at,
        domain={"status": status, "code": "SIMULATED-original", "simulated": True,
                "real_write_attempted": False, "physically_confirmed": False,
                "coverage": {"external_rules_intercepted": False}},
    )


def receipt(response, ctx):
    canonical = response.model_dump(mode="json")
    return VerifiedFeedback.from_response(response, ctx, verify_report=lambda candidate, current:
                                          candidate.model_dump(mode="json") == canonical and current.session_id == ctx.session_id)


def router_for(ctx, output, *, quiet=False, minimum_priority=0):
    endpoint = RoomEndpoint(ctx.room_id, "mac-a", "map-1", 0.2, quiet, minimum_priority)
    return ResponseRouter({ctx.room_id: endpoint}, {"mac-a": output},
                          verify_endpoint=lambda selected, current: selected.room_id == current.room_id)


@pytest.mark.asyncio
async def test_no_endpoint_preserves_original_truth_and_public_json_cannot_supply_result():
    ctx = context()
    original = report(ctx)
    routed = await ResponseRouter().deliver(receipt(original, ctx), ctx)
    assert routed.domain["report"] == original.domain
    assert routed.domain["output"]["code"] == "room_output_unavailable"
    assert routed.output_audibility == "unavailable" and "字幕" in routed.caption
    assert routed.domain["report"]["physically_confirmed"] is False
    with pytest.raises(ValidationError):
        FlowRequest(**request(ctx).model_dump(), result={"success": True})
    refused = await ResponseRouter().deliver({"success": True}, ctx)
    assert refused.code == "feedback_receipt_required" and refused.domain is None


@pytest.mark.asyncio
async def test_quiet_explicit_room_and_scope_mismatch_never_fallback_to_another_room(tmp_path):
    ctx = context()
    a, b = Spawn(), Spawn()
    outputs = {"mac-a": MacLocalOutput(installation(tmp_path), spawn=a, simulated=True),
               "mac-b": MacLocalOutput(installation(tmp_path), spawn=b, simulated=True)}
    router = ResponseRouter(
        {"room-a": RoomEndpoint("room-a", "mac-a", "map-1", quiet=True),
         "room-b": RoomEndpoint("room-b", "mac-b", "map-1")}, outputs,
        verify_endpoint=lambda endpoint, current: endpoint.room_id == current.room_id,
    )
    good = receipt(report(ctx), ctx)
    quiet = await router.deliver(good, ctx)
    assert quiet.domain["output"]["status"] == "quiet" and not a.calls and not b.calls
    other = replace(ctx, room_id="room-b")
    refused = await router.deliver(good, other)
    assert refused.phase == "cancelled" and refused.domain is None and not b.calls
    with pytest.raises(ValueError, match="cross_room"):
        ResponseRouter({"room-a": RoomEndpoint("room-a", "same", "map-1"),
                        "room-b": RoomEndpoint("room-b", "same", "map-1")})
    await router.close()


@pytest.mark.asyncio
async def test_call_completion_duplicate_failed_and_deadline_degrade_without_device_success(tmp_path):
    ctx = context()
    spawn = Spawn(Child(), Child(7), Child(blocked=True))
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01)
    router = router_for(ctx, output)
    first = receipt(report(ctx), ctx)
    done = await router.deliver(first, ctx)
    assert done.domain["output"]["completed"] is True and done.output_audibility == "unverified"
    assert done.domain["report"]["status"] == "failed", "Audio exit zero cannot change device result."
    duplicate = await router.deliver(first, ctx)
    assert duplicate.domain["output"]["status"] == "duplicate" and len(spawn.calls) == 1
    failed = await router.deliver(receipt(report(ctx, operation="operation-b"), ctx), ctx)
    assert failed.domain["output"]["status"] == "failed" and "输出失败" in failed.caption
    expired_audio = report(ctx, operation="operation-c", expires_at=datetime.now(UTC) + timedelta(seconds=0.06))
    timed = await router.deliver(receipt(expired_audio, ctx), ctx)
    assert timed.domain["output"]["status"] == "timeout" and not timed.domain["output"]["completed"]
    assert timed.domain["output"]["cleanup"] == "reaped" and len(spawn.calls) == 3
    await router.close()


@pytest.mark.asyncio
async def test_source_change_during_playback_ignores_late_success_and_reaps(tmp_path):
    state = {"current": True, "cancelled": False, "source": "source-1", "authority": "authority-1"}
    ctx = context(state=state)
    child, spawn = Child(blocked=True), Spawn()
    spawn.children = [child]
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01)
    router = router_for(ctx, output)
    pending = asyncio.create_task(router.deliver(receipt(report(ctx, status="reported_match"), ctx), ctx))
    await spawn.entered.wait()
    router.invalidate(ctx.session_id)  # App must do this synchronously before changing binding.
    state["source"] = "source-2"
    child.finished.set()
    late = await pending
    assert late.phase == "cancelled" and late.domain is None and late.output_audibility == "unavailable"
    assert child.waited == 1 and output._active is None
    await router.close()


@pytest.mark.asyncio
async def test_cancel_session_does_not_cancel_other_room_session(tmp_path):
    a, b = context(), context(session="session-b", room="room-b")
    first, second = Child(blocked=True), Child(blocked=True)
    spawn_a, spawn_b = Spawn(first), Spawn(second)
    out_a = MacLocalOutput(installation(tmp_path), spawn=spawn_a, simulated=True, cleanup_timeout=0.01)
    out_b = MacLocalOutput(installation(tmp_path), spawn=spawn_b, simulated=True, cleanup_timeout=0.01)
    router = ResponseRouter({"room-a": RoomEndpoint("room-a", "mac-a", "map-1"),
                             "room-b": RoomEndpoint("room-b", "mac-b", "map-1")},
                            {"mac-a": out_a, "mac-b": out_b}, verify_endpoint=lambda *_: True)
    pa = asyncio.create_task(router.deliver(receipt(report(a), a), a))
    pb = asyncio.create_task(router.deliver(receipt(report(b), b), b))
    await spawn_a.entered.wait()
    await spawn_b.entered.wait()
    stopped = await router.cancel(request(a, kind="cancel"), a)
    assert stopped.phase == "cancelled" and (await pa).phase == "cancelled"
    assert not pb.done() and not second.terminated
    second.finished.set()
    assert (await pb).domain["output"]["completed"] is True
    await router.close()


@pytest.mark.asyncio
async def test_higher_priority_failure_preempts_feedback_only(tmp_path):
    ctx = context()
    low_child, high_child = Child(blocked=True), Child()
    spawn = Spawn(low_child, high_child)
    output = MacLocalOutput(installation(tmp_path), spawn=spawn, simulated=True, cleanup_timeout=0.01)
    router = router_for(ctx, output)
    low = asyncio.create_task(router.deliver(receipt(report(ctx, phase="preview", status="planned"), ctx), ctx))
    await spawn.entered.wait()
    high = await router.deliver(receipt(report(ctx, phase="refused", operation="operation-b"), ctx), ctx)
    assert (await low).phase == "cancelled" and low_child.terminated >= 1
    assert high.domain["feedback"]["requires_attention"] is True
    assert high.domain["report"]["status"] == "failed" and high.domain["output"]["completed"] is True
    assert high.domain["feedback"]["prompt_id"] == "failure"
    assert spawn.asset_sha256[-1] == installation(tmp_path).prompts["failure"].sha256
    await router.close()


@pytest.mark.asyncio
async def test_unconfirmed_pause_remains_attention_required_under_quiet_degradation():
    ctx = context()
    original = report(ctx).model_copy(update={
        "domain": {"pause": {"status": "unknown_outcome"}, "effective": {"mode": "unknown"}},
        "caption": "原暂停结果未确认完成。",
    })
    router = ResponseRouter({ctx.room_id: RoomEndpoint(ctx.room_id, "mac-a", "map-1", quiet=True)},
                            verify_endpoint=lambda *_: True)
    result = await router.deliver(receipt(original, ctx), ctx)
    assert result.domain["feedback"]["kind"] == "unknown"
    assert result.domain["feedback"]["requires_attention"] is True
    assert result.domain["report"] == original.domain and "未确认" in result.caption
    assert result.domain["output"]["status"] == "quiet" and not result.domain["output"]["attempted"]
    await router.close()
