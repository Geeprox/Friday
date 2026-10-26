"""Explicit home-domain measurement profiles. No hardware sampling or AI transport."""
import hashlib

from pydantic import AwareDatetime, Field, StrictFloat, StrictInt, model_validator

from .contracts import Contract


class DiagnosticReading(Contract):
    value: StrictFloat | StrictInt
    measured_at: AwareDatetime | None = None
    freshness_basis: str = Field(default="unknown", max_length=32)


class MeasurementProfile(Contract):
    source: str = Field(min_length=1, max_length=128)
    entity: str = Field(min_length=1, max_length=128)
    capability: str = Field(default="temperature", min_length=1, max_length=64)
    unit: str = Field(default="°C", min_length=1, max_length=24)
    location: str = Field(min_length=1, max_length=128)
    placement: str = Field(min_length=1, max_length=128)
    installation_height_m: float | None = Field(default=None, ge=0, le=10)
    reporting: str = Field(pattern="^(periodic_sample|change_triggered|latched_state|derived)$")
    context: str = Field(min_length=1, max_length=64)  # e.g. verified AC mode, never inferred from power.
    version: str = Field(min_length=1, max_length=64)
    ttl_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    plausible_minimum: float = -40
    plausible_maximum: float = 85
    freeze_window_seconds: int = Field(default=60, ge=30, le=3600, strict=True)
    freeze_change: float = Field(default=2, gt=0, le=20)
    resolution: float = Field(default=.1, gt=0, le=5)
    simulated: bool = False

    @model_validator(mode="after")
    def plausible_range(self):
        if self.plausible_minimum >= self.plausible_maximum:
            raise ValueError("invalid_measurement_range")
        return self

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class MeasurementGroup(Contract):
    entity: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=128)
    primary: str = Field(min_length=1, max_length=128)
    sources: list[str] = Field(min_length=1, max_length=8)
    recovery_seconds: int = Field(default=30, ge=1, le=300, strict=True)
    recovery_samples: int = Field(default=3, ge=2, le=20, strict=True)
    switch_hold_seconds: int = Field(default=60, ge=1, le=600, strict=True)
    disagreement_tolerance: float = Field(default=1, ge=.1, le=10)

    @model_validator(mode="after")
    def distinct_sources(self):
        if self.primary not in self.sources or len(set(self.sources)) != len(self.sources):
            raise ValueError("explicit_distinct_primary_required")
        return self


class RelativeCalibration(Contract):
    source: str
    reference: str
    source_profile: str = Field(pattern="^[a-f0-9]{64}$")
    reference_profile: str = Field(pattern="^[a-f0-9]{64}$")
    context: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    bias: float
    residual_bound: float = Field(ge=0, le=10)
    reference_minimum: float
    reference_maximum: float
    sample_count: int = Field(ge=8, le=256, strict=True)
    valid_until: AwareDatetime
    relative_only: bool = True
    simulated: bool

    @model_validator(mode="after")
    def relative_contract(self):
        if (self.source == self.reference or self.reference_minimum >= self.reference_maximum
                or self.relative_only is not True):
            raise ValueError("relative_calibration_is_not_absolute_truth")
        return self
