"""Bounded Console conversation over the existing Simulator rule transaction.

No rule writer, identity, model, persistent conversation or native adapter lives
here. App owners must invalidate synchronously before changing a private binding.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from uuid import uuid4

from .home_flow_contracts import (
    MAX_ACTIVE_OPERATIONS,
    MAX_HISTORY_BYTES,
    MAX_SESSIONS,
    MAX_TURNS,
    SESSION_TTL_SECONDS,
    FlowAbort,
    FlowChoice,
    FlowRequest,
    FlowResponse,
    TrustedFlowContext,
)
from .home_flow_ports import RulePorts
from .rule_compiler import digest
from .rule_deployment import RuleApproval, RuleDeployment
from .rule_text import RuleTextChoice, RuleTextRequest, RuleTextResult


@dataclass
class _Operation:
    text: str
    deadline: float
    expires_at: datetime
    context_token: str
    choices: dict[str, RuleTextChoice] = field(default_factory=dict)
    text_result: RuleTextResult | None = None
    plan: RuleDeployment | None = None
    response: FlowResponse | None = None
    task: asyncio.Task | None = None
    live: bool = True
    consumed: bool = False
    reason: str = "flow_cancelled"


@dataclass
class _Session:
    key: tuple
    deadline: float
    expires_at: datetime
    operations: dict[str, _Operation] = field(default_factory=dict)
    receipts: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    turns: dict[str, str] = field(default_factory=dict)
    text_bytes: int = 0
    live: bool = True


class RuleConversation:
    def __init__(self, ports: RulePorts | None, *, poll_seconds: float = 0.02):
        if ports is not None and ports.text.manager is not ports.manager:
            raise ValueError("rule_ports_instance_mismatch")
        if not 0.001 <= poll_seconds <= 0.05:
            raise ValueError("rule_poll_budget")
        self.ports = ports
        self.poll_seconds = poll_seconds
        self.sessions: dict[str, _Session] = {}
        self.retired: set[str] = set()
        self._tasks: dict[asyncio.Task, str] = {}
        self.closed = False

    @property
    def pending_count(self):
        return sum(not task.done() for task in self._tasks)

    def invalidate(self, session_id: str) -> None:
        """Fence first, then cancel; F5 calls this before changing its binding."""
        session = self.sessions.get(session_id)
        if session:
            session.live = False
            for operation in session.operations.values():
                self._invalidate_operation(operation, "flow_context_changed")

    @staticmethod
    def _invalidate_operation(operation, reason):
        operation.live = False
        operation.reason = reason
        if operation.task is not None and not operation.task.done():
            operation.task.cancel()

    async def close(self):
        self.closed = True
        for identifier in self.sessions:
            self.invalidate(identifier)
        tasks = set(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1)
            if pending:
                raise FlowAbort("rule_cleanup_pending")
        self.sessions.clear()

    def _scope(self, context):
        # The original rule path exposes complete RuleIR, rather than a scoped
        # projection. A partial home reader cannot safely consume that diff.
        if context.input_kind not in {"console_text", "simulated_fixture"}:
            raise FlowAbort("rule_write_authority_unavailable")
        scope = context.read_scope()
        if not (
            scope.boundary == "loopback_console"
            and scope.subject == "console"
            and scope.all_home is True
        ):
            raise FlowAbort("rule_source_scope_unavailable")

    def _check(self, context):
        if self.closed or self.ports is None:
            raise FlowAbort("rule_ports_unavailable")
        context.check()
        self._scope(context)

    def _enter(self, request, context, *, create=False):
        self._check(context)
        if request.session_id != context.session_id:
            raise FlowAbort("flow_session_mismatch")
        if request.lane not in {"auto", "rules"}:
            raise FlowAbort("rule_lane_mismatch")
        session = self.sessions.get(request.session_id)
        if session is None:
            if not create or request.session_id in self.retired:
                raise FlowAbort("rule_session_unavailable")
            if len(self.sessions) >= MAX_SESSIONS:
                # Eviction is bounded and tombstoned: a supplied new deadline
                # cannot revive the same expired session and its old approval.
                for identifier, old in tuple(self.sessions.items()):
                    if (not old.live or context.monotonic() >= old.deadline) and not any(
                        owner == identifier and not task.done() for task, owner in self._tasks.items()
                    ):
                        if len(self.retired) >= MAX_SESSIONS * MAX_TURNS:
                            raise FlowAbort("rule_session_capacity")
                        self.retired.add(identifier)
                        self.sessions.pop(identifier)
                if len(self.sessions) >= MAX_SESSIONS:
                    raise FlowAbort("rule_session_capacity")
            now = context.monotonic()
            deadline = min(context.deadline_mono, now + SESSION_TTL_SECONDS)
            session = _Session(
                context.focus_key, deadline, context.wall_now() + timedelta(seconds=deadline - now)
            )
            self.sessions[request.session_id] = session
        if session.key != context.focus_key:
            self.invalidate(request.session_id)
            raise FlowAbort("flow_context_changed")
        if not session.live:
            raise FlowAbort("flow_context_changed")
        if context.monotonic() >= session.deadline:
            self.invalidate(request.session_id)
            raise FlowAbort("flow_expired")
        fingerprint = digest(request)
        old = session.receipts.get(request.idempotency_key)
        if old and old != (fingerprint, request.turn_id, request.operation_id):
            raise FlowAbort("flow_idempotency_conflict")
        if request.turn_id in session.turns and session.turns[request.turn_id] != request.idempotency_key:
            raise FlowAbort("flow_turn_conflict")
        if not old:
            if len(session.turns) >= MAX_TURNS:
                raise FlowAbort("flow_turn_capacity")
            session.receipts[request.idempotency_key] = (fingerprint, request.turn_id, request.operation_id)
            session.turns[request.turn_id] = request.idempotency_key
        return session, bool(old)

    def _live(self, session, operation, context):
        self._check(context)
        if session.key != context.focus_key or not session.live:
            raise FlowAbort("flow_context_changed")
        if not operation.live:
            raise FlowAbort(operation.reason)
        if context.monotonic() >= min(session.deadline, operation.deadline):
            raise FlowAbort("flow_expired")

    def _finished_task(self, task):
        self._tasks.pop(task, None)
        if not task.cancelled():
            task.exception()  # consume diagnostics; never print private exception text

    async def _await(self, session, operation, context, call: Callable):
        self._live(session, operation, context)
        if operation.task is not None and not operation.task.done():
            raise FlowAbort("rule_operation_busy")
        if sum(owner == context.session_id and not task.done() for task, owner in self._tasks.items()) >= MAX_ACTIVE_OPERATIONS:
            raise FlowAbort("flow_operation_capacity")
        task = asyncio.create_task(call())
        operation.task = task
        self._tasks[task] = context.session_id
        task.add_done_callback(self._finished_task)
        try:
            while True:
                self._live(session, operation, context)
                done, _ = await asyncio.wait({task}, timeout=self.poll_seconds)
                self._live(session, operation, context)
                if done:
                    return task.result()
        except (FlowAbort, asyncio.CancelledError) as exc:
            reason = str(exc) if isinstance(exc, FlowAbort) else "flow_cancelled"
            self._invalidate_operation(operation, reason)
            task.cancel()
            # Non-cooperative late work remains tracked and cannot resume an
            # approval. The actual existing domain cooperates with cancellation.
            await asyncio.wait({task}, timeout=0.25)
            raise FlowAbort(reason) from None
        finally:
            if operation.task is task:
                operation.task = None

    def _response(self, request, context, phase, code, caption, *, operation=None, domain=None, choices=()):
        text_context = self.ports.text.context() if self.ports is not None else None
        expires_at = operation.expires_at if operation else None
        preview = phase == "preview" and operation and operation.plan
        fields = dict(
            session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
            phase=phase, code=code, input_kind=context.input_kind, caption=caption,
            context_token=text_context.fingerprint if text_context else None,
            choices=list(choices), domain=domain, expires_at=expires_at,
            plan_id=operation.plan.id if operation and operation.plan else None,
            approval_digest=preview.approval_digest if preview else None,
            limitations=text_context.coverage if text_context else ["规则领域接点未注入。"],
            native_graph="opaque" if text_context and not text_context.simulated else "unavailable",
        )
        try:
            response = FlowResponse(**fields)
        except ValueError:
            response = FlowResponse(
                session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
                phase="unavailable", code="flow_response_budget", input_kind=context.input_kind,
                caption="结果超过本次对话的展示预算，请在原规则记录中查看。",
            )
        if operation:
            operation.response = response
        return response

    def _domain(self, operation):
        if operation.plan is not None:
            plan = self.ports.manager.get(operation.plan.id)
            operation.plan = plan
            return plan.model_dump(mode="json")
        return None

    def _failure(self, request, context, code, operation=None):
        domain = None
        if operation:
            self._invalidate_operation(operation, code)
            try:
                self._check(context)
                domain = self._domain(operation)
            except (ValueError, KeyError):
                pass
        phase = "cancelled" if code == "flow_cancelled" else "unavailable" if (
            "unavailable" in code or "expired" in code or "capacity" in code
        ) else "refused"
        captions = {
            "flow_cancelled": "本次请求已停止。已进入部署的配置结果需要读取确认。",
            "flow_expired": "本次会话或预览已过期，请重新预览。",
            "flow_context_changed": "来源或权限已变化，请在当前来源重新预览。",
            "rule_write_authority_unavailable": "现场语音的规则修改权限接点尚未接入。",
            "rule_source_scope_unavailable": "当前读取范围不能展示完整规则差异。",
            "rule_ports_unavailable": "规则领域服务尚未接入。",
        }
        return self._response(request, context, phase, code, captions.get(code, "本次规则请求未获准，请重新选择或预览。"), operation=operation, domain=domain)

    async def turn(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        if request.kind == "approve":
            return await self.approve(request, context)
        if request.kind == "cancel":
            return await self.cancel(request, context)
        if request.kind in {"status", "reconcile", "rollback"}:
            return await self._inspect(request, context)
        operation = None
        try:
            if request.kind != "turn" or any(v is not None for v in (
                request.plan_id, request.evidence_ref, request.start, request.end, request.ttl_seconds,
                request.room_id, request.device_id, request.camera_id,
            )):
                raise FlowAbort("rule_request_unsupported")
            session, replay = self._enter(request, context, create=bool(request.text and not request.candidate_id))
            operation = session.operations.get(request.operation_id)
            if operation is not None and operation.task is not None and not operation.task.done():
                return self._response(request, context, "unavailable", "rule_operation_busy",
                                      "原请求仍在处理中，请等待或停止；没有再次调用规则领域。")
            if operation is not None and replay and operation.response and operation.task is None:
                self._live(session, operation, context)
                if not operation.consumed and self.ports.text.context().fingerprint != operation.context_token:
                    raise FlowAbort("rule_context_changed")
                if operation.plan is not None:
                    operation.plan = self.ports.manager.get(operation.plan.id)
                    self._cap_plan(operation, context)
                    self._live(session, operation, context)
                return operation.response.model_copy(update={"turn_id": request.turn_id})
            if operation is None:
                if not request.text or request.candidate_id:
                    raise FlowAbort("rule_original_text_required")
                if session.text_bytes + len(request.text.encode()) > MAX_HISTORY_BYTES:
                    raise FlowAbort("flow_history_budget")
                operation = _Operation(request.text, session.deadline, session.expires_at, self.ports.text.context().fingerprint)
                session.text_bytes += len(request.text.encode())
                session.operations[request.operation_id] = operation
            self._live(session, operation, context)
            if operation.consumed or operation.plan is not None:
                raise FlowAbort("rule_new_preview_required")
            if request.text and request.text != operation.text:
                raise FlowAbort("rule_original_text_changed")
            if self.ports.text.context().fingerprint != operation.context_token or (
                request.expected_context is not None and request.expected_context != operation.context_token
            ):
                raise FlowAbort("rule_context_changed")
            selected = None
            if request.candidate_id:
                selected = operation.choices.get(request.candidate_id)
                if selected is None:
                    raise FlowAbort("rule_candidate_mismatch")
                if request.expected_context != operation.context_token:
                    raise FlowAbort("rule_context_changed")
            text_request = RuleTextRequest(
                text=operation.text, expected_context=operation.context_token,
                idempotency_key="flow-text:" + digest([request.session_id, request.operation_id, request.idempotency_key]),
                expires_at=operation.expires_at,
                **(dict(record_id=selected.record_id, action_id=selected.action_id,
                        expected_fingerprint=selected.expected_fingerprint) if selected else {}),
            )
            result = await self._await(session, operation, context, lambda: self.ports.text.plan(text_request))
            operation.text_result = result
            if result.status == "clarification":
                operation.choices = {"rule-choice:" + uuid4().hex: choice for choice in result.choices}
                choices = [FlowChoice(id=identifier, label=(choice.rule_label + " · " + choice.action_label)[:256])
                           for identifier, choice in operation.choices.items()]
                return self._response(request, context, "select", result.code, "选择要修改的规则和空调动作；其他动作将保留。",
                                      operation=operation, domain=result.model_dump(mode="json"), choices=choices)
            if result.plan is None:
                return self._response(request, context, "unavailable" if result.status in {"unsupported", "expired"} else "refused",
                                      result.code, "当前来源或操作不能修改，请查看来源与覆盖范围。",
                                      operation=operation, domain=result.model_dump(mode="json"))
            operation.plan = result.plan
            self._cap_plan(operation, context)
            trial = await self._await(session, operation, context, lambda: self.ports.manager.simulate(result.plan.id))
            operation.plan = trial
            self._cap_plan(operation, context)
            # Reuse the original result schema and the current original domain
            # plan projection. No status or truth field is rewritten.
            domain = result.model_copy(update={"plan": trial}).model_dump(mode="json")
            return self._response(request, context, "preview" if trial.status == "awaiting_approval" else "result",
                                  trial.code, "请核对仅移除的动作、保留内容与覆盖范围，再批准这次变更。",
                                  operation=operation, domain=domain)
        except FlowAbort as exc:
            return self._failure(request, context, str(exc), operation)
        except (ValueError, OSError):
            return self._failure(request, context, "rule_domain_unavailable", operation)

    @staticmethod
    def _cap_plan(operation, context):
        remaining = (operation.plan.expires_at - context.wall_now()).total_seconds()
        operation.deadline = min(operation.deadline, context.monotonic() + max(0, remaining))
        operation.expires_at = min(operation.expires_at, operation.plan.expires_at)

    async def approve(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        operation = None
        try:
            if request.kind != "approve" or request.text or any(v is not None for v in (
                request.candidate_id, request.evidence_ref, request.start, request.end, request.ttl_seconds,
                request.room_id, request.device_id, request.camera_id,
            )):
                raise FlowAbort("rule_exact_approval_required")
            session, _ = self._enter(request, context)
            operation = session.operations.get(request.operation_id)
            if operation is None or operation.plan is None:
                raise FlowAbort("rule_plan_unavailable")
            if request.plan_id != operation.plan.id or request.approval_digest != operation.plan.approval_digest:
                raise FlowAbort("rule_approval_mismatch")
            if operation.consumed:
                return self._response(request, context, "result", "rule_approval_already_consumed",
                                      "本次批准已经处理，显示原部署记录。", operation=operation,
                                      domain=self._domain(operation))
            self._live(session, operation, context)
            if request.expected_context != operation.context_token or self.ports.text.context().fingerprint != operation.context_token:
                raise FlowAbort("rule_context_changed")
            plan = self.ports.manager.get(operation.plan.id)
            if plan.boot_id != self.ports.manager.boot_id or plan.status != "awaiting_approval":
                raise FlowAbort("rule_new_preview_required")
            self._cap_plan(operation, context)
            self._live(session, operation, context)
            operation.consumed = True  # even an interrupted apply is never replayed
            result = await self._await(session, operation, context, lambda: self.ports.manager.apply(
                RuleApproval(plan_id=plan.id, approval_digest=plan.approval_digest)
            ))
            operation.plan = result
            return self._response(request, context, "result", result.code, self._result_caption(result),
                                  operation=operation, domain=result.model_dump(mode="json"))
        except FlowAbort as exc:
            return self._failure(request, context, str(exc), operation)
        except (ValueError, OSError):
            return self._failure(request, context, "rule_domain_unavailable", operation)

    @staticmethod
    def _result_caption(result):
        if result.status == "verified":
            return "自有模拟规则配置已读取验证。其他动作保留；没有真实 Graph 写入或物理动作确认。"
        if result.status == "rolled_back":
            return "原模拟规则配置已恢复。配置回滚不撤销过去的物理动作。"
        if result.status == "needs_reconcile":
            return "部署结果需要读取对账，请勿重复批准或重发。"
        return "原规则领域返回 " + result.status + "，请查看具体结果与覆盖范围。"

    async def _inspect(self, request, context):
        operation = None
        try:
            if request.text or any(v is not None for v in (
                request.candidate_id, request.evidence_ref, request.start, request.end, request.ttl_seconds,
                request.room_id, request.device_id, request.camera_id,
            )):
                raise FlowAbort("rule_request_unsupported")
            session, _ = self._enter(request, context)
            operation = session.operations.get(request.operation_id)
            if not operation or not operation.plan or request.plan_id not in {None, operation.plan.id}:
                raise FlowAbort("rule_plan_unavailable")
            if operation.task is not None:
                raise FlowAbort("rule_operation_busy")
            if request.kind == "status":
                result = self.ports.manager.get(operation.plan.id)
            else:
                if not operation.consumed:
                    raise FlowAbort("rule_no_deployment_to_reconcile")
                # A cancelled approval may reconcile/rollback only its original
                # record. It cannot become an eligible apply operation again.
                call = self.ports.manager.reconcile if request.kind == "reconcile" else self.ports.manager.rollback
                self._check(context)
                if sum(owner == context.session_id and not task.done() for task, owner in self._tasks.items()) >= MAX_ACTIVE_OPERATIONS:
                    raise FlowAbort("flow_operation_capacity")
                task = asyncio.create_task(call(operation.plan.id))
                operation.task = task
                self._tasks[task] = context.session_id
                task.add_done_callback(self._finished_task)
                try:
                    while not task.done():
                        self._check(context)
                        if not session.live or session.key != context.focus_key:
                            raise FlowAbort("flow_context_changed")
                        if context.monotonic() >= session.deadline:
                            raise FlowAbort("flow_expired")
                        await asyncio.wait({task}, timeout=self.poll_seconds)
                    self._check(context)
                    if not session.live or session.key != context.focus_key:
                        raise FlowAbort("flow_context_changed")
                    if context.monotonic() >= session.deadline:
                        raise FlowAbort("flow_expired")
                    result = task.result()
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.wait({task}, timeout=0.25)
                    operation.task = None
            self._check(context)
            operation.plan = result
            return self._response(request, context, "result", result.code, self._result_caption(result),
                                  operation=operation, domain=result.model_dump(mode="json"))
        except (FlowAbort, asyncio.CancelledError) as exc:
            return self._failure(request, context, str(exc) if isinstance(exc, FlowAbort) else "flow_cancelled", operation)
        except (ValueError, OSError):
            return self._failure(request, context, "rule_domain_unavailable", operation)

    async def cancel(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        # Stop remains available at the turn budget and after a private expiry;
        # expired/revoked contexts receive no old scoped domain data.
        session = self.sessions.get(request.session_id)
        if request.kind != "cancel" or request.session_id != context.session_id or (
            session is not None and session.key != context.focus_key
        ):
            return self._failure(request, context, "flow_session_mismatch")
        operation = session.operations.get(request.operation_id) if session else None
        if operation:
            task = operation.task
            self._invalidate_operation(operation, "flow_cancelled")
            if task is not None:
                await asyncio.wait({task}, timeout=0.25)
        return self._failure(request, context, "flow_cancelled", operation)
