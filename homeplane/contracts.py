"""Versioned project contracts, not vendor API declarations."""
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1


class Binding(Contract):
    source: str
    external_id: str


class Capability(Contract):
    name: str
    access: Literal["read", "write", "event"]
    support: Literal["supported", "unsupported", "unknown"] = "unknown"
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    minimum_verified: bool = False
    atomic_power_brightness: bool = False
    no_bright_flash_verified: bool = False
    verification: Literal["unknown", "simulated", "protocol", "hardware"] = "unknown"
    wan_dependency: Literal["yes", "no", "unknown"] = "unknown"
    choices: list[str] = Field(default_factory=list, max_length=128)
    step: float | None = Field(default=None, gt=0)


class Entity(Contract):
    id: str
    name: str
    kind: str
    room: str | None = None
    physical_source_id: str
    bindings: list[Binding] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    simulated: bool = False
    state_version: str | None = None


class PhysicalDevice(Contract):
    """Product projection; grouping functions does not merge measurement evidence."""
    id: str
    name: str
    kind: str
    room: str | None = None
    function_ids: list[str]
    bindings: list[Binding] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    identity_basis: Literal["xiaomi_identifier", "ha_device_registry", "simulated"]
    category: Literal["device", "service"] = "device"
    limitations: list[str] = Field(default_factory=list)
    simulated: bool = False


class Observation(Contract):
    entity_id: str
    capability: str
    value: Any
    unit: str | None = None
    physical_source_id: str
    ingest_source: str
    measured_at: AwareDatetime | None = None
    source_reported_at: AwareDatetime | None = None
    integration_written_at: AwareDatetime | None = None
    received_at: AwareDatetime
    freshness_basis: Literal["device_sample", "upstream_report", "cache_write", "unknown"] = "unknown"
    cached: bool | None = None
    device_sequence: str | None = None
    clock_uncertainty_seconds: float | None = Field(default=None, ge=0)
    semantics: Literal["periodic_sample", "change_triggered", "latched_state", "derived"] = "periodic_sample"
    transport_health: Literal["online", "offline", "unknown"] = "unknown"
    simulated: bool = False


    @field_validator("value")
    @classmethod
    def finite_value(cls, value):
        import math
        def check(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("nonfinite_observation")
            if isinstance(item, dict):
                for part in item.values():
                    check(part)
            if isinstance(item, (list, tuple)):
                for part in item:
                    check(part)
        check(value)
        return value


class CanonicalState(Contract):
    id: str
    value: Any = None
    unit: str | None = None
    quality: Literal["healthy", "suspect", "stale", "faulty", "unknown"]
    reasons: list[str]
    selected_source: str | None = None
    excluded_sources: dict[str, str] = Field(default_factory=dict)
    as_of: AwareDatetime
    valid_until: AwareDatetime | None = None
    resolver_version: str = "conservative-v1"
    calibration_version: str | None = None
    inferred: bool = False
    value_interval: tuple[float, float] | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    simulated: bool = False

    @field_validator("value_interval")
    @classmethod
    def valid_interval(cls, value):
        if value is not None and value[0] > value[1]:
            raise ValueError("invalid_measurement_interval")
        return value


class SourceQualityEvidence(Contract):
    physical_source_id: str
    entity_id: str
    quality: Literal["healthy", "suspect", "stale", "faulty", "unknown"]
    reasons: list[str]
    profile_version: str
    profile_fingerprint: str | None = None
    location: str
    placement: str
    installation_height_m: float | None = None
    reporting: str | None = None
    ttl_seconds: int | None = None
    context: str
    distinct_sample_count: int
    recovery_samples: int
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    simulated: bool


class Event(Contract):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    kind: Literal["state-snapshot", "state-transition", "source-report", "quality-change", "action-result", "observation-gap"]
    source_seq: str | None = None
    correlation_id: str
    causation_id: str | None = None
    wall_time: AwareDatetime
    received_at: AwareDatetime | None = None
    time_basis: Literal["upstream_event", "local_event", "unknown"] = "unknown"
    monotonic_time: float
    boot_id: str
    payload: dict[str, Any]
    sensitivity: Literal["public", "household", "sensitive"] = "household"
    simulated: bool = False


class ExternalRuleRecord(Contract):
    id: str
    source: str
    raw: str | None
    fingerprint: str | None
    normalization: Literal["exact", "partial", "opaque"] = "opaque"
    readable: bool = False
    writable: bool = False
    coverage_note: str
    simulated: bool = False


class ActionRequest(Contract):
    target: str = Field(min_length=1, max_length=256)
    capability: Literal["light.set"] = "light.set"
    desired: dict[str, Any]
    actor: Literal["console", "cli", "test"] = "console"
    origin: Literal["dry-run"] = "dry-run"
    expires_at: AwareDatetime
    expected_version: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)


    @field_validator("desired")
    @classmethod
    def safe_action_shape(cls, value):
        import math
        if not value or set(value) - {"power", "brightness"}:
            raise ValueError("unsupported_parameters")
        if "power" in value and value["power"] not in ("on", "off"):
            raise ValueError("unsupported_parameters")
        if "brightness" in value and (isinstance(value["brightness"], bool)
                or not isinstance(value["brightness"], (int, float))
                or not math.isfinite(value["brightness"])):
            raise ValueError("unsupported_parameters")
        return value


class ActionTrace(Contract):
    action_id: str
    correlation_id: str
    request: ActionRequest
    status: Literal["planned", "rejected", "expired"]
    code: str
    route: Literal["dry-run"] = "dry-run"
    reasons: list[str]
    events: list[Event] = Field(default_factory=list)
    real_write_attempted: Literal[False] = False
    simulated: bool


class Snapshot(Contract):
    mode: Literal["simulator", "ha-read-only"]
    version: str
    generated_at: AwareDatetime
    entities: list[Entity]
    observations: list[Observation]
    states: list[CanonicalState]
    rules: list[ExternalRuleRecord]
    events: list[Event]
    limitations: list[str]
    devices: list[PhysicalDevice] = Field(default_factory=list)
    action_targets: list[str] = Field(default_factory=list)
    derived_entities: list[Entity] = Field(default_factory=list, max_length=32)
    quality_sources: list[SourceQualityEvidence] = Field(default_factory=list, max_length=64)
    simulator_scenario: str | None = Field(default=None, max_length=64)
