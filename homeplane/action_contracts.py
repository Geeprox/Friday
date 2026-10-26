"""Controlled actions are separate from dry-run/replay contracts."""

from typing import Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import ActionRequest, Contract, Event


class LightPlanRequest(Contract):
    target: str = Field(min_length=1, max_length=256)
    desired: dict[str, Any]
    actor: Literal["console", "cli", "test"] = "console"
    expected_state_version: str = Field(min_length=1, max_length=256)
    expires_at: AwareDatetime
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("desired")
    @classmethod
    def light_parameters(cls, value):
        ActionRequest.safe_action_shape(value)
        if value.get("power") not in {"on", "off"}:
            raise ValueError("explicit_absolute_power_required")
        if "brightness" in value:
            if (
                value["power"] != "on"
                or type(value["brightness"]) is not int
                or not 1 <= value["brightness"] <= 255
            ):
                raise ValueError("invalid_ha_brightness")
        return value


DeviceOperation = Literal[
    "light.set",
    "switch.set",
    "fan.set",
    "fan.percentage",
    "climate.mode",
    "climate.temperature",
    "cover.position",
    "cover.state",
    "lock.set",
    "number.value",
    "select.option",
]


class DevicePlanRequest(Contract):
    target: str = Field(min_length=1, max_length=256)
    operation: DeviceOperation
    desired: dict[str, Any]
    actor: Literal["console", "cli", "test"] = "console"
    expected_state_version: str = Field(min_length=1, max_length=256)
    expires_at: AwareDatetime
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def typed_parameters(self):
        import math

        if self.operation == "light.set":
            LightPlanRequest.light_parameters(self.desired)
            return self
        key = {
            "switch.set": "power",
            "fan.set": "power",
            "fan.percentage": "percentage",
            "climate.mode": "hvac_mode",
            "climate.temperature": "temperature",
            "cover.position": "position",
            "cover.state": "state",
            "lock.set": "lock",
            "number.value": "value",
            "select.option": "option",
        }[self.operation]
        if set(self.desired) != {key}:
            raise ValueError("unsupported_parameters")
        value = self.desired[key]
        if (
            key == "power"
            and value not in ("on", "off")
            or key == "lock"
            and value not in ("locked", "unlocked")
        ):
            raise ValueError("absolute_state_required")
        if key in {"percentage", "position"} and (type(value) is not int or not 0 <= value <= 100):
            raise ValueError("invalid_percentage")
        if key in {"temperature", "value"} and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError("finite_numeric_parameter_required")
        if key in {"hvac_mode", "option"} and (
            not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("invalid_choice")
        if key == "state" and value not in ("open", "closed"):
            raise ValueError("absolute_state_required")
        return self


class HomeDeviceAuthority(Contract):
    """Private operator grant. Runtime peers do not inherit Console device authority."""

    enabled: bool = False
    authority_ref: str = Field(min_length=1, max_length=256)
    ha_url: str = Field(min_length=1, max_length=512)
    ha_instance: str = Field(min_length=1, max_length=96)
    scope: Literal["all_registered_devices"]
    operations: list[DeviceOperation] = Field(min_length=1, max_length=11)
    external_entity_ids: list[str] | None = Field(default=None, min_length=1, max_length=256)
    expires_at: AwareDatetime | None

    @field_validator("operations")
    @classmethod
    def unique_operations(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("duplicate_operation")
        return value

    @field_validator('external_entity_ids')
    @classmethod
    def bounded_entity_scope(cls, value):
        import re
        if value is not None and (len(value) != len(set(value)) or any(
                not re.fullmatch(r'(?:light|switch|climate|cover|fan|lock|number|select)\.[a-z0-9_]{1,220}', item)
                for item in value)):
            raise ValueError('invalid_entity_scope')
        return value


class CoreRuleEffect(DevicePlanRequest):
    """Internal runtime request; public plan endpoints never accept this actor."""

    actor: Literal["core_rule"] = "core_rule"
    rule_id: str = Field(min_length=1, max_length=128)
    rule_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=128)


class LightExecutionTrace(Contract):
    action_id: str
    correlation_id: str
    request: LightPlanRequest | DevicePlanRequest | CoreRuleEffect
    created_at: AwareDatetime
    boot_id: str
    authority_revision: str | None = None
    target_binding_fingerprint: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    status: Literal[
        "planned", "rejected", "expired", "sent", "accepted", "reported_match", "failed", "unknown_outcome"
    ]
    code: str
    route: Literal["ha-scoped-light", "ha-scoped-device"] = "ha-scoped-light"
    reasons: list[str]
    events: list[Event] = Field(default_factory=list)
    real_write_attempted: bool = False
    physically_confirmed: Literal[False] = False
    reported: dict[str, Any] | None = None
    simulated: Literal[False] = False


class ExecutePlan(Contract):
    plan_id: str = Field(min_length=1, max_length=128)


class ResumeDeviceControls(Contract):
    expected_revision: str = Field(pattern="^[a-f0-9]{64}$")


class LightAuthority(Contract):
    """Local operator policy; no HTTP endpoint accepts arbitrary service authority."""

    enabled: bool = False
    authority_ref: str = Field(min_length=1, max_length=256)
    ha_url: str = Field(min_length=1, max_length=512)
    ha_instance: str = Field(min_length=1, max_length=96)
    target: str = Field(min_length=1, max_length=256)
    external_entity_id: str = Field(pattern=r"^light\.[a-z0-9_]+$", max_length=256)
    registry_device_id: str = Field(min_length=1, max_length=128)
    allow_brightness: bool = False
    expires_at: AwareDatetime
