"""Camera/time identities and bounded readonly archive operations, not vendor APIs."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import Contract


class ArchiveTimeRange(Contract):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def bounded_range(self):
        if not 0 < (self.end - self.start).total_seconds() <= 86400:
            raise ValueError("archive_segment_range_budget")
        return self


class ArchiveResolveRequest(Contract):
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    camera_id: str = Field(min_length=1, max_length=128)
    time_range: ArchiveTimeRange

    @model_validator(mode="after")
    def query_budget(self):
        if (self.time_range.end - self.time_range.start).total_seconds() > 1800:
            raise ValueError("archive_query_range_budget")
        return self


class ArchiveCameraEdit(Contract):
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    folder_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    camera_id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    room: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    timing_reviewed: bool = False
    clock_offset_seconds: float | None = Field(default=None, ge=-86400, le=86400)
    clock_uncertainty_seconds: float | None = Field(default=None, ge=0, le=60)
    completion_convention: Literal["unreviewed", "closed_files_operator_reviewed"] = "unreviewed"
    arrival_delay_seconds: int | None = Field(default=None, ge=0, le=86400)
    source_retention_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name", "room")
    @classmethod
    def safe_label(cls, value):
        if value is not None and (
            not value.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
        ):
            raise ValueError("invalid_archive_label")
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("unknown_archive_timezone") from None
        return value

    @model_validator(mode="after")
    def reviewed_clock(self):
        if self.timing_reviewed and (
            self.clock_offset_seconds is None or self.clock_uncertainty_seconds is None
        ):
            raise ValueError("archive_clock_bounds_required")
        return self


class ArchiveSegmentRef(Contract):
    media_ref: str
    camera_id: str
    time_range: ArchiveTimeRange
    status: Literal["ready", "pending", "missing", "expired", "unavailable"]
    reason: str
    file_signature: str | None = None
    size_bytes: int | None = None
    audio_track: bool | None = None
    codecs: list[str] = Field(default_factory=list, max_length=4)
    media_time_bases: list[str] = Field(default_factory=list, max_length=4)
    clock_offset_seconds: float | None = None
    clock_uncertainty_seconds: float | None = None
    time_basis: Literal["simulated", "operator_reviewed_filename"]
    completion_basis: Literal["simulated", "operator_reviewed_convention", "unverified"]
    simulated: bool
    playable_verified: Literal[False] = False
    hardware_clock_verified: Literal[False] = False
    seek_error_seconds: None = None


class ArchiveResolution(Contract):
    context_fingerprint: str
    camera_id: str
    time_range: ArchiveTimeRange
    status: Literal["ready", "pending", "missing", "expired", "unavailable"]
    reason: str
    segments: list[ArchiveSegmentRef] = Field(default_factory=list, max_length=16)
    gaps: list[ArchiveTimeRange] = Field(default_factory=list, max_length=17)
    simulated: bool
    source_scan_complete: bool = False
    causal_relation: Literal["unknown"] = "unknown"
    media_copied: Literal[False] = False
    device_write_attempted: Literal[False] = False
    model_inference_attempted: Literal[False] = False


class ArchivePlaybackRequest(ArchiveResolveRequest):
    media_ref: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArchivePlaybackPlan(Contract):
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    camera_id: str
    time_range: ArchiveTimeRange
    source_time_range: ArchiveTimeRange
    stream_url: str = Field(pattern=r"^/api/archive/media/[a-f0-9]{64}$")
    expires_at: AwareDatetime
    start_offset_seconds: float = Field(ge=0)
    end_offset_seconds: float = Field(gt=0)
    previous_keyframe_seconds: float | None = Field(default=None, ge=0)
    indexed_keyframes_seconds: list[float] = Field(default_factory=list, max_length=64)
    duration_seconds: float = Field(gt=0, le=86400)
    audio_track: bool
    codecs: list[str] = Field(max_length=4)
    clock_uncertainty_seconds: float = Field(ge=0, le=60)
    privacy_protected: Literal[True]
    privacy_mode: Literal["local_person_mosaic", "local_full_frame_mosaic"]
    privacy_preview_generated: Literal[True] = True
    privacy_degraded_reason: Literal[
        "none", "detector_unconfigured", "detector_unavailable", "analysis_uncertain"
    ]
    original_modified: Literal[False] = False
    original_copied: Literal[False] = False
    simulated: bool
    range_budget_bytes: Literal[8388608] = 8388608
    session_budget_bytes: Literal[67108864] = 67108864
    decoder_verified_by_server: Literal[False] = False
    hardware_clock_verified: Literal[False] = False
    media_copied: Literal[False] = False
    causal_relation: Literal["unknown"] = "unknown"


class ArchivePlaybackCancel(Contract):
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
