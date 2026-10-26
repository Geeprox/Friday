"""Duck-typed F1 seam, not another NAS transport or mount implementation.

The manager owns all credentials, path bindings, worker lifetimes and no-follow
file IO. F2 accepts only its public metadata and 1 MiB source-bound ranges.
"""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import OpaqueRef

RANGE_BYTES = 1048576
ERROR_CODES = frozenset(
    {
        "nas_not_configured",
        "nas_disabled",
        "nas_unavailable",
        "nas_busy",
        "nas_source_changed",
        "nas_object_unknown",
        "nas_cursor_expired",
        "nas_missing",
        "nas_revision_changed",
        "nas_invalid_request",
        "nas_budget_exceeded",
        "nas_auth_failed",
        "nas_access_denied",
        "nas_host_key_mismatch",
        "nas_dependency_missing",
        "nas_timeout",
        "nas_transport_failed",
    }
)


class BoundedNASReader(Protocol):
    def status(self) -> Any: ...  # F1 cached getter; async fixtures are accepted too.

    async def list_page(
        self,
        source_ref: str,
        cursor: str | None = None,
        limit: int = 128,
        *,
        directory_ref: str | None = None,
    ) -> Any: ...

    async def stat(
        self,
        source_ref: str,
        object_ref: str,
        expected_revision: str | None = None,
    ) -> Any: ...

    async def read_range(
        self,
        source_ref: str,
        object_ref: str,
        expected_revision: str,
        offset: int,
        length: int,
    ) -> bytes: ...


class ReaderStatus(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, strict=True)
    source_ref: OpaqueRef | None
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef | None
    state: Literal["not_configured", "disabled", "connecting", "online", "backoff", "paused", "stopped"]
    reachable: bool | None = None
    authenticated: bool | None = None
    readable: bool | None = None

    @property
    def online(self):
        return bool(
            self.source_ref
            and self.connection_epoch
            and self.state == "online"
            and self.reachable is True
            and self.authenticated is True
            and self.readable is True
        )

    @property
    def binding(self):
        return [self.source_ref, self.config_generation, self.connection_epoch]


class ReaderEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, strict=True)
    object_ref: OpaqueRef
    kind: Literal["file", "directory"]
    revision: OpaqueRef
    size: int = Field(ge=0)
    modified_ns: int | None = None
    media_kind: Literal["image", "video", "audio", "other"]
    closed: bool | None = None
    revision_basis: Literal["metadata"] = "metadata"


class ReaderPage(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, strict=True)
    source_ref: OpaqueRef
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef
    entries: list[ReaderEntry] = Field(max_length=128)
    next_cursor: OpaqueRef | None = None
    enumeration_complete: bool
    completed_scan_token: OpaqueRef | None = None
    snapshot_consistent: Literal[False] = False


def wire(value):
    return value.model_dump(mode="python") if hasattr(value, "model_dump") else value


def error_code(error):
    if isinstance(error, TimeoutError):
        return "nas_timeout"
    code = getattr(error, "code", None)
    return code if code in ERROR_CODES else "nas_transport_failed"
