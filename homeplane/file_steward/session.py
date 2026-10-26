"""Read-only projection of Friday's existing cached NAS session status."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from homeplane.nas_connection.contracts import ERRORS, NASConnectionStatus


class _ARLScope(BaseModel):
    """Local validation of task-10's passive schema; no ARL implementation import."""

    model_config = ConfigDict(extra="forbid", strict=True)
    source_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,96}$")
    credential_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    credential_generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_generation: int = Field(gt=0, le=2**63 - 1)


def arl_session_projection(value, context):
    """Consume task-10 schema 1 metadata only, never its resolver/controller/transport."""
    projected = {
        "state": "paused",
        "reason": "handoff_unsupported",
        "label": "来源接点待启用",
        "retry_credentials": False,
        "handoff_service": "arl.nas.read-session.v1",
        "configuration_saved": False,
        "credential_reference_saved": False,
        "credential_state": "unsupported",
        "connected": None,
        "readable": None,
        "writable": "unsupported",
        "content_grant_provided": False,
    }
    if value.get("schema_version") != 1 or value.get("service") != projected["handoff_service"]:
        return projected
    try:
        connection = NASConnectionStatus.model_validate(value.get("connection"))
    except ValidationError:
        return projected
    credential = value.get("credential", {})
    if not isinstance(credential, dict) or value.get("steward_content_grant") != "not_provided":
        return projected
    credential_state = credential.get("state")
    if credential_state not in {
        "unsupported",
        "disabled",
        "not_checked",
        "resolving",
        "resolved",
        "failed",
        "revoked",
        "cancelled",
    }:
        return projected
    projected.update(
        configuration_saved=connection.configured,
        credential_reference_saved=value.get("credential_reference_saved") is True,
        credential_state=credential_state,
        state=connection.state,
        reason=connection.failure,
    )
    if connection.failure and connection.failure not in ERRORS:
        projected.update(state="paused", reason="handoff_unsupported")
        return projected
    if not connection.configured:
        projected.update(label="来源待配置")
        return projected
    if connection.failure in {
        "nas_auth_failed",
        "nas_access_denied",
        "nas_host_key_mismatch",
    } or credential_state in {"failed", "revoked", "cancelled"}:
        projected.update(state="paused", reason="authentication_required", label="核对 NAS 授权")
        return projected
    if connection.state in {"disabled", "not_configured", "stopped"}:
        # A stopped manager may advance its generation beyond immutable scope.
        # Preserve this negative state; it supplies no connected/readable evidence.
        projected.update(label="来源待连接")
        return projected
    try:
        scope = _ARLScope.model_validate(value.get("scope"))
    except ValidationError:
        projected.update(state="paused", reason="handoff_unsupported", label="来源接点待启用")
        return projected
    if (
        context is None
        or (
            connection.source_ref,
            connection.config_generation,
            connection.connection_epoch,
        )
        != (
            context.source_ref,
            context.config_generation,
            context.connection_epoch,
        )
        or (scope.source_ref, scope.config_generation)
        != (
            connection.source_ref,
            connection.config_generation,
        )
    ):
        projected.update(state="paused", reason="binding_changed", label="核对来源配置")
        return projected
    connected = (
        connection.enabled
        and connection.state == "online"
        and connection.reachable is True
        and connection.authenticated is True
    )
    projected.update(
        connected=True if connected else None if connection.state == "online" else False,
        readable=connection.readable if connected else None,
        label="目录在线"
        if connected and connection.readable is True
        else "已连接 · 待读取核对"
        if connected
        else "连接待核对"
        if connection.state == "online"
        else "来源离线 · 保留索引",
    )
    return projected


def session_projection(status, context=None):
    value = status.model_dump() if hasattr(status, "model_dump") else status
    if isinstance(value, dict) and ("service" in value or "connection" in value):
        return arl_session_projection(value, context)
    reason = value.get("failure")
    if reason in {"nas_auth_failed", "nas_access_denied", "nas_host_key_mismatch"}:
        return {
            "state": "paused",
            "reason": "authentication_required",
            "label": "核对 NAS 授权",
            "retry_credentials": False,
        }
    state = value.get("state", "not_configured")
    connection_required = state in {"not_configured", "disabled", "stopped"}
    return {
        "state": state,
        "reason": reason or ("connection_required" if connection_required else None),
        "label": "需连接 NAS" if connection_required else "目录在线" if state == "online" else "来源离线 · 保留索引",
        "retry_credentials": False,
    }
