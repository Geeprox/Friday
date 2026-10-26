"""Local profile edits do not supply observations, identity or hardware timestamps."""

from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import CanonicalState, Contract
from .measurement_profiles import MeasurementGroup, MeasurementProfile, RelativeCalibration


class ProfileFields(Contract):
    placement: str = Field(min_length=1, max_length=128)
    installation_height_m: float | None = Field(default=None, ge=0, le=10)
    reporting: Literal["periodic_sample", "change_triggered", "latched_state", "derived"]
    expected_context: Literal["ambient", "cooling", "heating", "fan-only", "unknown"]
    ttl_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    representativeness: Literal["unreviewed", "room_reference", "contextual_air"] = "unreviewed"

    @field_validator("placement")
    @classmethod
    def plain_placement(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("invalid_placement")
        return value


class MeasurementChange(Contract):
    realm: str = Field(pattern="^[a-f0-9]{64}$")
    expected_revision: str = Field(min_length=1, max_length=64)
    source_key: str = Field(pattern="^[a-f0-9]{64}$")
    binding_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")


class EditMeasurement(MeasurementChange):
    fields: ProfileFields


class ExcludeMeasurement(MeasurementChange):
    duration_seconds: int = Field(ge=30, le=86400, strict=True)


class SavedMeasurement(Contract):
    source_key: str = Field(pattern="^[a-f0-9]{64}$")
    binding_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
    profile: MeasurementProfile
    edited: bool = False
    excluded_until: AwareDatetime | None = None
    representativeness: Literal["unreviewed", "room_reference", "contextual_air"] = "unreviewed"


class GroupMember(Contract):
    source_key: str = Field(pattern="^[a-f0-9]{64}$")
    binding_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
    identity_fingerprint: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


class EditMeasurementGroup(Contract):
    realm: str = Field(pattern="^[a-f0-9]{64}$")
    expected_revision: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=96)
    primary_key: str = Field(pattern="^[a-f0-9]{64}$")
    members: list[GroupMember] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def distinct_members(self):
        keys = [m.source_key for m in self.members]
        if len(set(keys)) != len(keys) or self.primary_key not in keys:
            raise ValueError("distinct_group_members_required")
        return self


class SavedMeasurementGroup(Contract):
    group: MeasurementGroup
    members: list[GroupMember] = Field(min_length=1, max_length=8)
    independence_basis: Literal["single_source", "simulated_fixture", "upstream_xiaomi_identifiers"]


class SavedCalibration(Contract):
    group_entity: str
    group_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
    calibration: RelativeCalibration
    approved_at: AwareDatetime


class MeasurementConfiguration(Contract):
    revision: str = Field(min_length=1, max_length=64)
    sources: list[SavedMeasurement] = Field(default_factory=list, max_length=32)
    groups: list[SavedMeasurementGroup] = Field(default_factory=list, max_length=16)
    calibrations: list[SavedCalibration] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_entries(self):
        keys = [s.source_key for s in self.sources]
        entities = [g.group.entity for g in self.groups]
        rooms = [g.group.location for g in self.groups]
        calibrations = [(c.group_entity, c.calibration.source) for c in self.calibrations]
        if (
            len(set(keys)) != len(keys)
            or len(set(entities)) != len(entities)
            or len(set(rooms)) != len(rooms)
            or len(set(calibrations)) != len(calibrations)
        ):
            raise ValueError("duplicate_measurement_configuration")
        return self


class MeasurementSource(Contract):
    source_key: str
    binding_fingerprint: str
    entity_id: str
    physical_source_id: str
    name: str
    room: str | None
    unit: Literal["°C"] = "°C"
    capability: Literal["temperature"] = "temperature"
    configured: bool
    binding_valid: bool
    profile: MeasurementProfile | None = None
    excluded_until: AwareDatetime | None = None
    excluded: bool = False
    state: CanonicalState | None = None
    simulated: bool
    identity_fingerprint: str | None = None
    representativeness: Literal["unreviewed", "room_reference", "contextual_air"] = "unreviewed"


class MeasurementGroupView(Contract):
    entity: str
    name: str
    location: str
    primary_key: str | None
    source_keys: list[str]
    managed: bool
    binding_valid: bool
    independence_basis: str
    physical_independence_verified: Literal[False] = False
    state: CanonicalState | None
    calibration_status: Literal["none", "active", "invalid", "expired"]
    calibration: RelativeCalibration | None = None
    reasons: list[str]


class PreviewRelativeCalibration(Contract):
    realm: str = Field(pattern="^[a-f0-9]{64}$")
    expected_revision: str = Field(min_length=1, max_length=64)
    group_entity: str = Field(min_length=1, max_length=128)
    source_key: str = Field(pattern="^[a-f0-9]{64}$")


class CalibrationPreview(Contract):
    candidate_id: str
    fingerprint: str
    realm: str
    revision: str
    group_entity: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    calibration: RelativeCalibration
    installed: Literal[False] = False
    sample_history_persisted: Literal[False] = False
    evidence_refs: list[str] = Field(max_length=32)


class ApproveRelativeCalibration(Contract):
    realm: str = Field(pattern="^[a-f0-9]{64}$")
    expected_revision: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
    reviewed_relative_only: Literal[True]


class MeasurementSettingsView(Contract):
    realm: str
    revision: str
    generated_at: AwareDatetime
    simulated: bool
    sources: list[MeasurementSource] = Field(max_length=256)
    omitted_count: int = Field(ge=0)
    limits: list[str]
    groups: list[MeasurementGroupView] = Field(default_factory=list, max_length=16)
