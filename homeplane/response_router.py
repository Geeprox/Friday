"""Verified current reports -> explicit room endpoint or visible local captions."""
from __future__ import annotations

from pydantic import ValidationError

from .feedback_contracts import (
    PROMPTS,
    LocalOutputReport,
    RoomEndpoint,
    VerifiedFeedback,
    _Operations,
    digest,
)
from .home_flow_contracts import FlowAbort, FlowRequest, FlowResponse, TrustedFlowContext

OUTPUT_LIMITATION = "输出 configured/attempted/completed 分开；退出成功不证明房间可听见，回声/语音停止未资格化。"


class ResponseRouter:
    def __init__(self, endpoints=None, outputs=None, *, verify_endpoint=None, operation_limit=64):
        self.endpoints = dict(endpoints or {})
        self.outputs = dict(outputs or {})
        self.verify_endpoint = verify_endpoint
        if any(not isinstance(e, RoomEndpoint) or room != e.room_id for room, e in self.endpoints.items()):
            raise ValueError("explicit_room_endpoint_required")
        if len({e.endpoint_id for e in self.endpoints.values()}) != len(self.endpoints):
            raise ValueError("cross_room_endpoint_reuse_rejected")
        if len({id(output) for output in self.outputs.values()}) != len(self.outputs):
            raise ValueError("cross_endpoint_output_reuse_rejected")
        self._operations = _Operations(operation_limit)
        self._active = {}

    def invalidate(self, session_id):
        self._operations.invalidate(session_id)

    @staticmethod
    def _policy(report):
        domain = report.domain or {}
        if report.code == "input_ack" and report.domain is None:
            return "ack", 0
        if report.phase in {"clarify", "select", "preview"}:
            return "clarify", 1
        if report.phase in {"refused", "unavailable"}:
            return "failure", 2
        if report.phase == "cancelled":
            return "ack", 0
        if domain.get("pause") is not None:
            pause = domain["pause"]
            return ("paused", 1) if type(pause) is dict and pause.get("status") == "paused" else ("unknown", 2)
        if type(domain.get("effective")) is dict:
            return ("mode", 1) if domain["effective"].get("status") == "active" else ("unknown", 2)
        status = domain.get("status")
        if status in {"failed", "rejected", "expired"}:
            return "failure", 2
        if status in {"unknown_outcome", "needs_reconcile", "sent", "accepted"}:
            return "unknown", 2
        if status == "planned":
            return "clarify", 1
        return "result", 1

    def _endpoint_current(self, endpoint, context):
        context.check()
        if (context.room_id != endpoint.room_id or context.map_revision != endpoint.mapping_revision
            or self.endpoints.get(endpoint.room_id) != endpoint or self.verify_endpoint is None):
            return False
        try:
            result = self.verify_endpoint(endpoint, context) is True
        except Exception:
            result = False
        context.check()
        return result

    @staticmethod
    def _abort(context, report, code):
        owned = isinstance(report, FlowResponse) and report.session_id == context.session_id
        return FlowResponse(
            session_id=context.session_id, turn_id=report.turn_id if owned else "feedback",
            operation_id=report.operation_id if owned else "feedback", phase="cancelled", code=code,
            input_kind=context.input_kind, caption="本次反馈已停止或未取得当前结果资格；不会恢复旧输出或撤销已派发动作。",
            limitations=[OUTPUT_LIMITATION],
        )

    @staticmethod
    def _response(report, feedback, output):
        degrade = {
            "unavailable": "该房间音频输出不可用；字幕仍可见。",
            "quiet": "安静/优先级策略：本次仅显示字幕。",
            "failed": "房间输出失败；请查看字幕。",
            "timeout": "房间输出超时，已进入本地清理；请查看字幕。",
            "cancelled": "本次输出已停止。",
            "duplicate": "重复反馈未再次播放。",
        }.get(output.status, "本地输出调用完成，可听见状态仍未核验。")
        caption = report.caption + (" " if report.caption else "") + degrade
        if len(caption) > 2048:
            caption = report.caption  # Keep the original caption; degradation stays in feedback/output.
        limits = list(report.limitations)
        if OUTPUT_LIMITATION not in limits and len(limits) < 16:
            limits.append(OUTPUT_LIMITATION)
        body = report.model_dump(mode="json")
        body.update(
            caption=caption, limitations=limits, output_audibility=output.audibility,
            domain={"report": report.domain, "feedback": {**feedback, "caption": degrade}, "output": output.json()},
        )
        return FlowResponse.model_validate(body)

    async def deliver(self, verified_feedback: VerifiedFeedback, context: TrustedFlowContext):
        report = verified_feedback.report if isinstance(verified_feedback, VerifiedFeedback) else None
        try:
            if not isinstance(verified_feedback, VerifiedFeedback):
                raise FlowAbort("feedback_receipt_required")
            verified_feedback.check(context)
            key = (report.session_id, report.operation_id, report.turn_id)
            entry, duplicate = self._operations.begin(key, context, digest(report.model_dump(mode="json")))
            prompt, priority = self._policy(report)
            feedback = {"kind": prompt, "room_id": verified_feedback.room_id, "priority": priority,
                        "prompt_id": prompt, "caption": PROMPTS[prompt], "requires_attention": priority >= 2}
            if duplicate:
                if entry.task and not entry.task.done():
                    output = LocalOutputReport("unavailable", "feedback_operation_pending")
                else:
                    previous = entry.response.domain["output"] if entry.response and entry.response.domain else {}
                    output = LocalOutputReport("duplicate", "output_duplicate_no_replay",
                                               previous.get("endpoint_id"), previous.get("configured", False),
                                               simulated=previous.get("simulated", False),
                                               audibility=previous.get("audibility", "unavailable"))
                return self._response(report, feedback, output)
            endpoint = self.endpoints.get(verified_feedback.room_id)
            output = None
            if report.phase == "cancelled":
                output = LocalOutputReport("cancelled", "output_cancelled")
            elif endpoint is None or not self._endpoint_current(endpoint, context):
                output = LocalOutputReport("unavailable", "room_output_unavailable")
            elif endpoint.quiet or endpoint.volume == 0 or priority < endpoint.minimum_priority:
                output = LocalOutputReport("quiet", "room_output_quiet", endpoint.endpoint_id,
                                           configured=endpoint.endpoint_id in self.outputs,
                                           audibility="unverified" if endpoint.endpoint_id in self.outputs else "unavailable")
            elif endpoint.endpoint_id not in self.outputs:
                output = LocalOutputReport("unavailable", "room_output_unavailable", endpoint.endpoint_id)
            if output is not None:
                response = self._response(report, feedback, output)
                entry.response = response
                return response
            adapter = self.outputs[endpoint.endpoint_id]
            active = self._active.get(endpoint.endpoint_id)
            if active:
                if priority <= active["priority"]:
                    response = self._response(report, feedback, LocalOutputReport(
                        "unavailable", "room_output_busy", endpoint.endpoint_id, configured=True,
                        simulated=adapter.simulated, audibility="unverified",
                    ))
                    entry.response = response
                    return response
                active["entry"].cancelled = True
                if active["entry"].task:
                    active["entry"].task.cancel()
                await adapter.cancel(active["operation_id"])
                verified_feedback.check(context)
            active = {"session_id": report.session_id, "entry": entry, "priority": priority,
                      "operation_id": report.operation_id}
            self._active[endpoint.endpoint_id] = active
            deadline = context.deadline_mono
            if report.expires_at is not None:
                deadline = min(deadline, context.monotonic() + (report.expires_at - context.wall_now()).total_seconds())

            def current():
                self._operations.check(entry, context)
                verified_feedback.check(context)
                return self._endpoint_current(endpoint, context)

            try:
                output = await self._operations.run(entry, context, lambda: adapter.play(
                    operation_id=report.operation_id, room_id=endpoint.room_id, endpoint_id=endpoint.endpoint_id,
                    prompt_id=prompt, volume=endpoint.volume, deadline_mono=deadline, is_current=current,
                ))
                verified_feedback.check(context)
                if not self._endpoint_current(endpoint, context):
                    raise FlowAbort("feedback_mapping_changed")
                response = self._response(report, feedback, output)
                entry.response = response
                return response
            finally:
                if self._active.get(endpoint.endpoint_id) is active:
                    self._active.pop(endpoint.endpoint_id, None)
        except FlowAbort as exc:
            return self._abort(context, report, str(exc))
        except ValidationError:
            result = self._abort(context, report, "flow_response_budget")
            return result.model_copy(update={"phase": "refused", "caption": "反馈回应超过显示预算，未截断原结果；请经原记录查看，不重复执行原操作。"})
        except Exception:
            return self._abort(context, report, "feedback_backend_failed")

    async def cancel(self, request: FlowRequest, context: TrustedFlowContext):
        if request.session_id != context.session_id:
            return self._abort(context, None, "flow_session_mismatch")
        self.invalidate(request.session_id)
        for endpoint_id, active in tuple(self._active.items()):
            if active["session_id"] == request.session_id:
                await self.outputs[endpoint_id].cancel(active["operation_id"])
        await self._operations.wait_session(request.session_id)
        return FlowResponse(
            session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
            phase="cancelled", code="flow_cancelled", input_kind=context.input_kind,
            caption="本会话反馈已停止；已派发设备动作的结果仍由原领域记录和对账决定。",
            limitations=[OUTPUT_LIMITATION],
        )

    async def close(self):
        await self._operations.close()
        return [await output.close() for output in self.outputs.values()]
