"""Media input verification is not perception, synchronization or control."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .settings import SettingsConfig


class MediaProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seconds: float = Field(default=6.0, ge=1, le=10, allow_inf_nan=False)


class MediaConnection(MediaProbeRequest):
    origin: str = Field(max_length=512)
    camera_id: str = Field(min_length=1, max_length=128)
    token: SecretStr = Field(min_length=1, max_length=8192)

    @field_validator("origin")
    @classmethod
    def local_origin(cls, value):
        return SettingsConfig(camera_service_url=value).camera_service_url

    @field_validator("camera_id")
    @classmethod
    def camera_identifier(cls, value):
        return SettingsConfig(camera_id=value).camera_id


class NativeMediaConnection(MediaProbeRequest):
    """Private worker input. Paths and credential storage are fixed by the parent."""
    camera_id: str = Field(min_length=1, max_length=128)

    @field_validator("camera_id")
    @classmethod
    def camera_identifier(cls, value):
        return SettingsConfig(camera_id=value).camera_id


MediaFailure = Literal["service_login_required", "account_login_required", "service_unavailable",
    "protocol_error", "unsupported_codec", "codec_changed", "missing_init", "malformed_packet",
    "decode_failed", "stream_closed", "stream_error", "no_decoded_frames", "input_budget",
    "worker_unavailable", "worker_failed", "worker_timeout", "worker_memory_limit",
    "sdk_not_qualified", "account_token_expired", "camera_not_found", "camera_offline",
    "camera_pin_required", "service_conflict", "stream_idle"]


class MediaStreamResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["not_requested", "decoded", "failed"] = "not_requested"
    codec: Literal["h264", "opus", "g711a", "g711u"] | None = None
    packets: int = Field(default=0, ge=0, le=4096)
    encoded_bytes: int = Field(default=0, ge=0, le=12 * 1024 * 1024)
    decoded_frames: int = Field(default=0, ge=0, le=4096)
    width: int | None = Field(default=None, ge=1, le=4096)
    height: int | None = Field(default=None, ge=1, le=4096)
    sample_rate: int | None = Field(default=None, ge=8000, le=48000, description="Decoded source rate before mono16k resampling; null if the SDK hides it")
    channels: int | None = Field(default=None, ge=1, le=2, description="Decoded source channels before resampling; null if unavailable")
    pcm_format: Literal["mono_s16_16000"] | None = None
    pcm_16k_samples: int = Field(default=0, ge=0, le=320000)
    first_decode_ms: float | None = Field(default=None, ge=0, le=20000, allow_inf_nan=False)
    last_decode_ms: float | None = Field(default=None, ge=0, le=20000, allow_inf_nan=False)
    video_timestamps_seen: int = Field(default=0, ge=0, le=4096)
    video_clock_discontinuities: int = Field(default=0, ge=0, le=4096)
    source_timestamps_seen: int = Field(default=0, ge=0, le=4096)
    source_clock_discontinuities: int = Field(default=0, ge=0, le=4096)
    failure: MediaFailure | None = None


class MediaProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    status: Literal["not_requested", "not_configured", "running", "decoded_sync_unknown", "failed",
                    "stopped", "discarded_configuration_changed", "expired"] = "not_requested"
    probe_id: str | None = Field(default=None, max_length=36)
    checked_at: str | None = Field(default=None, max_length=40)
    valid_until: str | None = Field(default=None, max_length=40)
    simulated: bool = False
    provider: Literal["miloco_service", "miot_sdk"] = "miloco_service"
    video: MediaStreamResult = Field(default_factory=MediaStreamResult)
    audio: MediaStreamResult = Field(default_factory=MediaStreamResult)
    failure: MediaFailure | None = None
    failure_phase: Literal["sdk_preflight", "login", "inventory", "connect", "sampling", "cleanup", "worker"] | None = None
    native_start_status: Literal["unobserved", "pending", "succeeded", "failed"] = "unobserved"
    native_start_error_code: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1,
        description="Opaque miot_camera_start return value; no vendor meaning is inferred")
    source_av_alignment: Literal["unavailable"] = "unavailable"
    alignment_error_bound_ms: None = None
    alignment_reason: Literal["audio_source_timestamp_absent_video_clock_domain_unverified", "sdk_source_timestamps_clock_domain_unverified"] = "audio_source_timestamp_absent_video_clock_domain_unverified"
    reception_overlap_ms: float | None = Field(default=None, ge=0, le=20000, allow_inf_nan=False)
    reception_clock: Literal["worker_monotonic_not_capture_time"] = "worker_monotonic_not_capture_time"
    worker_peak_rss_bytes: int | None = Field(default=None, ge=0)
    media_persisted: Literal[False] = False
    model_invoked: Literal[False] = False
    device_write: Literal[False] = False
    nas_recording_impact: Literal["not_independently_verified"] = "not_independently_verified"

    @field_validator("checked_at", "valid_until")
    @classmethod
    def aware_time(cls, value):
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("media_time_requires_zone")
        return value

    @model_validator(mode="after")
    def actual_decodes_required(self):
        if self.provider == "miot_sdk":
            self.alignment_reason = "sdk_source_timestamps_clock_domain_unverified"
        if self.native_start_error_code is not None and (
            self.provider != "miot_sdk" or self.native_start_status != "failed" or self.native_start_error_code == 0
        ):
            raise ValueError("native_start_error_requires_failed_sdk_start")
        if self.status == "decoded_sync_unknown" and self.native_start_status in {"pending", "failed"}:
            raise ValueError("native_start_must_finish_successfully")
        if self.status == "decoded_sync_unknown" and not (
            self.video.status == self.audio.status == "decoded" and self.video.decoded_frames > 0
            and self.audio.decoded_frames > 0 and self.audio.pcm_16k_samples > 0):
            raise ValueError("decoded_samples_required")
        return self
