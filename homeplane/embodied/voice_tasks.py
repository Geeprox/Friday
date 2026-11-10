"""Admitted voice prepares a bounded memory-only task, never a rule or approval.

The local Console adopts an unchanged proposal using its own current context.
Only the existing rule owner can persist/enable it after normal exact approval.
"""

import asyncio
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal

from fastapi import HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from homeplane.home_flow_contracts import FlowAbort, TrustedFlowContext
from homeplane.preview_request import prepare_for_viewer
from homeplane.query_contracts import ReadScope
from homeplane.rule_compiler import digest
from homeplane.rule_text import RuleTextRequest
from homeplane.task_composition import PreparedTask, TaskTextResult, is_task_request

from .intent import address
from .voice_question_contracts import VoiceQuestion


class VoiceTaskAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    camera: str = Field(pattern=r"^cam_[a-f0-9]{10}$")
    camera_source_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    voice_revision: int = Field(ge=0, strict=True)
    status: Literal["pending_confirmation", "clarify", "unavailable"]
    proposal_ref: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    configured: Literal[False] = False
    device_authority: Literal[False] = False
    reply: str = Field(min_length=1, max_length=256)
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def pending_reference(self):
        if (self.status == "pending_confirmation") != (self.proposal_ref is not None):
            raise ValueError("voice_task_reference_required")
        return self


@dataclass
class PendingTask:
    id: str
    text: str
    room: str
    prepared: PreparedTask
    expires_at: datetime
    deadline: float
    source_current: Callable = field(repr=False)
    live: bool = True
    claimed_by: tuple | None = None

    def current(self):
        try:
            return bool(
                self.live
                and time.monotonic() < self.deadline
                and datetime.now(UTC) < self.expires_at
                and self.source_current()
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False


class VoiceTasks:
    def __init__(self, source):
        self.source, self.flow = source, source.flow
        self.composer = self.flow.task_plans.ports.text
        self.pending, self.receipts, self.tasks, self.stops = {}, {}, set(), set()
        self.closed = False
        self.flow.task_plans.proposals = self
        self.flow.voice_tasks = self

    def prune(self):
        for ref, item in list(self.pending.items()):
            if not item.current():
                item.live = False
                self.pending.pop(ref, None)
        for key, (_, ref, deadline) in list(self.receipts.items()):
            if time.monotonic() >= deadline and ref not in self.pending:
                self.receipts.pop(key, None)

    def listing(self):
        self.prune()
        return dict(
            proposals=[
                dict(
                    id=item.id,
                    text=item.text,
                    room=item.room,
                    expires_at=item.expires_at.isoformat(),
                    task_review=item.prepared.task_review,
                )
                for item in self.pending.values()
                if item.claimed_by is None
            ]
        )

    def discard(self, ref):
        item = self.pending.pop(ref, None)
        if item:
            item.live = False
        return dict(status="discarded")

    def claim(self, ref, context, operation_id):
        context.check()
        scope = context.read_scope()
        if (
            self.closed
            or context.input_kind != "console_text"
            or scope.subject != "console"
            or scope.boundary != "loopback_console"
            or not scope.all_home
        ):
            raise FlowAbort("task_proposal_unavailable")
        self.prune()
        item = self.pending.get(ref)
        key = (context.session_id, operation_id)
        if item is None or not item.current():
            raise FlowAbort("task_proposal_expired")
        if item.claimed_by not in {None, key}:
            raise FlowAbort("task_proposal_already_claimed")
        item.claimed_by = key
        return item

    async def close(self):
        self.closed = True
        for item in self.pending.values():
            item.live = False
        for stop in self.stops:
            stop.set()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.pending.clear()
        self.receipts.clear()

    async def receive(self, request, incoming, key):
        if not 0 <= time.monotonic() - request.captured_mono <= 10:
            raise HTTPException(409, detail="voice_task_expired")
        self.prune()
        fingerprint = digest(request.model_dump())
        previous = self.receipts.get(request.request_id)
        if previous:
            old_fingerprint, ref, _ = previous
            item = self.pending.get(ref)
            if old_fingerprint != fingerprint or item is None or not item.current():
                raise HTTPException(409, detail="voice_task_retired")
            return self._answer(request, "pending_confirmation", ref)
        if (
            self.closed
            or request.camera in self.source.active
            or len(self.source.active) >= 4
            or len(self.pending) >= 8
            or len(self.receipts) >= 64
        ):
            raise HTTPException(409, detail="voice_task_busy")
        try:
            binding = self.source._binding(request, key)
            admitted = address(request.text, request.attending, wake_word=binding[4].wake_word)
            body = admitted.get("body", "")
            if not admitted["admitted"] or not is_task_request(body):
                raise FlowAbort("voice_task_not_admitted")
        except (FlowAbort, OSError, ValueError, KeyError, TypeError):
            raise HTTPException(409, detail="voice_task_not_admitted") from None
        stopped = threading.Event()
        self.stops.add(stopped)
        session = "voice-task:" + request.request_id
        deadline = min(time.monotonic() + 30, request.captured_mono + 40)
        expires = datetime.now(UTC) + timedelta(seconds=deadline - time.monotonic())
        revision = hashlib.sha256(repr((binding, request.request_id, expires)).encode()).hexdigest()

        def current_input():
            try:
                return bool(
                    not stopped.is_set()
                    and not self.closed
                    and self.source.active.get(request.camera) == session
                    and self.source._binding(request, key) == binding
                )
            except (OSError, ValueError, KeyError, TypeError):
                return False

        scope = ReadScope(
            subject="embodied_voice",
            revision=revision,
            expires_at=expires,
            boundary="qualified_ambient",
            all_home=True,
            permitted=True,
        )
        context = TrustedFlowContext(
            input_kind="qualified_ambient",
            session_id=session,
            source_revision=request.camera_source_context,
            authority_revision=revision,
            boot_id=self.flow.core.boot_id,
            generation=request.voice_revision,
            deadline_mono=deadline,
            check_current=lambda _: current_input(),
            cancelled=stopped.is_set,
            current_scope=lambda: scope if current_input() else None,
            room_id=binding[3],
            camera_id=request.camera,
            producer_ref="friday.worker.voice_task.v1",
            household_timezone=self.flow.timezone,
        )
        text_request = RuleTextRequest(
            text=body,
            expected_context=self.composer.context().fingerprint,
            idempotency_key="voice-task:" + request.request_id,
            expires_at=expires,
        )

        async def run():
            context.check()
            task = asyncio.create_task(
                self.composer.prepare(
                    text_request,
                    context=context,
                    operation_guard=lambda: not stopped.is_set(),
                    draft_only=True,
                ),
                name="voice-task-prepare",
            )
            self.tasks.add(task)
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.05)
                    context.check()
                prepared = task.result()
                context.check()
                if isinstance(prepared, TaskTextResult):
                    return self._answer(
                        request, "clarify" if prepared.status == "clarification" else "unavailable"
                    )
                if not isinstance(prepared, PreparedTask):
                    raise FlowAbort("voice_task_unavailable")

                # Proposal admission ends the audio-age lease. The remembered
                # source, policy, worker and authority still fence later review;
                # this does not retimestamp old audio or grant ambient writes.
                def source_current():
                    return (
                        not self.closed
                        and self.source._binding(request, key, require_fresh=False) == binding
                        and self.composer.context().fingerprint == prepared.context.fingerprint
                    )

                ref = secrets.token_hex(32)
                item = PendingTask(
                    ref,
                    body,
                    binding[3],
                    prepared,
                    datetime.now(UTC) + timedelta(minutes=5),
                    time.monotonic() + 300,
                    source_current,
                )
                if len(self.pending) >= 8:
                    raise FlowAbort("voice_task_busy")
                self.pending[ref] = item
                self.receipts[request.request_id] = (fingerprint, ref, request.captured_mono + 40)
                return self._answer(request, "pending_confirmation", ref)
            finally:
                # Fence before cancelling work which may ignore cancellation.
                stopped.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.tasks.discard(task)

        self.source.active[request.camera] = session
        try:
            return await prepare_for_viewer(
                incoming,
                run,
                timeout=max(0, deadline - time.monotonic()),
                invalidate=stopped.set,
                discard=lambda answer: self.discard(answer.proposal_ref) if answer.proposal_ref else None,
            )
        except (FlowAbort, ValueError, TimeoutError):
            raise HTTPException(409, detail="voice_task_context_changed") from None
        finally:
            stopped.set()
            self.stops.discard(stopped)
            if self.source.active.get(request.camera) == session:
                self.source.active.pop(request.camera, None)

    @staticmethod
    def _answer(request, status, ref=None):
        return VoiceTaskAnswer(
            request_id=request.request_id,
            camera=request.camera,
            camera_source_context=request.camera_source_context,
            voice_revision=request.voice_revision,
            status=status,
            proposal_ref=ref,
            valid_until=datetime.now(UTC) + timedelta(seconds=30),
            reply={
                "pending_confirmation": "方案已准备好，请在 Friday 核对后启用。",
                "clarify": "请再说清楚触发条件和要执行的步骤。",
                "unavailable": "这项任务暂时无法完整安排，请调整任务内容。",
            }[status],
        )


def install(router, source):
    owner = VoiceTasks(source)

    @router.post("/api/embodied/task", response_model=VoiceTaskAnswer)
    async def task(request: VoiceQuestion, incoming: Request):
        try:
            key = (source.private / "control-key").read_text().strip()
        except OSError:
            raise HTTPException(403) from None
        if not 32 <= len(key) <= 512 or not secrets.compare_digest(
            key, incoming.headers.get("x-friday-worker", "")
        ):
            raise HTTPException(403)
        return await owner.receive(request, incoming, key)

    @router.get("/api/home-flow/task-proposals")
    async def proposals():
        return owner.listing()

    @router.post("/api/home-flow/task-proposals/{reference}/discard")
    async def discard(reference: str):
        return owner.discard(reference)

    @router.post("/api/embodied/task/{reference}/discard")
    async def worker_discard(reference: str, incoming: Request):
        try:
            key = (source.private / "control-key").read_text().strip()
        except OSError:
            raise HTTPException(403) from None
        if not 32 <= len(key) <= 512 or not secrets.compare_digest(
            key, incoming.headers.get("x-friday-worker", "")
        ):
            raise HTTPException(403)
        return owner.discard(reference)

    return owner
