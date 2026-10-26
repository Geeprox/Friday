"""Restricted project AST. These types do not describe a vendor Graph protocol."""

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .action_contracts import DeviceOperation, DevicePlanRequest
from .contracts import Contract, Event

Scalar = StrictBool | StrictInt | StrictFloat | StrictStr
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class TransitionTrigger(Contract):
    type: Literal["state_transition"] = "state_transition"
    entity: Identifier
    capability: Identifier
    from_value: Scalar
    to_value: Scalar

    @model_validator(mode="after")
    def genuine_change(self):
        if type(self.from_value) is not type(self.to_value) or self.from_value == self.to_value:
            raise ValueError("transition_requires_distinct_same_type_values")
        return self


class EventTrigger(Contract):
    type: Literal["event"] = "event"
    entity: Identifier
    capability: Identifier
    value: Scalar


class NumericCondition(Contract):
    type: Literal["numeric"] = "numeric"
    entity: Identifier
    capability: Identifier
    operator: Literal["lt", "le", "gt", "ge", "eq"]
    value: StrictFloat | StrictInt
    unit: str | None = Field(default=None, max_length=24)


class ModeCondition(Contract):
    type: Literal["mode"] = "mode"
    mode: Literal["home.sleep", "home.manual", "home.present"]
    equals: StrictBool
    require_valid_lease: Literal[True] = True


class TimeCondition(Contract):
    type: Literal["time_window"] = "time_window"
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def zone_and_window(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unsupported_timezone") from exc
        if self.start == self.end:
            raise ValueError("ambiguous_full_day_window")
        return self


class DecisionCondition(Contract):
    type: Literal["decision"] = "decision"
    provider: Literal["laya_mlx", "jev", "openai_decisions"]
    task: Identifier
    equals: Identifier
    require_valid_lease: Literal[True] = True


Condition = Annotated[
    NumericCondition | ModeCondition | TimeCondition | DecisionCondition, Field(discriminator="type")
]


class LightInstruction(Contract):
    type: Literal["light.set"] = "light.set"
    id: Identifier
    target: Identifier
    power: Literal["on", "off"]
    brightness: StrictInt | Literal["minimum_verified"] | None = None
    require_no_bright_flash: bool = False

    @model_validator(mode="after")
    def safe_parameters(self):
        if self.brightness is not None and self.power != "on":
            raise ValueError("brightness_requires_on")
        if isinstance(self.brightness, int) and not 1 <= self.brightness <= 255:
            raise ValueError("brightness_out_of_api_range")
        if self.brightness == "minimum_verified" and not self.require_no_bright_flash:
            raise ValueError("minimum_requires_no_flash")
        return self


class ClimateInstruction(Contract):
    type: Literal["climate.set"] = "climate.set"
    id: Identifier
    target: Identifier
    power: Literal["on", "off"]


class DeviceInstruction(Contract):
    """Absolute shared operation; no raw HA services or implicit climate mode."""

    type: Literal["device.set"] = "device.set"
    id: Identifier
    target: Identifier
    operation: DeviceOperation
    desired: dict

    @model_validator(mode="after")
    def typed_parameters(self):
        return DevicePlanRequest.typed_parameters(self)


class DelayInstruction(Contract):
    type: Literal["delay"] = "delay"
    id: Identifier
    milliseconds: int = Field(ge=1, le=30000, strict=True)
    cancel_on_new_event: Literal[True] = True


class CancelInstruction(Contract):
    type: Literal["cancel_pending"] = "cancel_pending"
    id: Identifier
    scope: Literal["same_rule"] = "same_rule"


class FeedbackInstruction(Contract):
    type: Literal["feedback.speak"] = "feedback.speak"
    id: Identifier
    endpoint: Identifier
    route: Literal["local_tts", "cloud_tts"]
    text: str = Field(min_length=1, max_length=256)


Instruction = Annotated[
    LightInstruction
    | ClimateInstruction
    | DeviceInstruction
    | DelayInstruction
    | CancelInstruction
    | FeedbackInstruction,
    Field(discriminator="type"),
]


class RuleExecution(Contract):
    preferred_backend: Literal["simulator", "home_core", "ha", "mijia_geek"] = "simulator"
    require_wan_independent: bool = False
    require_core_independent: bool = False
    replay_policy: Literal["never_actuate"] = "never_actuate"
    max_event_age_ms: int = Field(default=3000, ge=1, le=30000, strict=True)


class RuleSafety(Contract):
    policy_ref: Identifier = "policy.managed-lighting.v1"
    prohibited_targets: list[Identifier] = Field(default_factory=list, max_length=32)
    respect_sleep: Literal[True] = True
    respect_manual_override: Literal[True] = True
    allow_during_sleep: bool = False


class RuleIR(Contract):
    id: Identifier
    revision: int = Field(ge=1, strict=True)
    intent: str = Field(min_length=1, max_length=512)
    ownership: Literal["managed"] = "managed"
    trigger: Annotated[TransitionTrigger | EventTrigger, Field(discriminator="type")]
    conditions: list[Condition] = Field(default_factory=list, max_length=8)
    on_unknown: Literal["suppress_and_record"] = "suppress_and_record"
    actions: list[Instruction] = Field(min_length=1, max_length=16)
    execution: RuleExecution = Field(default_factory=RuleExecution)
    safety: RuleSafety = Field(default_factory=RuleSafety)
    simulated: bool = False

    @model_validator(mode="after")
    def input_budget(self):
        if len(self.model_dump_json().encode()) > 12288:
            raise ValueError("rule_input_budget_exceeded")
        return self

    @field_validator("actions")
    @classmethod
    def distinct_actions(cls, actions):
        if len({a.id for a in actions}) != len(actions):
            raise ValueError("duplicate_action_id")
        if not any(
            isinstance(a, (LightInstruction, ClimateInstruction, DeviceInstruction, FeedbackInstruction))
            for a in actions
        ):
            raise ValueError("rule_requires_effect")
        return actions


class RuleCompilation(Contract):
    rule_id: Identifier
    revision: int
    fingerprint: str
    status: Literal["supported", "unsupported", "requires_core"]
    backend: Literal["simulator", "home_core", "ha", "mijia_geek"]
    reasons: list[str]
    core_independent: bool = False
    wan_independent: bool = False
    bindings: dict[str, list[dict[str, str]]] = Field(default_factory=dict)
    instructions: list[dict] = Field(default_factory=list)
    simulated: bool
    compiler_version: Literal["restricted-v1"] = "restricted-v1"
    native_artifact: None = None


class ModeLease(Contract):
    value: StrictBool
    issued_at: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def bounded_lease(self):
        if not 0 < (self.valid_until - self.issued_at).total_seconds() <= 1800:
            raise ValueError("lease_ttl_required")
        return self


class RuleEvaluation(Contract):
    rule_id: Identifier
    event_id: str
    outcome: Literal["matched", "not_triggered", "suppressed", "unsupported"]
    reasons: list[str]
    commands: list[dict] = Field(default_factory=list)
    simulated: bool = True
    real_write_attempted: Literal[False] = False
    physically_confirmed: Literal[False] = False


class RuleTrial(Contract):
    event: Event
    modes: dict[str, ModeLease] = Field(default_factory=dict, max_length=3)
    replay: bool = False
