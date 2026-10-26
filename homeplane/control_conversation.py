"""Caller-owned action tasks and bounded explicit control conversation.

All effects remain in the existing ActionController. Its qualified execute port
checks the private fence before the original send claim. The await wrapper checks
actual Task suspension/resumption; internal awaits can also complete inline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from friday.core import Evidence, parse_command

from .action_contracts import DevicePlanRequest, LightExecutionTrace, LightPlanRequest
from .attention_policy import attention_policy
from .interaction_contracts import AttentionAdmission, InteractionBinding
from .interaction_sessions import FocusFence, InteractionSessions

if TYPE_CHECKING:
    from .home_flow_contracts import FlowRequest, FlowResponse, TrustedFlowContext
    from .home_flow_ports import ActionPorts


class InteractionInvalidated(asyncio.CancelledError):
    pass


class FencedAwait:
    """Inject cancellation at actual Task suspension/resumption boundaries.

    Inline-completing internal awaits do not pass through this wrapper. Effect
    dispatch therefore requires the original controller's separate private guard.
    Cancellation after its sent claim remains owned by that controller.
    """

    def __init__(self, awaitable, current):
        self.awaitable, self.current = awaitable, current

    def __await__(self):
        iterator = self.awaitable.__await__()
        value, error, invalidated = None, None, False
        try:
            while True:
                if not invalidated and not self.current():
                    error, invalidated = InteractionInvalidated(), True
                try:
                    if error is not None:
                        yielded = iterator.throw(error)
                        error = None
                    else:
                        yielded = iterator.send(value)
                except StopIteration as finished:
                    if invalidated:
                        raise InteractionInvalidated()
                    return finished.value
                try:
                    value = yield yielded
                except BaseException as caught:
                    value, error = None, caught
        finally:
            iterator.close()


@dataclass
class ControlOperation:
    binding_key: tuple
    fence: FocusFence
    fingerprint: str
    expires_mono: float
    text: str
    candidates: tuple[str, ...] = ()
    trace: LightExecutionTrace | None = None
    digest: str | None = None
    approved: bool = False
    cancelled: bool = False
    task: asyncio.Task | None = None
    response: FlowResponse | None = None
    context_key: tuple = ()
    room: str | None = None
    room_choices: dict | None = None
    scope_key: tuple = ()
    expires_at: datetime | None = None
    read_binding: str | None = None


class ControlConversation:
    """Finite preview/approval flow over F0 ports and the original action domain."""

    def __init__(self, ports: ActionPorts | None = None, *, sessions=None, aliases=None, wake_required=False,
                 allow_simulated_admission=False, ambient_permission=None):
        self.ports = ports
        self.sessions = sessions or InteractionSessions(max_sessions=32)
        self.aliases = aliases
        self.wake_required = wake_required
        self.allow_simulated_admission = allow_simulated_admission
        self.ambient_permission = ambient_permission
        self._operations = OrderedDict()
        self._bindings = {}
        self._contexts = {}
        self._turns = {}
        self._session_deadlines = {}
        self._closed = False

    @staticmethod
    def _scope_key(scope):
        return (scope.subject, scope.revision, scope.boundary, scope.all_home,
                tuple(sorted(scope.rooms)), tuple(sorted(scope.entities)), scope.expires_at)

    @staticmethod
    def _abort_code(exc):
        from .home_flow_contracts import FlowAbort
        return str(exc) if isinstance(exc, FlowAbort) else "flow_cancelled"

    def _context_token(self, context):
        return hashlib.sha256(json.dumps(context.focus_key, ensure_ascii=False).encode()).hexdigest()

    def _reply(self, request, context, phase, code, caption="", *, operation=None, choices=(), domain=None):
        from .home_flow_contracts import FlowChoice, FlowResponse
        trace = operation.trace if operation else None
        return FlowResponse(
            session_id=request.session_id, turn_id=request.turn_id, operation_id=request.operation_id,
            phase=phase, code=code, caption=caption, input_kind=context.input_kind,
            expires_at=operation.expires_at if operation else context.wall_now() + timedelta(
                seconds=max(0, context.deadline_mono - context.monotonic())),
            context_token=self._context_token(context), choices=[FlowChoice(id=k, label=v) for k, v in choices],
            plan_id=trace.action_id if trace else None,
            approval_digest=operation.digest if operation and trace and phase == "preview"
            and not operation.approved and not operation.cancelled else None,
            domain=domain if domain is not None else trace.model_dump(mode="json") if trace else None,
            limitations=[
                "Console session does not identify a person or grant device authority.",
                "Reported state is not independent physical confirmation; external rules remain uncovered.",
                "ASR/visual producer, native Graph, room audibility and NAS lease are independently held.",
            ],
        )

    def _check(self, request, context, operation=None):
        from .home_flow_contracts import FlowAbort
        context.check()
        if self._closed:
            raise FlowAbort("flow_closed")
        if request.session_id != context.session_id:
            raise FlowAbort("flow_session_mismatch")
        fixed_deadline = self._session_deadlines.get(context.session_id)
        if fixed_deadline is not None and context.monotonic() >= fixed_deadline:
            raise FlowAbort("flow_expired")
        if request.expected_context is not None and request.expected_context != self._context_token(context):
            raise FlowAbort("flow_context_changed")
        if operation is not None and (
            operation.cancelled or operation.context_key != context.focus_key
            or operation.expires_mono <= context.monotonic()
            or not self.sessions.current(operation.fence)
        ):
            raise FlowAbort("flow_operation_invalidated")
        scope = context.read_scope()
        if operation is not None and operation.scope_key != self._scope_key(scope):
            raise FlowAbort("flow_read_scope_changed")
        return scope

    def _live(self, request, context, operation):
        try:
            self._check(request, context, operation)
            return True
        except ValueError:
            return False

    def _prune(self, context):
        now = context.monotonic()
        for key, op in list(self._operations.items()):
            if op.expires_mono <= now:
                self._invalidate_operation(op)
                self._operations.pop(key)
        session = context.session_id
        previous = self._contexts.get(session)
        if previous is not None and previous != context.focus_key:
            self.invalidate(session_id=session)
        if session not in self._contexts and len(self._contexts) >= 32:
            oldest = next(iter(self._contexts))
            self.invalidate(session_id=oldest)
            self._contexts.pop(oldest, None)
            self._turns.pop(oldest, None)
            self._session_deadlines.pop(oldest, None)
        self._contexts[session] = context.focus_key
        self._session_deadlines.setdefault(session, min(context.deadline_mono, context.monotonic() + 60))

    def _invalidate_operation(self, operation):
        operation.cancelled = True
        self.sessions.invalidate(slot=operation.fence.slot)
        if operation.task and not operation.task.done():
            operation.task.cancel()

    def invalidate(self, session_id=None):
        """Owner calls synchronously on stop/source/identity/map/authority changes."""
        for key, op in list(self._operations.items()):
            if session_id is None or key[0] == session_id:
                self._invalidate_operation(op)
                self._operations.pop(key)
        self.sessions.invalidate(session_id=session_id)
        for key in list(self._bindings):
            if session_id is None or key == session_id:
                self._bindings.pop(key, None)
        if session_id is None:
            self._contexts.clear()
            self._turns.clear()
            self._session_deadlines.clear()

    async def close(self):
        tasks = [op.task for op in self._operations.values() if op.task and not op.task.done()]
        self._closed = True
        self.invalidate()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _call(self, request, context, operation, awaitable):
        async def run():
            return await FencedAwait(awaitable, lambda: self._live(request, context, operation))
        operation.task = asyncio.create_task(run())
        try:
            result = await operation.task
            self._check(request, context, operation)
            return result
        finally:
            operation.task = None

    def _binding(self, context, room, scope):
        if context.input_kind == "console_text":
            return InteractionBinding(
                input_kind="console_text", session_id=context.session_id, room_id=room,
                camera_id=context.camera_id or "console", map_revision=hashlib.sha256(json.dumps(
                    [context.map_revision, context.camera_session_id, scope.revision]
                ).encode()).hexdigest(), source_revision=context.source_revision,
                authority_revision=context.authority_revision, boot_id=context.boot_id,
                generation=context.generation, deadline_mono=context.deadline_mono,
            )
        binding = context.interaction
        if not isinstance(binding, InteractionBinding) or (
            binding.session_id != context.session_id or binding.room_id != room
            or binding.camera_id != context.camera_id or binding.map_revision != context.map_revision
            or binding.subject_ref != context.subject_ref or binding.track_ref != context.track_ref
            or binding.track_epoch != context.track_epoch or binding.source_revision != context.source_revision
            or binding.authority_revision != context.authority_revision or binding.boot_id != context.boot_id
            or binding.generation != context.generation
        ):
            return None
        return binding

    @staticmethod
    def _allowed(entity, scope):
        return scope.all_home or entity.id in scope.entities or entity.room in scope.rooms

    async def plan(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        from .home_flow_contracts import FlowAbort
        operation = None
        try:
            scope = self._check(request, context)
            self._prune(context)
            if request.kind != "turn" or request.lane not in {"auto", "control"}:
                return self._reply(request, context, "refused", "unsupported_control_turn")
            if self.ports is None:
                return self._reply(request, context, "unavailable", "control_port_unavailable")
            key = (context.session_id, request.operation_id)
            operation = self._operations.get(key)
            if operation and operation.task and not operation.task.done():
                return self._reply(request, context, "refused", "control_operation_in_progress")
            if operation and operation.trace:
                self._check(request, context, operation)
                # Preview replay keeps the original expiry and typed request intact.
                if ((request.text and request.text != operation.text)
                        or (request.device_id or request.candidate_id) not in (None, operation.trace.request.target)
                        or request.room_id not in (None, "room:" + hashlib.sha256(operation.room.encode()).hexdigest())):
                    return self._reply(request, context, "refused", "flow_operation_conflict")
                return self._reply(request, context,
                                   "preview" if operation.trace.status == "planned" else "result",
                                   "control_preview" if operation.trace.status == "planned" else operation.trace.code,
                                   operation=operation)
            text = request.text or (operation.text if operation else "")
            action, name, wake = parse_command(text)
            parts = [part for part in re.split(r"[，,。;；]|然后|并且|同时|顺便|接着", text) if part.strip()]
            mixed = sum(bool(parse_command(part)[0]) for part in parts) > 1
            if not action or mixed or any(word in name for word in ("然后", "并且", "同时", "顺便", "接着")):
                return self._reply(request, context, "refused", "unsupported_command",
                                   "请使用完整、明确的设备命令。")
            if self.wake_required and not wake:
                return self._reply(request, context, "refused", "say_friday")
            turns = self._turns.setdefault(context.session_id, set())
            if request.turn_id not in turns and len(turns) >= 16:
                return self._reply(request, context, "refused", "flow_turn_budget")
            turns.add(request.turn_id)
            if sum(bool(op.task and not op.task.done()) for op in self._operations.values()) >= 2:
                return self._reply(request, context, "refused", "flow_operation_budget")
            # A temporary room fence scopes discovery; it is replaced only after server-side selection.
            room = context.room_id or (self._bindings.get(context.session_id).room_id
                                       if context.session_id in self._bindings else "unselected")
            binding = self._binding(context, room, scope)
            resolved = self.sessions.resolve(binding)
            if resolved.fence is None:
                return self._reply(request, context, "unavailable", "interaction_binding_unavailable")
            if context.input_kind != "console_text":
                admission = context.admission
                if (not isinstance(admission, AttentionAdmission) or not context.producer_ref
                        or admission.producer_ref != context.producer_ref
                        or (admission.source_kind == "fixture" and context.input_kind != "simulated_fixture")):
                    return self._reply(request, context, "unavailable", "ambient_admission_unavailable")
                evidence = Evidence(live_speaker=True, people_count=binding.speaker_count, attention=1)
                policy = attention_policy(
                    evidence=evidence, admission=admission if isinstance(admission, AttentionAdmission) else None,
                    binding=binding, explicit_command=True, wake=wake, wake_required=self.wake_required,
                    threshold=.85, max_age_seconds=12, allow_simulated=self.allow_simulated_admission,
                )
                if not policy.admitted:
                    return self._reply(request, context, "unavailable", policy.code)
            elif scope.boundary != "loopback_console":
                return self._reply(request, context, "refused", "console_boundary_required")
            if operation is None:
                if len(self._operations) >= 64:
                    return self._reply(request, context, "refused", "flow_operation_budget")
                history_bytes = sum(len(op.text.encode()) for k, op in self._operations.items()
                                    if k[0] == context.session_id)
                if history_bytes + len(text.encode()) > 16384:
                    return self._reply(request, context, "refused", "flow_history_budget")
                operation = ControlOperation(
                    binding.key, resolved.fence, hashlib.sha256(text.encode()).hexdigest(),
                    min(context.deadline_mono, context.monotonic() + 30), text,
                    context_key=context.focus_key, room=room, scope_key=self._scope_key(scope),
                    expires_at=context.wall_now() + timedelta(seconds=min(
                        context.deadline_mono - context.monotonic(), 30)),
                )
                self._operations[key] = operation
                if (name in {"", "灯", "这个", "这个灯", "那个", "那个灯", "它"}
                        and not request.device_id and not request.candidate_id and resolved.target_id
                        and resolved.expires_mono is not None):
                    operation.expires_mono = min(operation.expires_mono, resolved.expires_mono)
                    operation.expires_at = min(operation.expires_at, context.wall_now() + timedelta(
                        seconds=max(0, resolved.expires_mono - context.monotonic())))
            else:
                self._check(request, context, operation)
                if request.text and request.text != operation.text:
                    return self._reply(request, context, "refused", "flow_operation_conflict")
            snapshot = await self._call(request, context, operation, self.ports.snapshot(context))
            entities = [e for e in snapshot.entities if self._allowed(e, scope)]
            if context.room_id:
                entities = [e for e in entities if e.room == context.room_id]
            room_choices = {"room:" + hashlib.sha256(r.encode()).hexdigest(): r
                            for r in sorted({e.room for e in entities if e.room})}
            if request.room_id:
                selected_room = room_choices.get(request.room_id)
                if selected_room is None:
                    return self._reply(request, context, "refused", "room_selection_unavailable")
                room = selected_room
                entities = [e for e in entities if e.room == room]
            generic = name in {"", "灯", "这个", "这个灯", "那个", "那个灯", "它"}
            if generic:
                candidates = [e for e in entities if e.kind == "light"] if name == "灯" else entities
                if not request.device_id and not request.candidate_id and resolved.target_id:
                    candidates = [e for e in candidates if e.id == resolved.target_id and e.room == room]
                elif action in {"dim", "brighten"} and not request.device_id and not request.candidate_id:
                    candidates = []
            else:
                candidates = [e for e in entities if name in (e.name, *(self.aliases(e) if self.aliases else ()))]
            selected = request.device_id or request.candidate_id
            if request.device_id and request.candidate_id and request.device_id != request.candidate_id:
                return self._reply(request, context, "refused", "device_selection_conflict")
            if selected:
                if operation.candidates and selected not in operation.candidates:
                    return self._reply(request, context, "refused", "selection_not_current_candidate")
                candidates = [e for e in candidates if e.id == selected]
                if not candidates:
                    return self._reply(request, context, "refused", "device_selection_unavailable")
            if len(candidates) != 1:
                operation.candidates = tuple(e.id for e in candidates[:16])
                operation.room_choices = room_choices
                choices = [(e.id, (e.room + " · " if e.room else "") + e.name) for e in candidates[:16]]
                if not choices and generic:
                    choices = [(e.id, (e.room + " · " if e.room else "") + e.name)
                               for e in entities if e.kind == "light"][:16]
                    operation.candidates = tuple(k for k, _ in choices)
                return self._reply(request, context, "select" if choices else "clarify", "target_ambiguous",
                                   "请选择当前范围内的设备。", choices=choices, operation=operation)
            entity = candidates[0]
            if not entity.room:
                return self._reply(request, context, "unavailable", "device_room_unavailable")
            room = entity.room
            binding = self._binding(context, room, scope)
            resolved = self.sessions.resolve(binding)
            if resolved.fence is None:
                return self._reply(request, context, "unavailable", "interaction_binding_unavailable")
            operation.fence, operation.binding_key, operation.room = resolved.fence, binding.key, room
            self._bindings[context.session_id] = binding
            if not entity.state_version:
                return self._reply(request, context, "unavailable", "device_state_version_unavailable")
            desired = {"power": "off" if action == "off" else "on"}
            if action in {"dim", "brighten"}:
                if entity.kind != "light" or getattr(self.ports, "read_current", None) is None:
                    return self._reply(request, context, "unavailable", "brightness_state_unavailable")
                if resolved.target_id and not selected and generic and resolved.expires_mono is not None:
                    operation.expires_mono = min(operation.expires_mono, resolved.expires_mono)
                current = await self._call(request, context, operation,
                                          self.ports.read_current(entity.id, entity.state_version, context))
                try:
                    reported = current["reported"]
                    received = datetime.fromisoformat(current["received_at"])
                    age = (context.wall_now() - received).total_seconds()
                    brightness = reported["brightness"]
                    fingerprint = current["target_binding_fingerprint"]
                    valid = (received.tzinfo is not None and -2 <= age <= 12 and reported["power"] == "on"
                             and type(brightness) is int and 0 <= brightness <= 255
                             and current["physically_confirmed"] is False
                             and current["expected_state_version"] == entity.state_version
                             and isinstance(fingerprint, str) and len(fingerprint) == 64
                             and all(c in "0123456789abcdef" for c in fingerprint))
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    return self._reply(request, context, "clarify", "brightness_state_unavailable")
                operation.read_binding = fingerprint
                desired["brightness"] = max(1, min(255, brightness + (51 if action == "brighten" else -51)))
            if entity.kind not in {"light", "switch", "fan"}:
                return self._reply(request, context, "refused", "unsupported_device_command")
            seconds = operation.expires_mono - context.monotonic()
            if request.ttl_seconds is not None:
                seconds = min(seconds, request.ttl_seconds)
                operation.expires_mono = context.monotonic() + seconds
            expires = min(operation.expires_at, context.wall_now() + timedelta(seconds=seconds))
            operation.expires_at = expires
            idem = hashlib.sha256(json.dumps([context.focus_key, request.operation_id,
                                              request.idempotency_key], ensure_ascii=False).encode()).hexdigest()
            fields = dict(target=entity.id, desired=desired, actor="console",
                          expected_state_version=entity.state_version, expires_at=expires, idempotency_key=idem)
            typed = (LightPlanRequest(**fields) if entity.kind == "light" else
                     DevicePlanRequest(operation=entity.kind + ".set", **fields))
            trace = await self._call(request, context, operation, self.ports.controller.plan(typed))
            operation.trace = trace
            if operation.read_binding is not None and trace.target_binding_fingerprint != operation.read_binding:
                self._invalidate_operation(operation)
                return self._reply(request, context, "refused", "brightness_binding_changed", operation=operation)
            operation.digest = hashlib.sha256(json.dumps(
                [trace.model_dump(mode="json"), context.focus_key, request.operation_id,
                 operation.expires_at.isoformat()],
                sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            if trace.status == "planned":
                return self._reply(request, context, "preview", "control_preview", "请核对绝对目标值后批准。",
                                   operation=operation)
            self.sessions.invalidate(slot=operation.fence.slot)
            return self._reply(request, context, "result", trace.code, operation=operation)
        except (FlowAbort, InteractionInvalidated, asyncio.CancelledError) as exc:
            if operation:
                self._invalidate_operation(operation)
            return self._reply(request, context, "cancelled", self._abort_code(exc))
        except (ValueError, OSError, TimeoutError):
            if operation:
                self._invalidate_operation(operation)
            return self._reply(request, context, "unavailable", "control_domain_unavailable")

    async def approve(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        from .home_flow_contracts import FlowAbort
        operation = self._operations.get((context.session_id, request.operation_id))
        try:
            scope = self._check(request, context, None if request.kind == "reconcile" else operation)
            if request.kind != "reconcile":
                self._prune(context)
            if (request.kind not in {"approve", "reconcile"} or operation is None or operation.trace is None
                    or request.plan_id != operation.trace.action_id):
                return self._reply(request, context, "refused", "control_plan_unavailable")
            if not self._allowed_target(operation.trace.request.target, scope, operation.room):
                return self._reply(request, context, "refused", "flow_read_scope_unavailable")
            if request.kind == "reconcile":
                if operation.context_key != context.focus_key or operation.scope_key != self._scope_key(scope):
                    return self._reply(request, context, "refused", "flow_context_changed")
                if self.ports.read_trace:
                    async def read_owned():
                        return await self.ports.read_trace(operation.trace.action_id, context)
                    saved = await FencedAwait(read_owned(), lambda: self._read_live(request, context, operation))
                    if saved and saved.action_id == operation.trace.action_id and saved.request == operation.trace.request:
                        operation.trace = saved
                if operation.trace.status != "unknown_outcome":
                    return self._reply(request, context, "result", operation.trace.code, operation=operation)
                operation.trace = await FencedAwait(
                    self.ports.controller.reconcile(request.plan_id),
                    lambda: self._read_live(request, context, operation),
                )
                return self._reply(request, context, "result", operation.trace.code, operation=operation)
            if request.approval_digest != operation.digest:
                return self._reply(request, context, "refused", "control_approval_mismatch")
            if context.input_kind == "console_text" and scope.boundary != "loopback_console":
                return self._reply(request, context, "refused", "console_boundary_required")
            if context.input_kind != "console_text" and (
                self.ambient_permission is None or self.ambient_permission(context, operation.trace) is not True
            ):
                return self._reply(request, context, "unavailable", "ambient_control_authority_unavailable")
            if operation.approved:
                if operation.task and not operation.task.done():
                    return self._reply(request, context, "refused", "control_operation_in_progress")
                return self._reply(request, context, "result", operation.trace.code, operation=operation)
            guarded_execute = getattr(self.ports, "guarded_execute", None)
            if not callable(guarded_execute):
                return self._reply(request, context, "unavailable", "control_dispatch_guard_unavailable",
                                   "当前执行端缺少效果前校验，计划保持待批准。", operation=operation)

            def dispatch_current():
                self._check(request, context, operation)
                return True

            operation.approved = True  # Consume once before the existing domain await.
            operation.trace = await self._call(request, context, operation,
                                               guarded_execute(request.plan_id, context, dispatch_current))
            self._check(request, context, operation)
            self.sessions.commit(operation.fence, operation.trace.request.target,
                                 status=operation.trace.status, ttl_seconds=15)
            return self._reply(request, context, "result", operation.trace.code, operation=operation)
        except (FlowAbort, InteractionInvalidated, asyncio.CancelledError) as exc:
            if operation:
                self._invalidate_operation(operation)
            # The original store owns sent/unknown. A lost/cancelled await is not an undo.
            return self._reply(request, context, "cancelled", self._abort_code(exc),
                               "操作已停止；若已发送，结果需按原领域记录核对。")
        except (ValueError, OSError, TimeoutError):
            if operation:
                self._invalidate_operation(operation)
            return self._reply(request, context, "unavailable", "control_domain_unavailable")

    def _read_live(self, request, context, operation):
        try:
            scope = self._check(request, context)
            return (operation.context_key == context.focus_key and operation.scope_key == self._scope_key(scope)
                    and self._allowed_target(operation.trace.request.target, scope, operation.room))
        except ValueError:
            return False

    @staticmethod
    def _allowed_target(target, scope, room):
        return scope.all_home or target in scope.entities or room in scope.rooms

    async def cancel(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse:
        from .home_flow_contracts import FlowAbort
        try:
            self._check(request, context)
        except FlowAbort as exc:
            self.invalidate(session_id=context.session_id)
            return self._reply(request, context, "cancelled", str(exc))
        operation = self._operations.get((context.session_id, request.operation_id))
        if operation is None or operation.context_key != context.focus_key:
            return self._reply(request, context, "cancelled", "control_operation_unavailable")
        task = operation.task
        self._invalidate_operation(operation)
        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        # Read only the known, owned action reference through the scoped private port.
        if operation.trace and self.ports and self.ports.read_trace:
            try:
                context.read_scope()
                trace = await self.ports.read_trace(operation.trace.action_id, context)
                context.read_scope()
                if trace and trace.action_id == operation.trace.action_id and trace.request == operation.trace.request:
                    operation.trace = trace
            except (ValueError, OSError, TimeoutError):
                return self._reply(request, context, "cancelled", "control_cancelled_result_unavailable",
                                   "已停止等待；发送结果当前无法核验。")
        code = "control_cancelled_after_send" if (operation.trace and operation.trace.real_write_attempted) else (
            "control_cancelled_result_unavailable" if operation.approved else "control_cancelled_before_dispatch")
        return self._reply(request, context, "cancelled", code,
                           "已取消。发送后的设备结果保留原记录，不代表物理撤销。", operation=operation)
