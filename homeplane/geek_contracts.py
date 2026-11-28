"""Geek 会话、观察清单及后台服务的共有数据契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

REVISION = "4fe2c03ce16c365838b58f70e955e987d1a23531"
FailurePhase = Literal["local_setup", "websocket_connect", "authenticate", "inventory"]
FailureCode = Literal[
    "connection_refused", "network_unreachable", "gateway_closed_connection", "connection_timeout",
    "websocket_upgrade_rejected", "authentication_timeout", "protocol_selection_failed",
    "authentication_handshake_failed", "gateway_error_frame", "unexpected_handshake_frame",
    "readonly_request_timeout", "session_not_connected", "check_failed",
]


class Changes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    added: int = Field(ge=0, le=64)
    removed: int = Field(ge=0, le=64)
    modified: int = Field(ge=0, le=64)


class WorkerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    kind: Literal["status"]
    provider: Literal["oh-my-sage-readonly"]
    revision: Literal[REVISION]
    status: Literal["starting", "online", "reconnecting", "blocked", "stopped"]
    simulated: Literal[False]
    writes_attempted: Literal[False]
    graph_semantics: Literal["opaque"]
    checked_at: str = Field(max_length=40)
    reconnects: int = Field(ge=0, le=2147483647)
    device_count: int | None = Field(default=None, ge=0, le=4096)
    graph_count: int | None = Field(default=None, ge=0, le=64)
    scope_count: int | None = Field(default=None, ge=0, le=32)
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    changes: Changes | None = None
    gap: bool = False
    session_seconds: int | None = Field(default=None, ge=0, le=2147483647)
    failure_phase: FailurePhase | None = None
    failure_code: FailureCode | None = None
    network_errno: Literal["ECONNREFUSED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPERM", "EACCES"] | None = None

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value):
        if value is None:
            return value
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("worker_time_requires_zone")
        return value


class LastChange(Changes):
    checked_at: str = Field(max_length=40)


class BackgroundAttempt(BaseModel):
    """Last preparation result, distinct from the current owner's state."""
    model_config = ConfigDict(extra="forbid", strict=True)
    checked_at: str = Field(max_length=40)
    outcome: Literal["connected", "failed"]
    failure_phase: FailurePhase | None = None
    failure_code: FailureCode | None = None
    network_errno: Literal["ECONNREFUSED", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPERM", "EACCES"] | None = None

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("attempt_time_requires_zone")
        return value


class GeekSessionView(WorkerStatus):
    schema_version: Literal[1] = 1
    kind: Literal["status"] = "status"
    provider: Literal["oh-my-sage-readonly"] = "oh-my-sage-readonly"
    revision: Literal[REVISION] = REVISION
    status: Literal[
        "stopped", "starting", "online", "reconnecting", "blocked", "stale",
        "worker_stopped", "worker_fault", "worker_unavailable", "pairing_required",
        "unsupported_gateway_address",
        "background_unavailable",
    ]
    simulated: Literal[False] = False
    writes_attempted: Literal[False] = False
    graph_semantics: Literal["opaque"] = "opaque"
    checked_at: str | None = Field(default=None, max_length=40)
    reconnects: int = Field(default=0, ge=0, le=2147483647)
    last_change: LastChange | None = None
    session_live: bool
    credential_available: bool
    inventory_stale: bool
    poll_seconds: Literal[60] = 60
    session_resume: Literal["saved_pin_reauthentication"] = "saved_pin_reauthentication"
    permanent_login_verified: Literal[False] = False
    raw_graphs_public: Literal[False] = False
    retention_days: Literal[7] = 7
    coverage: Literal["geek_visible_only"] = "geek_visible_only"
    external_rules_intercepted: Literal[False] = False
    connection_host: Literal["console_child", "background_service"] = "console_child"
    background_service_live: bool = False
    keep_connected: bool = False
    background_last_attempt: BackgroundAttempt | None = None


class GeekGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=128)
    source_enabled: bool | None = None
    normalization: Literal["opaque"] = "opaque"
    readable: Literal[True] = True
    writable: Literal[False] = False
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class GeekInventoryView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    session: GeekSessionView
    status: Literal["ready", "unavailable", "stale", "integrity_failed"]
    observed_at: str | None = None
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    graphs: list[GeekGraph] = Field(default_factory=list, max_length=64)
    coverage: Literal["geek_visible_only"] = "geek_visible_only"
    simulated: Literal[False] = False
    raw_graphs_public: Literal[False] = False
    external_rules_intercepted: Literal[False] = False


class BackgroundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    operation: Literal["status", "inventory", "connect", "disconnect", "diagnostics"]
    url: str | None = Field(default=None, max_length=512)


class ConnectionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    keep_connected: bool = False
    url: str | None = Field(default=None, max_length=512)
    credential_stamp: str | None = Field(default=None, max_length=128)
    authentication_blocked: bool = False
    automatic_retry_blocked: bool = False
    failure_status: Literal["blocked", "worker_stopped", "worker_fault", "worker_unavailable"] | None = None
    failure_phase: FailurePhase | None = None
    failure_code: FailureCode | None = None


class BackgroundInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    label: Literal["local.friday.geek-readonly"] = "local.friday.geek-readonly"
    installed: Literal[True] = True
    use_for_connections: bool = False
    last_attempt: BackgroundAttempt | None = None
