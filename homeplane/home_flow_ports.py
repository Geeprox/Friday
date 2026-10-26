"""Typed in-process references to original domain instances, no second writer.

Absent private ports are unavailable. F5 binds these under the original app
locks/lifecycle. Public JSON can neither construct ports nor submit feedback.
"""
from __future__ import annotations

from dataclasses import dataclass
from inspect import signature
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol, TypeVar

from .home_flow_contracts import FlowAbort, FlowRequest, FlowResponse, TrustedFlowContext

if TYPE_CHECKING:
    from .action_contracts import LightExecutionTrace
    from .actions import ActionController
    from .archive_contracts import ArchivePlaybackPlan, ArchivePlaybackRequest, ArchiveResolveRequest
    from .archive_index import ArchiveIndex
    from .archive_playback import ArchivePlayer
    from .contracts import Snapshot
    from .query import HomeQueries
    from .rule_deployment import RuleManager
    from .rule_text import RuleTextChanges


T = TypeVar("T")


async def guarded_call(context: TrustedFlowContext, operation: Callable[[], Awaitable[T]]) -> T:
    """Check before/after await; caller-owned cancellation must also fence IO.

    This helper alone cannot stop dispatch inside ActionController.execute.
    Its managed_guard argument is not a Console binding fence.
    """
    context.check()
    result = await operation()
    context.check()
    return result


class SnapshotReader(Protocol):
    async def __call__(self, context: TrustedFlowContext) -> Snapshot: ...


class ScopedTraceReader(Protocol):
    async def __call__(self, action_id: str, context: TrustedFlowContext) -> LightExecutionTrace | None: ...


class CurrentActionReader(Protocol):
    async def __call__(self, target: str, expected_state_version: str,
                       context: TrustedFlowContext) -> dict | None: ...


class OperationGuard(Protocol):
    """Private F1 check: current focus/operation/source/scope/TTL, exact True."""
    def __call__(self) -> bool: ...


class GuardedActionExecutor(Protocol):
    async def __call__(self, action_id: str, context: TrustedFlowContext,
                       operation_guard: OperationGuard) -> LightExecutionTrace: ...


def bind_guarded_action_executor(controller: ActionController) -> GuardedActionExecutor:
    """Bind the actual original controller with the explicit synchronous seam.

    There is no ordinary-execute fallback or public capability flag. The
    dispatch guard runs inside the controller after snapshot/policy and before
    its durable sent claim. All domain truth and post-send uncertainty remain.
    """
    from .actions import ActionController
    if (type(controller) is not ActionController
            or getattr(controller.execute, "__func__", None) is not ActionController.execute
            or "dispatch_guard" not in signature(controller.execute).parameters):
        raise FlowAbort("control_dispatch_guard_unavailable")

    async def execute(action_id: str, context: TrustedFlowContext,
                      operation_guard: OperationGuard) -> LightExecutionTrace:
        def checked() -> bool:
            context.check()
            context.read_scope()
            if operation_guard() is not True:
                raise FlowAbort("flow_operation_invalidated")
            context.check()
            return True

        checked()
        result = await controller.execute(action_id, dispatch_guard=checked)
        checked()
        return result

    return execute


@dataclass(frozen=True)
class ActionPorts:
    controller: ActionController
    snapshot: SnapshotReader
    read_trace: ScopedTraceReader | None = None
    read_current: CurrentActionReader | None = None
    guarded_execute: GuardedActionExecutor | None = None


@dataclass(frozen=True)
class RulePorts:
    text: RuleTextChanges
    manager: RuleManager


@dataclass(frozen=True)
class QueryPorts:
    queries: HomeQueries
    read_trace: ScopedTraceReader | None = None


class ArchiveMetadataVerifier(Protocol):
    def __call__(self, context: TrustedFlowContext, request: ArchiveResolveRequest) -> bool: ...


class ArchiveContentVerifier(Protocol):
    def __call__(self, context: TrustedFlowContext, request: ArchivePlaybackRequest) -> bool: ...


class ArchiveContentDeadline(Protocol):
    def __call__(self, context: TrustedFlowContext, request: ArchivePlaybackRequest) -> float | None: ...


class ArchiveReaderGuard(Protocol):
    """F5 capability backed by the actual media route, not a public flag.

    Registration binds scope/source/cancel/permission checks and the earlier
    content/context/domain expiry. Route checks before IO, after each await and
    before yielding chunks; failure revokes the original plan and closes reader.
    No implementation is supplied by F0. None keeps content preparation held.
    """
    def bind(self, plan: ArchivePlaybackPlan, request: ArchivePlaybackRequest,
             context: TrustedFlowContext, *, deadline_mono: float) -> bool: ...
    def revoke(self, plan_id: str, context: TrustedFlowContext) -> None: ...


class RoomCameraMapping(Protocol):
    def __call__(self, context: TrustedFlowContext, room_id: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class ArchivePorts:
    index: ArchiveIndex
    player: ArchivePlayer
    verify_metadata: ArchiveMetadataVerifier | None = None
    verify_content: ArchiveContentVerifier | None = None
    content_deadline: ArchiveContentDeadline | None = None
    reader_guard: ArchiveReaderGuard | None = None
    cameras_for_room: RoomCameraMapping | None = None


class ModePermission(Protocol):
    def __call__(self, context: TrustedFlowContext, operation: Literal["mode", "pause"]) -> bool: ...


class ModeChange(Protocol):
    async def __call__(self, mode: str, *, ttl_seconds: int | None,
                       context: TrustedFlowContext) -> dict: ...


class ModeStatus(Protocol):
    def __call__(self, context: TrustedFlowContext) -> dict: ...


class PauseAndInvalidate(Protocol):
    async def __call__(self, context: TrustedFlowContext) -> dict: ...


@dataclass(frozen=True)
class ModePorts:
    backend: Literal["unavailable", "simulator", "home_core"] = "unavailable"
    permitted: ModePermission | None = None
    set_mode: ModeChange | None = None
    status: ModeStatus | None = None
    pause_and_invalidate: PauseAndInvalidate | None = None
    fixed_ttl_seconds: int | None = None


class FlowHandler(Protocol):
    async def __call__(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse: ...


class FlowDispatcher(Protocol):
    async def send(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse: ...
    async def cancel(self, request: FlowRequest, context: TrustedFlowContext) -> FlowResponse: ...
    async def close(self) -> None: ...
