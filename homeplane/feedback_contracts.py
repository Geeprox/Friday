"""F4 private receipts and bounded display/output evidence; no write grants."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

from .home_flow_contracts import FlowAbort, FlowResponse, TrustedFlowContext

PROMPTS = {
    "ack": "已收到。",
    "clarify": "需要你确认目标或范围。",
    "result": "结果已显示，请查看记录与覆盖范围。",
    "failure": "本次操作未完成，请查看原因。",
    "unknown": "当前结果未知，请查看原记录。",
    "mode": "有限保护窗口已显示，请查看剩余时间。",
    "paused": "暂停回执已显示；外部规则不受拦截。",
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class VerifiedFeedback:
    """F5 creates this only after accepting a current original domain response.

    The verifier must prove operation/report ownership and current scope. There
    is intentionally no JSON decoder or public result submission endpoint.
    """

    report: FlowResponse
    room_id: str | None
    binding: tuple
    verify_report: Callable[[FlowResponse, TrustedFlowContext], bool] = field(repr=False, compare=False)

    @classmethod
    def from_response(cls, report, context, *, verify_report):
        context.check()
        if not isinstance(report, FlowResponse) or not callable(verify_report):
            raise FlowAbort("feedback_receipt_required")
        receipt = cls(report.model_copy(deep=True), context.room_id, context.focus_key, verify_report)
        receipt.check(context)
        return receipt

    def check(self, context):
        context.check()
        if (
            self.binding != context.focus_key
            or self.room_id != context.room_id
            or self.report.session_id != context.session_id
            or self.report.input_kind != context.input_kind
        ):
            raise FlowAbort("feedback_context_changed")
        try:
            if self.verify_report(self.report, context) is not True:
                raise FlowAbort("feedback_report_unverified")
        except FlowAbort:
            raise
        except Exception as exc:
            raise FlowAbort("feedback_report_unavailable") from exc
        context.check()


@dataclass(frozen=True)
class RoomEndpoint:
    """Server-owned explicit room mapping. Mapping is not audibility evidence."""

    room_id: str
    endpoint_id: str
    mapping_revision: str
    volume: float = 0.25
    quiet: bool = False
    minimum_priority: int = 0

    def __post_init__(self):
        if any(type(v) is not str or not 0 < len(v.encode()) <= 128 for v in (
            self.room_id, self.endpoint_id, self.mapping_revision
        )):
            raise ValueError("invalid_room_endpoint")
        if type(self.volume) not in {int, float} or not 0 <= self.volume <= 1:
            raise ValueError("invalid_output_volume")
        if type(self.quiet) is not bool or type(self.minimum_priority) is not int or not 0 <= self.minimum_priority <= 3:
            raise ValueError("invalid_output_policy")


@dataclass(frozen=True)
class PlaybackWindow:
    operation_id: str
    room_id: str
    endpoint_id: str
    prompt_id: str
    started_mono: float
    ended_mono: float | None = None
    outcome: str = "attempted"
    simulated: bool = False


@dataclass(frozen=True)
class LocalOutputReport:
    status: str
    code: str
    endpoint_id: str | None = None
    configured: bool = False
    attempted: bool = False
    completed: bool = False
    simulated: bool = False
    audibility: Literal["unavailable", "unverified"] = "unavailable"
    cleanup: Literal["not_needed", "reaped", "pending"] = "not_needed"
    playback_window: PlaybackWindow | None = None

    def json(self):
        return asdict(self)


@dataclass
class _Operation:
    binding: tuple
    digest: str
    deadline: float
    cancelled: bool = False
    task: asyncio.Task | None = None
    response: FlowResponse | None = None


class _Operations:
    """Caller-owned tasks/dedupe only, not a scheduler, lease store or outbox."""

    def __init__(self, limit=64):
        self.limit = limit
        self.entries = {}

    def begin(self, key, context, fingerprint):
        context.check()
        now = context.monotonic()
        self.entries = {
            k: v for k, v in self.entries.items()
            if v.deadline > now or v.task and not v.task.done()
        }
        existing = self.entries.get(key)
        if existing:
            if existing.binding != context.focus_key or existing.digest != fingerprint:
                raise FlowAbort("flow_operation_conflict")
            self.check(existing, context)
            return existing, True
        if len(self.entries) >= self.limit:
            raise FlowAbort("flow_operation_capacity")
        entry = _Operation(context.focus_key, fingerprint, context.deadline_mono)
        self.entries[key] = entry
        return entry, False

    @staticmethod
    def check(entry, context):
        context.check()
        if entry.cancelled:
            raise FlowAbort("flow_cancelled")
        if entry.binding != context.focus_key or context.monotonic() >= entry.deadline:
            raise FlowAbort("flow_expired")

    async def run(self, entry, context, operation):
        self.check(entry, context)
        task = asyncio.create_task(operation(), name="home-flow-f4-operation")
        task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
        entry.task = task
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, entry.deadline - context.monotonic()))
            if not done:
                entry.cancelled = True
                task.cancel()
                raise FlowAbort("flow_expired")
            result = task.result()
            self.check(entry, context)
            return result
        except asyncio.CancelledError as exc:
            entry.cancelled = True
            task.cancel()
            raise FlowAbort("flow_cancelled") from exc
        finally:
            if entry.cancelled and not task.done():
                await asyncio.wait({task}, timeout=0.25)
            if task.done():
                entry.task = None
                # Retrieve a late exception without publishing or replaying it.
                if not task.cancelled():
                    task.exception()

    def invalidate(self, session_id):
        for key, entry in self.entries.items():
            if key[0] == session_id:
                entry.cancelled = True
                if entry.task and not entry.task.done():
                    entry.task.cancel()

    async def wait_session(self, session_id):
        tasks = {e.task for k, e in self.entries.items()
                 if k[0] == session_id and e.task and not e.task.done()}
        if tasks:
            await asyncio.wait(tasks, timeout=0.5)
        return not any(e.task and not e.task.done() for k, e in self.entries.items() if k[0] == session_id)

    async def close(self):
        for key in tuple(self.entries):
            self.invalidate(key[0])
        tasks = {e.task for e in self.entries.values() if e.task and not e.task.done()}
        if tasks:
            await asyncio.wait(tasks, timeout=0.5)
        return not any(e.task and not e.task.done() for e in self.entries.values())
