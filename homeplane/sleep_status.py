"""Explicit finite text -> original runtime mode/pause, with truthful lease views."""
from __future__ import annotations

import math
from datetime import datetime
from uuid import uuid4

from pydantic import ValidationError

from .feedback_contracts import _Operations, digest
from .home_flow_contracts import FlowAbort, FlowChoice, FlowRequest, FlowResponse, TrustedFlowContext
from .home_flow_ports import ModePorts
from .rule_contracts import ModeLease

LIMITATIONS = [
    "仅覆盖本项目受治理动作和规则；external_rules_intercepted=False。",
    "core_independent=False；WAN 独立与真实房间效果未核验。",
    "仅显式文本模式，不推断真实睡眠；真实 ASR/AV unavailable。",
    "保护窗口不会自动续期，停止输出不逆转已经发生的动作。",
]
MODE_TEXT = {
    "我要睡觉": "sleep", "我要睡觉了": "sleep", "进入睡眠模式": "sleep",
    "进入手动模式": "manual", "切换手动模式": "manual",
}
PAUSE_TEXT = {"暂停自动操作", "暂停自动化", "暂停本系统自动操作"}
LONG_TEXT = {"我要睡觉一整晚", "我要睡觉整晚", "保护一整晚", "我要睡觉到明天"}


class SleepStatus:
    def __init__(self, ports: ModePorts, *, operation_limit=64):
        self.ports = ports
        self._operations = _Operations(operation_limit)
        self._origin = None  # Provenance/earliest observed expiry only; no shadow mode lease.
        self._window_choices = {}

    def invalidate(self, session_id):
        self._operations.invalidate(session_id)
        self._window_choices.pop(session_id, None)

    @staticmethod
    def _binding(context):
        return (context.source_revision, context.authority_revision, context.boot_id, context.generation)

    def _response(self, request, context, phase, code, caption, *, domain=None, choices=None, context_token=None):
        try:
            return FlowResponse(
                session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
                phase=phase, code=code, input_kind=context.input_kind, caption=caption,
                choices=choices or [], domain=domain, limitations=LIMITATIONS,
                context_token=context_token,
            )
        except ValidationError:
            return FlowResponse(
                session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
                phase="refused", code="flow_response_budget", input_kind=context.input_kind,
                caption="原领域回应超过显示预算，未截断状态证据；原操作可能已经生效，请经原记录查看，不重复执行。",
                limitations=LIMITATIONS,
            )

    def _check(self, request, context, operation="mode"):
        context.check()
        if request.session_id != context.session_id:
            raise FlowAbort("flow_session_mismatch")
        if self.ports.backend == "unavailable" or self.ports.permitted is None:
            raise FlowAbort("mode_backend_unavailable")
        if self.ports.permitted(context, operation) is not True:
            raise FlowAbort("mode_permission_required")
        context.check()

    def _read(self, context):
        if self.ports.status is None:
            raise FlowAbort("mode_status_unavailable")
        context.check()
        raw = self.ports.status(context)
        context.check()
        if type(raw) is not dict:
            raise FlowAbort("mode_status_unavailable")
        return raw

    def _effective(self, raw, context):
        result = {
            "mode": "unknown", "status": "unknown", "remaining_seconds": 0.0,
            "valid_until": None, "backend": self.ports.backend,
            "coverage": {"governed_only": True, "external_rules_intercepted": False,
                         "core_independent": False, "wan_independent": "unverified"},
        }
        if raw.get("available") is not True:
            result["status"] = "unavailable"
            return result
        if raw.get("mode_revision_valid") is False or not self._origin:
            return result
        origin_binding, original_modes, observed_deadline = self._origin
        if origin_binding != self._binding(context) or original_modes != digest(raw.get("modes", {})):
            return result
        try:
            leases = {k: ModeLease.model_validate(raw["modes"][k]) for k in ("home.sleep", "home.manual")}
            now = context.wall_now()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return result
            if any(now < lease.issued_at for lease in leases.values()):
                return result
            until = min(lease.valid_until for lease in leases.values())
            remaining = min((until - now).total_seconds(), observed_deadline - context.monotonic())
            result["valid_until"] = until.isoformat()
            if remaining <= 0:
                result["status"] = "expired"
                return result
            sleep, manual = leases["home.sleep"].value, leases["home.manual"].value
            if sleep and manual:
                return result
            result.update(mode="sleep" if sleep else "manual" if manual else "awake",
                          status="active", remaining_seconds=round(remaining, 3))
            return result
        except (ValueError, KeyError, TypeError):
            return result

    def _capture_origin(self, raw, context):
        try:
            leases = [ModeLease.model_validate(raw["modes"][k]) for k in ("home.sleep", "home.manual")]
            residual = min((lease.valid_until - context.wall_now()).total_seconds() for lease in leases)
            self._origin = (self._binding(context), digest(raw["modes"]), context.monotonic() + max(0, residual))
        except (ValueError, KeyError, TypeError):
            self._origin = None

    def _view(self, request, context, raw, pause=None, *, code="mode_status"):
        effective = self._effective(raw, context)
        if effective["status"] == "active":
            label = {"sleep": "显式睡眠", "manual": "手动", "awake": "清醒"}[effective["mode"]]
            caption = f"{'模拟：' if raw.get('simulated') is True else ''}{label}窗口剩余 {math.ceil(effective['remaining_seconds'])} 秒；仅覆盖受治理范围。"
        elif effective["status"] == "expired":
            caption = "原模式窗口已过期；当前模式未知，剩余 0 秒。"
        else:
            caption = "当前模式未知或不可用；不会自动续期或把关灯推断成睡眠。"
        if pause is not None:
            caption = ("原暂停回执已显示；受治理 pending 已进入原失效路径，外部规则不受拦截。 "
                       if pause.get("status") == "paused" else "原暂停结果未确认完成；不会宣称 pending 已取消。 ") + caption
        return self._response(request, context, "unavailable" if effective["status"] == "unavailable" else "result",
                              code, caption, domain={"runtime": raw, "pause": pause, "effective": effective})

    async def status(self, request: FlowRequest, context: TrustedFlowContext):
        try:
            self._check(request, context)
            return self._view(request, context, self._read(context))
        except FlowAbort as exc:
            return self._abort(request, context, str(exc))
        except Exception:
            return self._abort(request, context, "mode_status_unavailable")

    def _abort(self, request, context, code):
        phase = "refused" if code == "mode_permission_required" else "unavailable" if code.startswith("mode_") else "cancelled"
        return self._response(request, context, phase, code, "本次请求已停止或不可用；不会重新派发、续期或撤销已发生的物理动作。")

    async def change(self, request: FlowRequest, context: TrustedFlowContext):
        if request.kind == "cancel":
            return await self.cancel(request, context)
        if request.kind == "status":
            return await self.status(request, context)
        selection_deadline = None
        if request.candidate_id is not None:
            choice = self._window_choices.get(request.session_id)
            try:
                context.check()
                if (request.kind != "turn" or request.session_id != context.session_id or not choice
                    or choice["binding"] != context.focus_key or context.monotonic() >= choice["deadline"]
                    or request.candidate_id != choice["id"] or request.expected_context != choice["token"]
                    or request.text not in {"", choice["text"]}
                    or request.ttl_seconds not in {None, choice["seconds"]}):
                    raise FlowAbort("mode_window_choice_stale")
                selection_deadline = choice["deadline"]
                self._window_choices.pop(request.session_id, None)
                request = request.model_copy(update={"text": "进入睡眠模式" if choice["mode"] == "sleep" else "进入手动模式",
                                                     "ttl_seconds": choice["seconds"]})
            except FlowAbort as exc:
                return self._abort(request, context, str(exc))
        text = request.text.strip().rstrip("。！？!?")
        if request.kind != "turn" or request.lane not in {"auto", "mode"}:
            return self._response(request, context, "clarify", "mode_intent_unsupported", "请明确选择睡眠模式、手动模式或暂停受治理自动操作。")
        mode, pause_intent = MODE_TEXT.get(text), text in PAUSE_TEXT
        if mode is None and not pause_intent and text not in LONG_TEXT:
            return self._response(request, context, "clarify", "mode_intent_unsupported", "需要一个明确的单一模式或暂停请求；关闭事件不表示睡眠。")
        entry = None
        try:
            self._check(request, context, "pause" if pause_intent else "mode")
            if text in LONG_TEXT or (
                self.ports.fixed_ttl_seconds is not None and request.ttl_seconds is not None
                and request.ttl_seconds != self.ports.fixed_ttl_seconds
            ):
                seconds = self.ports.fixed_ttl_seconds or 1800
                self._window_choices = {k: v for k, v in self._window_choices.items() if v["deadline"] > context.monotonic()}
                if len(self._window_choices) >= 32 and request.session_id not in self._window_choices:
                    raise FlowAbort("mode_choice_capacity")
                choice_id = "mode-window:" + uuid4().hex
                token = digest({"binding": context.focus_key, "text": text, "seconds": seconds, "id": choice_id})
                selected_mode = mode or "sleep"
                self._window_choices[request.session_id] = {
                    "binding": context.focus_key, "deadline": context.deadline_mono, "id": choice_id,
                    "token": token, "mode": selected_mode, "seconds": seconds, "text": request.text,
                }
                return self._response(request, context, "clarify", "mode_window_choice_required",
                                      f"当前后端只支持有限窗口（最长 {seconds} 秒）；请明确选择，不承诺整夜保护。",
                                      choices=[FlowChoice(id=choice_id, label=f"{'睡眠' if selected_mode == 'sleep' else '手动'}窗口 {seconds} 秒")],
                                      context_token=token)
            entry, duplicate = self._operations.begin(
                (request.session_id, request.idempotency_key), context,
                digest({"text": text, "ttl": request.ttl_seconds, "pause": pause_intent}),
            )
            if selection_deadline is not None:
                entry.deadline = min(entry.deadline, selection_deadline)
            if duplicate:
                if entry.task and not entry.task.done():
                    return self._response(request, context, "unavailable", "mode_operation_pending", "原请求尚未结束；不会重复派发。")
                if entry.response and entry.response.phase in {"refused", "unavailable", "cancelled"}:
                    return entry.response.model_copy(update={"turn_id": request.turn_id, "operation_id": request.operation_id})
                pause = entry.response.domain.get("pause") if entry.response and entry.response.domain else None
                return self._view(request, context, self._read(context), pause, code="mode_duplicate_no_renewal")
            if pause_intent:
                if self.ports.pause_and_invalidate is None:
                    raise FlowAbort("mode_pause_unavailable")
                pause = await self._operations.run(entry, context, lambda: self.ports.pause_and_invalidate(context))
                if type(pause) is not dict:
                    raise FlowAbort("mode_pause_unavailable")
                response = self._view(request, context, self._read(context), pause,
                                      code="actions_paused" if pause.get("status") == "paused" else "actions_pause_result")
            else:
                if self.ports.set_mode is None:
                    raise FlowAbort("mode_change_unavailable")
                raw = await self._operations.run(entry, context, lambda: self.ports.set_mode(
                    mode, ttl_seconds=request.ttl_seconds, context=context,
                ))
                if type(raw) is not dict:
                    raise FlowAbort("mode_status_unavailable")
                self._capture_origin(raw, context)
                response = self._view(request, context, raw, code="explicit_mode_result")
            entry.response = response
            return response
        except FlowAbort as exc:
            response = self._abort(request, context, str(exc))
        except ValueError as exc:
            known = {"invalid_mode_window", "runtime_unavailable", "unsupported_simulated_mode",
                     "simulation_only_or_owner_unavailable", "core_runtime_unavailable",
                     "core_rule_source_or_authority_unavailable", "source_conflict"}
            code = str(exc) if str(exc) in known else "mode_backend_failed"
            response = self._response(request, context, "refused", code, "原领域后端拒绝请求；保护未获得新的有效资格。")
        except Exception:
            response = self._response(request, context, "unavailable", "mode_backend_failed", "模式或暂停后端失败；请查看原记录，不能自动重试。")
        if entry is not None:
            entry.response = response
        return response

    async def cancel(self, request: FlowRequest, context: TrustedFlowContext):
        if request.session_id != context.session_id:
            return self._abort(request, context, "flow_session_mismatch")
        self.invalidate(request.session_id)
        await self._operations.wait_session(request.session_id)
        return self._response(request, context, "cancelled", "flow_cancelled", "本会话请求已停止；原租约与已发生动作不会被伪称撤销。")

    async def close(self):
        return await self._operations.close()
