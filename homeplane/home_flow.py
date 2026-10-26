"""Console session/router binding to the existing app's domain instances.

Only transient fences live here. Domain records, grants, scheduling and effects
remain owned by ActionController, RuleManager, HomeQueries and ArchivePlayer.
The shipped ingress is local text; live writes, ambient input and audio stay held.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import Field

from .contracts import Contract
from .control_conversation import ControlConversation
from .feedback_contracts import VerifiedFeedback
from .home_explanation import HomeExplanation
from .home_flow_contracts import (
    MAX_ACTIVE_OPERATIONS,
    MAX_HISTORY_BYTES,
    MAX_SESSIONS,
    MAX_TURNS,
    SESSION_TTL_SECONDS,
    FlowAbort,
    FlowRequest,
    FlowResponse,
    TrustedFlowContext,
)
from .home_flow_ports import (
    ActionPorts,
    ArchivePorts,
    ModePorts,
    QueryPorts,
    RulePorts,
    bind_guarded_action_executor,
)
from .query_contracts import ReadScope
from .response_router import ResponseRouter
from .rule_conversation import RuleConversation
from .rule_text import parse_removal
from .simulator import Simulator
from .sleep_status import LONG_TEXT, MODE_TEXT, PAUSE_TEXT, SleepStatus

from .runtime_log import observe


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Operation:
    lane: str
    token: str | None = None
    domain_token: str | None = None
    cancelled: bool = False
    task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None
    report: FlowResponse | None = None


@dataclass
class Session:
    id: str
    binding: tuple
    scope: ReadScope
    deadline: float
    expires_at: datetime
    invalidated: bool = False
    turns: set = field(default_factory=set)
    keys: set = field(default_factory=set)
    history_bytes: int = 0
    active: str | None = None
    operations: dict = field(default_factory=dict)


class FlowSessionRequest(Contract):
    session_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')


class HomeFlow:
    def __init__(self, core, settings, rules, rule_text, queries, archive, player,
                 rule_runtime, core_rule_runtime, *, nas_status, household_timezone=None,
                 monotonic=time.monotonic, wall_now=None):
        if household_timezone is not None:
            ZoneInfo(household_timezone)
        self.core, self.settings = core, settings
        self.rule_runtime, self.core_rule_runtime = rule_runtime, core_rule_runtime
        self.archive, self.player, self.nas_status = archive, player, nas_status
        self.timezone, self.monotonic = household_timezone, monotonic
        self.wall_now = wall_now or (lambda: datetime.now(UTC))
        self.sessions, self.tasks, self.bootstrap_tasks = {}, set(), set()
        self.generation, self.closed = 0, False
        self.control = ControlConversation(ActionPorts(
            core.actions, self._snapshot, read_trace=self._read_trace,
            guarded_execute=bind_guarded_action_executor(core.actions),
        ))
        self.rules = RuleConversation(RulePorts(rule_text, rules))
        # This metadata capability comes from the same original local Console
        # boundary and explicit original camera profiles. Content remains held.
        self.why = HomeExplanation(QueryPorts(queries), ArchivePorts(
            archive, player, verify_metadata=self._archive_metadata,
            cameras_for_room=self._cameras_for_room,
        ))
        self.mode = SleepStatus(ModePorts(
            backend='simulator', permitted=self._mode_permitted,
            set_mode=self._set_mode, status=self._mode_status,
            pause_and_invalidate=self._pause, fixed_ttl_seconds=30,
        ))
        self.feedback = ResponseRouter()  # No installed or qualified room output.

    def _binding(self):
        adapter = self.core.adapter
        nas = self.nas_status()
        return (id(adapter), getattr(adapter, 'boot_id', None),
                getattr(adapter, 'instance_id', None), self.core.boot_id,
                self.settings.revision, self.core.actions.policy_report()['revision'],
                self.archive.revision, nas.config_generation,
                getattr(nas, 'connection_epoch', None), self.generation)

    def _current(self, session):
        if self.closed or session.invalidated or self.sessions.get(session.id) is not session:
            return False
        if self.monotonic() >= session.deadline or self.wall_now() >= session.expires_at:
            self.invalidate(session.id)
            return False
        try:
            current = session.binding == self._binding()
        except Exception:
            current = False
        if not current:
            self.invalidate(session.id)
        return current

    def _context(self, session):
        return TrustedFlowContext(
            input_kind='console_text', session_id=session.id,
            source_revision=digest(session.binding[:4]),
            authority_revision=digest(session.binding[4:]), boot_id=self.core.boot_id,
            generation=session.binding[-1], deadline_mono=session.deadline,
            check_current=lambda _context: self._current(session),
            cancelled=lambda: session.invalidated,
            current_scope=lambda: session.scope if self._current(session) else None,
            household_timezone=self.timezone, monotonic=self.monotonic, wall_now=self.wall_now,
        )

    @staticmethod
    def _reply(request, code, phase='refused', caption='本次会话不可用，请重新读取会话。'):
        return FlowResponse(session_id=request.session_id, turn_id=request.turn_id,
                            operation_id=request.operation_id, input_kind='console_text',
                            phase=phase, code=code, caption=caption)

    @observe('flow.bootstrap', upstream='local')
    async def bootstrap(self):
        if self.closed:
            raise FlowAbort('flow_closed')
        for key, old in list(self.sessions.items()):
            if not self._current(old):
                self.invalidate(key)
                if not any(op.task and not op.task.done() for op in old.operations.values()):
                    self.sessions.pop(key, None)
        if len(self.sessions) >= MAX_SESSIONS:
            raise FlowAbort('flow_session_capacity')
        binding = self._binding()
        task = asyncio.create_task(self.core.snapshot(), name='console-home-flow-bootstrap')
        self.tasks.add(task)
        self.bootstrap_tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self.bootstrap_tasks.discard)
        try:
            snapshot = await task
        except asyncio.CancelledError as exc:
            raise FlowAbort('flow_cancelled') from exc
        if self.closed or binding != self._binding():
            raise FlowAbort('flow_context_changed')
        identifier = 'console:' + secrets.token_hex(24)
        now, mono = self.wall_now(), self.monotonic()
        expires = now + timedelta(seconds=SESSION_TTL_SECONDS)
        session = Session(identifier, binding, ReadScope(
            subject='console', revision='local-console-flow-v1', boundary='loopback_console',
            all_home=True, history=True, permitted=True, expires_at=expires,
            rooms=frozenset(sorted({entity.room for entity in snapshot.entities if entity.room})[:64]),
            entities=frozenset(sorted({entity.id for entity in snapshot.entities})[:256]),
        ), mono + SESSION_TTL_SECONDS, expires)
        self.sessions[identifier] = session
        return FlowResponse(session_id=identifier, turn_id='bootstrap', operation_id='bootstrap',
                            input_kind='console_text', phase='result', code='flow_ready',
                            expires_at=expires, caption='本机会话已就绪，有效期 60 秒。')

    @observe('flow.invalidate', upstream='local')
    def invalidate(self, session_id=None):
        """Synchronous fence before any binding mutation or owned-task await."""
        for key, session in list(self.sessions.items()):
            if session_id is not None and key != session_id:
                continue
            session.invalidated = True
            self.control.invalidate(key)
            self.rules.invalidate(key)
            self.why.invalidate(key)
            self.mode.invalidate(key)
            self.feedback.invalidate(key)
            for operation in session.operations.values():
                operation.cancelled = True
                if operation.task and not operation.task.done():
                    operation.task.cancel()
        if session_id is None:
            self.generation += 1
            # Bootstrap has not minted a session yet, but its original snapshot
            # work still belongs to the application and the whole source fence.
            for task in tuple(self.bootstrap_tasks):
                task.cancel()

    @observe('flow.dispose', upstream='local')
    async def dispose(self, session_id):
        self.invalidate(session_id)
        session = self.sessions.get(session_id)
        tasks = [task for op in session.operations.values() for task in (op.task, op.cancel_task)
                 if task] if session else []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.pop(session_id, None)
        return {'code':'flow_session_disposed'}

    async def _snapshot(self, context):
        context.read_scope()
        result = await self.core.snapshot()
        context.read_scope()
        return result

    async def _read_trace(self, action_id, context):
        scope = context.read_scope()
        trace = self.core.store.light_action(action_id)
        if trace is None:
            return None
        snapshot = await self._snapshot(context)
        entity = next((e for e in snapshot.entities if e.id == trace.request.target), None)
        context.read_scope()
        return trace if entity and (scope.all_home or entity.id in scope.entities or entity.room in scope.rooms) else None

    def _cameras_for_room(self, context, room):
        scope = context.read_scope()
        if not scope.all_home and room not in scope.rooms:
            return ()
        result = tuple(c['id'] for c in self.archive.context()['cameras']
                       if c.get('room') == room and c.get('source_binding_current') is not False)
        context.check()
        return result

    def _archive_metadata(self, context, request):
        scope = context.read_scope()
        current = self.archive.context()
        camera = next((c for c in current['cameras'] if c['id'] == request.camera_id), None)
        context.check()
        return bool(camera and camera.get('source_binding_current') is not False
                    and request.expected_context == current['fingerprint']
                    and (scope.all_home or camera.get('room') in scope.rooms))

    def _mode_permitted(self, context, operation):
        scope = context.read_scope()
        return scope.boundary == 'loopback_console' and scope.all_home and isinstance(self.core.adapter, Simulator)

    def _mode_status(self, context):
        context.check()
        return self.rule_runtime.status() if isinstance(self.core.adapter, Simulator) else {'available':False}

    async def _set_mode(self, mode, *, ttl_seconds, context):
        context.check()
        if not self._mode_permitted(context, 'mode') or ttl_seconds not in {None, 30}:
            raise FlowAbort('mode_permission_required')
        result = await self.rule_runtime.set_mode(mode)
        context.check()
        return result

    async def _pause(self, context):
        if not self._mode_permitted(context, 'pause'):
            raise FlowAbort('mode_permission_required')
        # Original simulator governed runtime only; no fabricated action grant.
        await self.rule_runtime.invalidate('explicit_console_pause')
        await self.core_rule_runtime.invalidate('explicit_console_pause')
        context.check()
        return {'status':'paused', 'simulated':True, 'governed_only':True,
                'external_rules_intercepted':False, 'real_write_attempted':False}

    @staticmethod
    def _lane(request):
        if request.lane != 'auto':
            return request.lane
        text = request.text.strip().rstrip('。！？!?')
        if text in MODE_TEXT or text in PAUSE_TEXT or text in LONG_TEXT:
            return 'mode'
        if '为什么' in text or '为何' in text:
            return 'why'
        if parse_removal(text) is not None:
            return 'rules'
        return 'control'

    def _accept_ids(self, session, request, *, cleanup=False):
        if request.turn_id in session.turns or request.idempotency_key in session.keys:
            raise FlowAbort('flow_duplicate_turn')
        if len(session.turns) >= MAX_TURNS * (2 if cleanup else 1):
            raise FlowAbort('flow_turn_budget')
        size = len(request.text.encode())
        if session.history_bytes + size > MAX_HISTORY_BYTES:
            raise FlowAbort('flow_history_budget')
        session.turns.add(request.turn_id)
        session.keys.add(request.idempotency_key)
        session.history_bytes += size

    async def _cancel_operation(self, session, operation_id, request):
        operation = session.operations.get(operation_id)
        if operation is None:
            return self._reply(request, 'flow_operation_unavailable', 'cancelled')
        operation.cancelled = True
        if operation.cancel_task is None:
            # Exactly one captured cleanup owns the lane call. Repeating F1's
            # invalidation could revoke a new operation's reused focus slot.
            context = self._context(session)
            task = operation.task
            captured = request.model_copy(update={'kind':'cancel', 'operation_id':operation_id,
                'expected_context':operation.domain_token, 'lane':operation.lane})
            if task and not task.done():
                task.cancel()

            async def cleanup():
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                try:
                    # Use the whole-session context without setting its fence
                    # or calling F4's whole-session cancel API.
                    if operation.lane == 'control':
                        return await self.control.cancel(captured, context)
                    if operation.lane == 'rules':
                        return await self.rules.cancel(captured, context)
                    if operation.lane == 'why':
                        return await self.why.cancel(captured, context)
                    return self._reply(request, 'flow_cancelled', 'cancelled',
                                       '本次请求已停止；原模式租约与已发送动作保留。')
                except (FlowAbort, asyncio.CancelledError):
                    return self._reply(request, 'flow_cancelled', 'cancelled')
                except Exception:
                    return self._reply(request, 'flow_cancelled_result_unavailable', 'cancelled',
                                       '已停止等待；原领域结果暂不可核验。')

            operation.cancel_task = asyncio.create_task(cleanup(), name='console-home-flow-cancel')
            self.tasks.add(operation.cancel_task)
            operation.cancel_task.add_done_callback(self.tasks.discard)
        # Client cancellation cannot abandon the captured cleanup. Replacement,
        # repeat cancel, dispose and shutdown all await the same owned task.
        report = await asyncio.shield(operation.cancel_task)
        return report.model_copy(update={'operation_id':request.operation_id,
                                         'turn_id':request.turn_id, 'context_token':operation.token})

    @observe('flow.cancel', upstream='local', key='request.operation_id')
    async def cancel(self, request):
        session = self.sessions.get(request.session_id)
        if session is None:
            return self._reply(request, 'flow_session_unavailable', 'cancelled')
        # Expiry/source changes fence new work, but cannot revoke ownership of
        # an existing child. Cleanup must join that captured operation before
        # reporting cancellation; it never renews the session or its grant.
        self._current(session)
        try:
            self._accept_ids(session, request, cleanup=True)
            return await self._cancel_operation(session, request.operation_id, request)
        except FlowAbort as exc:
            return self._reply(request, str(exc), 'refused')

    @observe('flow.turn', upstream='local', key='request.operation_id')
    async def send(self, request):
        if request.kind == 'cancel':
            return await self.cancel(request)
        session = self.sessions.get(request.session_id)
        if session is None or not self._current(session):
            return self._reply(request, 'flow_session_unavailable')
        operation = None
        try:
            context = self._context(session)
            context.read_scope()
            self._accept_ids(session, request)
            operation = session.operations.get(request.operation_id)
            if operation is None:
                if (request.kind not in {'turn','status'} or request.expected_context is not None
                        or request.candidate_id or request.plan_id or request.evidence_ref):
                    raise FlowAbort('flow_operation_unavailable')
                if sum(op.task is not None and not op.task.done() for op in session.operations.values()) >= MAX_ACTIVE_OPERATIONS:
                    raise FlowAbort('flow_operation_budget')
                if session.active:
                    await self._cancel_operation(session, session.active, request)
                    context.check()
                operation = Operation(self._lane(request))
                session.operations[request.operation_id] = operation
                session.active = request.operation_id
            else:
                if operation.cancelled or session.active != request.operation_id:
                    raise FlowAbort('flow_operation_finished')
                if operation.task and not operation.task.done():
                    raise FlowAbort('flow_operation_in_progress')
                if request.expected_context != operation.token or operation.token is None:
                    raise FlowAbort('flow_context_changed')
                if request.lane not in {'auto', operation.lane}:
                    raise FlowAbort('flow_lane_changed')
            bound = request.model_copy(update={'lane':operation.lane,
                'expected_context':operation.domain_token})
            async def dispatch():
                context.check()
                if operation.cancelled:
                    raise FlowAbort('flow_cancelled')
                if (not isinstance(self.core.adapter, Simulator)
                        and request.kind in {'approve','rollback'}):
                    return self._reply(request, 'flow_live_write_held', 'unavailable')
                if operation.lane == 'control':
                    if request.kind in {'approve','reconcile'}:
                        if request.kind == 'approve':
                            await self.core_rule_runtime.invalidate('explicit_console_action_takes_over')
                            context.check()
                        return await self.control.approve(bound, context)
                    return await self.control.plan(bound, context)
                if operation.lane == 'rules':
                    return await (self.rules.approve(bound, context) if request.kind == 'approve'
                                  else self.rules.turn(bound, context))
                if operation.lane == 'why':
                    return await (self.why.prepare_playback(bound, context) if request.kind == 'playback'
                                  else self.why.answer(bound, context))
                return await self.mode.change(bound, context)
            task = asyncio.create_task(dispatch(), name='console-home-flow')
            operation.task = task
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            try:
                report = await task
            finally:
                operation.task = None
            context.check()
            if operation.cancelled or session.active != request.operation_id:
                raise FlowAbort('flow_cancelled')
            if (not isinstance(report, FlowResponse)
                    or (report.session_id, report.operation_id, report.turn_id, report.input_kind)
                    != (request.session_id, request.operation_id, request.turn_id, 'console_text')):
                raise FlowAbort('flow_report_unavailable')
            operation.domain_token = report.context_token
            operation.report = report
            receipt = VerifiedFeedback.from_response(report, context,
                verify_report=lambda candidate, current: (
                    self._current(session) and not operation.cancelled
                    and session.active == request.operation_id
                    and candidate == operation.report and current.focus_key == context.focus_key))
            report = await self.feedback.deliver(receipt, context)
            context.check()
            operation.token = secrets.token_hex(32)
            return report.model_copy(update={'context_token':operation.token,
                'expires_at':min(report.expires_at, session.expires_at) if report.expires_at else session.expires_at})
        except (FlowAbort, asyncio.CancelledError) as exc:
            return self._reply(request, str(exc) if isinstance(exc, FlowAbort) else 'flow_cancelled', 'cancelled')
        except Exception:
            # Stable code only: no private paths, authority or backend exception text.
            return self._reply(request, 'flow_backend_unavailable', 'unavailable')

    @observe('flow.close', upstream='local')
    async def close(self):
        self.closed = True
        self.invalidate()
        # Each handler waits its own current and retired work before shared close.
        await self.control.close()
        await self.rules.close()
        await self.why.close()
        await self.mode.close()
        await self.feedback.close()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.sessions.clear()


def create_router(flow, lock):
    router = APIRouter(prefix='/api/home-flow')

    @router.post('/bootstrap', response_model=FlowResponse)
    async def bootstrap():
        async with lock:
            try:
                return await flow.bootstrap()
            except FlowAbort as exc:
                return FlowResponse(session_id='bootstrap', turn_id='bootstrap', operation_id='bootstrap',
                                    input_kind='console_text', phase='unavailable', code=str(exc),
                                    caption='本机会话暂不可用，请稍后重新开始。')

    @router.post('/turn', response_model=FlowResponse)
    async def turn(body: FlowRequest):
        async with lock:
            return await flow.send(body)

    @router.post('/cancel', response_model=FlowResponse)
    async def cancel(body: FlowRequest):
        if body.kind != 'cancel':
            return flow._reply(body, 'flow_cancel_required')
        return await flow.cancel(body)

    @router.post('/dispose')
    async def dispose(body: FlowSessionRequest):
        return await flow.dispose(body.session_id)

    @router.post('/context', response_model=FlowResponse)
    async def context(body: FlowSessionRequest):
        session = flow.sessions.get(body.session_id)
        if session is None or not flow._current(session):
            return FlowResponse(session_id=body.session_id, turn_id='bootstrap', operation_id='bootstrap',
                                input_kind='console_text', phase='unavailable', code='flow_session_unavailable')
        return FlowResponse(session_id=session.id, turn_id='bootstrap', operation_id='bootstrap',
                            input_kind='console_text', phase='result', code='flow_current',
                            expires_at=session.expires_at)

    return router
