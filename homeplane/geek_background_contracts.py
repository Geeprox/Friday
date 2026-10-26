"""Local read-only connector protocol; never an Agent or device-write API."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .geek_session import BackgroundAttempt, FailureCode, FailurePhase


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
