"""Private finite recognition IPC; public reports expose fixed codes only."""
import math
import sys
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RecognitionSession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    operation: Literal["recognition"] = "recognition"
    session_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    generation: int = Field(ge=1)
    owner_operation: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    camera_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f]+$")
    channel: Literal[0] = 0
    modality: Literal["face", "speaker"]
    deadline_monotonic: float = Field(gt=0, allow_inf_nan=False)
    seconds: float = Field(default=2.0, ge=2, le=8, allow_inf_nan=False)
    simulated: bool

    @field_validator("channel", mode="before")
    @classmethod
    def fixed_channel(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError("invalid_recognition_channel")
        return value


Reason = Literal[
    "default_off", "owner_admission_unavailable", "owner_binding_mismatch", "running",
    "provider_unavailable", "quality_unqualified", "invalid_video", "invalid_audio",
    "audio_window_overflow", "short_audio", "no_input", "expired", "stopped",
    "configuration_changed", "stale_generation", "model_mismatch", "simulation_mismatch",
    "policy_unqualified", "candidate_unqualified", "below_threshold", "ambiguous",
    "similarity_evidence_only", "embedding_only", "native_failed", "worker_timeout",
    "worker_memory_limit", "worker_failed", "native_cleanup_uncertain",
]


class RecognitionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    state: Literal["disabled", "unavailable", "running", "ready", "unknown", "ambiguous", "matched", "expired"] = "disabled"
    reason: Reason = "default_off"
    simulated: bool = False
    security_authentication: Literal[False] = False
    action_authorized: Literal[False] = False
    av_association: Literal["unavailable"] = "unavailable"

    @field_validator("security_authentication", "action_authorized", mode="before")
    @classmethod
    def no_authority(cls, value):
        if value is not False:
            raise ValueError("identity_is_not_authority")
        return value

    @model_validator(mode="after")
    def fixed_meaning(self):
        allowed = {
            "disabled": {"default_off"}, "running": {"running"}, "expired": {"expired"},
            "ready": {"embedding_only"}, "matched": {"similarity_evidence_only"}, "ambiguous": {"ambiguous"},
            "unknown": {"quality_unqualified", "invalid_video", "invalid_audio", "audio_window_overflow",
                "short_audio", "no_input", "model_mismatch", "simulation_mismatch", "candidate_unqualified",
                "below_threshold", "provider_unavailable"},
            "unavailable": {"owner_admission_unavailable", "owner_binding_mismatch", "provider_unavailable",
                "configuration_changed", "stale_generation", "policy_unqualified", "native_failed",
                "worker_timeout", "worker_memory_limit", "worker_failed", "native_cleanup_uncertain", "stopped"},
        }
        if self.reason not in allowed[self.state]:
            raise ValueError("invalid_identity_evidence")
        return self


class RecognitionProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: Literal["opencv_yunet_sface", "sherpa_wespeaker"]
    embedding_model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    detector_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    implementation_version: Literal["friday-recognition/1"] = "friday-recognition/1"
    runtime_version: Literal["synthetic-api-fake"] = "synthetic-api-fake"


class ObservationQuality(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    evidence: Literal["synthetic_owner_fixture"] = "synthetic_owner_fixture"
    qualified: bool
    pose_ok: bool | None = None
    sharpness_ok: bool | None = None
    occlusion_ok: bool | None = None
    speech_present: bool | None = None
    single_speaker: bool | None = None
    acoustic_quality_ok: bool | None = None


class ObservationTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    observed_monotonic: float = Field(ge=0, allow_inf_nan=False)
    source_timestamp: int | None = Field(default=None, ge=0, le=2**64-1)
    source_clock_domain: Literal["unverified"] = "unverified"
    observation_clock: Literal["worker_monotonic"] = "worker_monotonic"
    capture_monotonic: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    capture_clock_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    av_sync: Literal["unavailable"] = "unavailable"
    capture_evidence: Literal["unavailable", "synthetic_owner_fixture"] = "unavailable"

    @model_validator(mode="after")
    def capture_pair(self):
        if (self.capture_monotonic is None) != (self.capture_clock_id is None):
            raise ValueError("capture_clock_binding_required")
        if (self.capture_monotonic is not None) != (self.capture_evidence == "synthetic_owner_fixture"):
            raise ValueError("capture_provenance_required")
        return self


class RecognitionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    observation_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    modality: Literal["face", "speaker"]
    frame_sequence: int = Field(default=0, ge=0, le=127)
    face_slot: int = Field(default=0, ge=0, le=7)
    provenance: RecognitionProvenance
    quality: ObservationQuality
    timing: ObservationTiming
    track_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    tracking: Literal["unavailable", "unknown", "tentative", "confirmed"] = "unavailable"
    embedding_produced: bool = False
    simulated: Literal[True] = True
    identity_claim: Literal["none"] = "none"
    person_association: Literal["unavailable"] = "unavailable"

    @field_validator("simulated", mode="before")
    @classmethod
    def synthetic_only(cls, value):
        if value is not True:
            raise ValueError("native_observation_not_qualified")
        return value

    @model_validator(mode="after")
    def tracking_binding(self):
        tracked = self.tracking in {"tentative", "confirmed"}
        if (tracked != (self.track_id is not None)
                or self.modality == "speaker" and (self.frame_sequence != 0 or self.face_slot != 0)
                or self.tracking != "unavailable" and (
                    self.modality != "face" or self.timing.capture_monotonic is None)
                or tracked and (not self.quality.qualified or not all(v is True for v in (
                    self.quality.pose_ok, self.quality.sharpness_ok, self.quality.occlusion_ok)))):
            raise ValueError("invalid_tracking_binding")
        return self


def observation_id(session):
    # Stable for retries/serialization of this one admitted window, opaque to UI.
    return str(uuid5(NAMESPACE_URL, f"friday-recognition/1:{session.session_id}:{session.generation}:{session.modality}:0"))


class RecognitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[2] = 2
    session: RecognitionSession
    status: RecognitionStatus
    observation: RecognitionObservation | None = None
    media_persisted: Literal[False] = False

    @field_validator("media_persisted", mode="before")
    @classmethod
    def no_media(cls, value):
        if value is not False:
            raise ValueError("no_media_export")
        return value

    @model_validator(mode="after")
    def simulation_binding(self):
        if self.session.simulated != self.status.simulated or self.status.state == "running":
            raise ValueError("invalid_result_binding")
        observation = self.observation
        if observation is not None:
            observation = RecognitionObservation.model_validate(observation.model_dump())
            q = observation.quality
            face = self.session.modality == "face"
            qualified = all(v is True for v in ((q.pose_ok, q.sharpness_ok, q.occlusion_ok) if face else
                (q.speech_present, q.single_speaker, q.acoustic_quality_ok)))
            irrelevant = (q.speech_present, q.single_speaker, q.acoustic_quality_ok) if face else (
                q.pose_ok, q.sharpness_ok, q.occlusion_ok)
            if (observation.observation_id != observation_id(self.session)
                    or observation.modality != self.session.modality or not self.session.simulated
                    or observation.timing.observed_monotonic >= self.session.deadline_monotonic
                    or observation.provenance.provider != ("opencv_yunet_sface" if face else "sherpa_wespeaker")
                    or q.qualified != qualified or any(v is not None for v in irrelevant)
                    or (self.status.state in {"ready", "matched"} and not (qualified and observation.embedding_produced))):
                raise ValueError("invalid_observation_binding")
        elif self.status.state in {"ready", "matched"}:
            raise ValueError("missing_qualified_observation")
        return self

    def require_session(self, expected: RecognitionSession):
        if self.session != expected:
            raise ValueError("recognition_session_mismatch")
        return self


class RecognitionOffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: Literal[False]

    @field_validator("enabled", mode="before")
    @classmethod
    def only_off(cls, value):
        if value is not False:
            raise ValueError("recognition_activation_unavailable")
        return value


class FaceFrameProvenance(BaseModel):
    """V3 evidence types do not relabel native metadata as a v2 fixture."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: Literal["opencv_yunet_sface"] = "opencv_yunet_sface"
    detector_model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    embedding_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence: Literal["native_unverified", "native_verified", "synthetic_contract_fixture"]
    artifact_evidence: Literal["configured_only", "verified_local_artifacts", "synthetic_contract_fixture"]
    runtime_version: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")

    @model_validator(mode="after")
    def exact_evidence(self):
        if (self.evidence == "native_unverified" and (
                self.artifact_evidence != "configured_only" or self.runtime_version is not None)
                or self.evidence == "native_verified" and (
                    self.artifact_evidence != "verified_local_artifacts" or self.runtime_version is None
                    or self.runtime_version in {"synthetic-api-fake", "synthetic_contract_fixture"})
                or self.evidence == "synthetic_contract_fixture" and (
                    self.artifact_evidence != "synthetic_contract_fixture"
                    or self.runtime_version != "synthetic_contract_fixture")):
            raise ValueError("invalid_native_provenance")
        return self


class FaceSlotQuality(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    evidence: Literal["unavailable", "native_measured", "synthetic_contract_fixture"] = "unavailable"
    evidence_reference: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    qualified: bool = False
    pose_ok: bool | None = None
    sharpness_ok: bool | None = None
    occlusion_ok: bool | None = None

    @model_validator(mode="after")
    def measured_quality(self):
        flags = self.pose_ok, self.sharpness_ok, self.occlusion_ok
        if (self.qualified != all(v is True for v in flags)
                or self.evidence == "unavailable" and (
                    self.evidence_reference is not None or any(v is not None for v in flags))
                or self.evidence != "unavailable" and self.evidence_reference is None):
            raise ValueError("invalid_slot_quality")
        return self


class ReceivedFrameTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    received_monotonic: float = Field(ge=0, allow_inf_nan=False)
    received_at: Literal["decoded_callback_entry"] = "decoded_callback_entry"
    received_clock_domain: Literal["worker_monotonic"] = "worker_monotonic"
    observation_monotonic: float = Field(ge=0, allow_inf_nan=False)
    source_timestamp: int | None = Field(default=None, ge=0, le=2**64-1)
    source_clock_domain: Literal["unverified"] = "unverified"
    source_discontinuity: bool = False
    capture_monotonic: None = None
    capture_clock_id: None = None
    capture_evidence: Literal["unavailable"] = "unavailable"
    av_sync: Literal["unavailable"] = "unavailable"

    @model_validator(mode="after")
    def receive_order(self):
        if (self.observation_monotonic < self.received_monotonic
                or self.observation_monotonic-self.received_monotonic > .25):
            raise ValueError("received_frame_stale")
        return self


class FaceSlotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    observation_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    face_slot: int = Field(ge=0, le=7)
    box: tuple[float, float, float, float]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    quality: FaceSlotQuality
    embedding_state: Literal["produced", "unavailable", "quality_unqualified", "budget_exhausted",
        "duplicate", "frame_mismatch", "model_mismatch", "simulation_mismatch", "provider_unavailable"]
    track_id: None = None
    tracking: Literal["unavailable"] = "unavailable"
    identity_claim: Literal["none"] = "none"
    person_association: Literal["unavailable"] = "unavailable"

    @model_validator(mode="after")
    def normalized_geometry(self):
        if (any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.box)
                or self.box[2]*self.box[3] < sys.float_info.min
                or self.box[0]+self.box[2] > 1 or self.box[1]+self.box[3] > 1
                or self.embedding_state == "produced" and not self.quality.qualified):
            raise ValueError("invalid_native_slot")
        return self


class FaceFrameMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    frame_sequence: int = Field(ge=0, le=127)
    frame_ref: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    width: int = Field(ge=1, le=1280)
    height: int = Field(ge=1, le=720)
    timing: ReceivedFrameTiming
    slots: list[FaceSlotMetadata] = Field(max_length=8)


class NativeFaceResult(BaseModel):
    """Same private worker channel, new explicit version; no vectors/People grant."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[3] = 3
    message_kind: Literal["face_frame_metadata"] = "face_frame_metadata"
    session: RecognitionSession
    status: RecognitionStatus
    provenance: FaceFrameProvenance
    admission_kind: Literal["unavailable", "synthetic_contract_fixture", "native_source_metadata"] = "unavailable"
    window_started_monotonic: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    frame: FaceFrameMetadata | None = None
    production_admission: Literal["unavailable"] = "unavailable"
    media_persisted: Literal[False] = False
    embeddings_exported: Literal[False] = False
    face_to_voice: Literal["unavailable"] = "unavailable"

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 3:
            raise ValueError("private_native_version_unavailable")
        return value

    @field_validator("media_persisted", "embeddings_exported", mode="before")
    @classmethod
    def no_export(cls, value):
        if value is not False:
            raise ValueError("native_export_unavailable")
        return value

    @model_validator(mode="after")
    def exact_frame(self):
        fixture = self.provenance.evidence == "synthetic_contract_fixture"
        if (self.session.modality != "face" or self.session.simulated != fixture
                or self.status.simulated != fixture or self.status.state in {"running", "matched", "ambiguous"}
                or self.admission_kind == "synthetic_contract_fixture" and not fixture
                or self.admission_kind == "native_source_metadata" and fixture
                or self.admission_kind == "unavailable" and self.frame is not None):
            raise ValueError("invalid_native_result_binding")
        if self.frame is not None:
            from .recognition_feature_handoff import feature_observation_id
            frame = FaceFrameMetadata.model_validate_json(self.frame.model_dump_json())
            start = self.window_started_monotonic
            if (start is None or not start <= frame.timing.received_monotonic
                    <= frame.timing.observation_monotonic < min(start+self.session.seconds,
                        self.session.deadline_monotonic)
                    or frame.frame_ref != feature_observation_id(self.session, frame.frame_sequence, 0)
                    or [s.face_slot for s in frame.slots] != list(range(len(frame.slots)))):
                raise ValueError("invalid_native_frame_binding")
            for slot in frame.slots:
                if (slot.observation_id != feature_observation_id(self.session, frame.frame_sequence, slot.face_slot)
                        or slot.quality.evidence not in ("unavailable",
                            "synthetic_contract_fixture" if fixture else "native_measured")
                        or slot.embedding_state == "produced" and (
                            self.provenance.embedding_model_sha256 is None
                            or self.provenance.evidence == "native_unverified")):
                    raise ValueError("invalid_native_slot_binding")
            produced = any(s.embedding_state == "produced" for s in frame.slots)
            if self.status.state == "ready" and not produced:
                raise ValueError("native_embedding_missing")
        elif self.status.state == "ready":
            raise ValueError("native_frame_missing")
        return self

    def require_session(self, expected):
        if self.session != expected:
            raise ValueError("recognition_session_mismatch")
        return self


class NativeFaceRequest(BaseModel):
    """Versioned request metadata cannot manufacture a provider or admission."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[3]
    message_kind: Literal["face_frame_request"] = "face_frame_request"
    session: RecognitionSession
    provenance: FaceFrameProvenance

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 3:
            raise ValueError("private_native_version_unavailable")
        return value

    @model_validator(mode="after")
    def request_binding(self):
        if (self.session.modality != "face" or self.session.simulated != (
                self.provenance.evidence == "synthetic_contract_fixture")):
            raise ValueError("invalid_native_request_binding")
        return self

    def require_session(self, expected):
        if self.session != expected:
            raise ValueError("recognition_session_mismatch")
        return self
