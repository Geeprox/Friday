"""Bounded public turns and privately injected flow bindings (F0 v1).

These contracts grant no domain permission. Only the app owner creates context;
the existing Action/Rule/Query/Archive services keep their own authority checks.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Callable, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import Contract
from .query_contracts import ReadScope

OpaqueID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
InputKind = Literal["console_text", "qualified_ambient", "simulated_fixture"]

MAX_SESSIONS = 32
MAX_TURNS = 16
MAX_ACTIVE_OPERATIONS = 2
SESSION_TTL_SECONDS = 60
MAX_HISTORY_BYTES = 16384
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 65536


class FlowRequest(Contract):
    """Selections and optimistic tokens are untrusted; IDs confer no scope."""

    session_id: OpaqueID
    turn_id: OpaqueID
    operation_id: OpaqueID
    idempotency_key: OpaqueID
    kind: Literal["turn", "approve", "cancel", "status", "playback", "reconcile", "rollback"] = "turn"
    lane: Literal["auto", "control", "rules", "why", "mode"] = "auto"
    text: str = Field(default="", max_length=1024, strict=True)
    room_id: OpaqueID | None = None
    device_id: OpaqueID | None = None
    camera_id: OpaqueID | None = None
    candidate_id: OpaqueID | None = None
    evidence_ref: OpaqueID | None = None
    expected_context: Digest | None = None
    plan_id: OpaqueID | None = None
    approval_digest: Digest | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=1800, strict=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid_flow_version")
        return value

    @model_validator(mode="after")
    def bounded_turn(self):
        if len(self.text.encode("utf-8")) > 4096 or any(
            ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in self.text
        ):
            raise ValueError("invalid_flow_text")
        if self.kind == "approve" and (not self.plan_id or not self.approval_digest):
            raise ValueError("exact_flow_approval_required")
        if self.kind != "approve" and self.approval_digest is not None:
            raise ValueError("flow_approval_only")
        if self.kind in {"reconcile", "rollback"} and not self.plan_id:
            raise ValueError("flow_plan_required")
        if (self.start is None) != (self.end is None):
            raise ValueError("complete_flow_window_required")
        if self.start is not None and not 0 < (self.end - self.start).total_seconds() <= 86400:
            raise ValueError("bounded_flow_window_required")
        if len(self.model_dump_json().encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("flow_request_budget")
        return self


class FlowChoice(Contract):
    id: OpaqueID
    label: str = Field(min_length=1, max_length=256, strict=True)


class FlowResponse(Contract):
    """phase is UI lifecycle, never an aggregate device/provider success flag.

    domain is an unchanged JSON projection of the original typed domain result.
    Refuse an oversized response instead of dropping status/coverage evidence.
    """

    session_id: OpaqueID
    turn_id: OpaqueID
    operation_id: OpaqueID
    phase: Literal["clarify", "select", "preview", "result", "cancelled", "unavailable", "refused"]
    code: OpaqueID
    input_kind: InputKind
    context_token: Digest | None = None
    plan_id: OpaqueID | None = None
    approval_digest: Digest | None = None
    expires_at: AwareDatetime | None = None
    caption: str = Field(default="", max_length=2048, strict=True)
    choices: list[FlowChoice] = Field(default_factory=list, max_length=16)
    domain: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list, max_length=16)
    asr_admission: Literal["unavailable", "unqualified", "qualified"] = "unavailable"
    visual_admission: Literal["unavailable", "unqualified", "qualified"] = "unavailable"
    native_graph: Literal["unavailable", "opaque", "qualified"] = "unavailable"
    nas_lease: Literal["closed", "unavailable", "metadata_only", "content_qualified"] = "closed"
    output_audibility: Literal["unavailable", "unverified", "qualified"] = "unavailable"

    @model_validator(mode="after")
    def bounded_response(self):
        if self.approval_digest is not None and self.plan_id is None:
            raise ValueError("flow_approval_plan_required")
        if any(type(v) is not str or len(v.encode("utf-8")) > 1024 for v in self.limitations):
            raise ValueError("flow_limitations_budget")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("flow_response_budget")
        return self


class FlowAbort(ValueError):
    """Stable cancellation/context/deadline code, without private binding text."""


@dataclass(frozen=True)
class TrustedFlowContext:
    """In-process only. No from_json/model_validate or public grant constructor.

    check_current must compare the app owner's current binding and permission
    revisions. It may not simply echo a client's expected_context. Console IDs
    bind the existing local boundary; they are not verified human identities.
    """

    input_kind: InputKind
    session_id: str
    source_revision: str
    authority_revision: str
    boot_id: str
    generation: int
    deadline_mono: float
    check_current: Callable[[TrustedFlowContext], bool] = field(repr=False, compare=False)
    cancelled: Callable[[], bool] = field(repr=False, compare=False)
    current_scope: Callable[[], ReadScope | None] = field(repr=False, compare=False)
    household_timezone: str | None = None
    room_id: str | None = None
    camera_id: str | None = None
    camera_session_id: str | None = None
    map_revision: str | None = None
    subject_ref: str | None = None
    track_ref: str | None = None
    track_epoch: str | None = None
    producer_ref: str | None = None
    interaction: object | None = field(default=None, repr=False, compare=False)
    admission: object | None = field(default=None, repr=False, compare=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    wall_now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False, compare=False)

    def __post_init__(self):
        if self.input_kind not in {"console_text", "qualified_ambient", "simulated_fixture"}:
            raise ValueError("invalid_private_input_kind")
        values = (self.session_id, self.source_revision, self.authority_revision, self.boot_id)
        optional = (self.room_id, self.camera_id, self.camera_session_id, self.map_revision,
                    self.subject_ref, self.track_ref, self.track_epoch, self.producer_ref)
        if any(type(v) is not str or not 0 < len(v.encode()) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in v)
               for v in (*values, *(v for v in optional if v is not None))):
            raise ValueError("invalid_private_binding")
        if type(self.generation) is not int or self.generation < 0 or type(self.deadline_mono) not in {int, float} or not math.isfinite(self.deadline_mono):
            raise ValueError("invalid_private_lifecycle")
        if self.input_kind == "console_text" and any(v is not None for v in (self.subject_ref, self.track_ref, self.track_epoch)):
            raise ValueError("console_is_not_person_identity")
        if (self.track_ref is None) != (self.track_epoch is None):
            raise ValueError("track_epoch_required")
        if any(not callable(v) for v in (self.check_current, self.cancelled, self.current_scope, self.monotonic, self.wall_now)):
            raise ValueError("private_checks_required")
        if self.household_timezone is not None:
            if type(self.household_timezone) is not str or len(self.household_timezone) > 128:
                raise ValueError("invalid_household_timezone")
            ZoneInfo(self.household_timezone)

    def check(self) -> None:
        try:
            if self.cancelled() is not False:
                raise FlowAbort("flow_cancelled")
            now = self.monotonic()
            if type(now) not in {int, float} or not math.isfinite(now) or now >= self.deadline_mono:
                raise FlowAbort("flow_expired")
            if self.check_current(self) is not True:
                raise FlowAbort("flow_context_changed")
        except FlowAbort:
            raise
        except Exception as exc:
            raise FlowAbort("flow_current_unavailable") from exc

    def read_scope(self) -> ReadScope:
        self.check()
        try:
            scope = self.current_scope()
            now = self.wall_now()
        except Exception as exc:
            raise FlowAbort("flow_read_scope_unavailable") from exc
        self.check()
        if not isinstance(scope, ReadScope) or scope.permitted is not True or scope.boundary == "unverified":
            raise FlowAbort("flow_read_scope_unavailable")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None or now >= scope.expires_at:
            raise FlowAbort("flow_read_scope_expired")
        return scope

    @property
    def focus_key(self) -> tuple:
        """Comparison key only; resolving focus never grants action authority."""
        return (self.input_kind, self.session_id, self.subject_ref, self.track_ref, self.track_epoch,
                self.room_id, self.camera_id, self.camera_session_id, self.map_revision,
                self.source_revision, self.authority_revision, self.boot_id, self.generation)
