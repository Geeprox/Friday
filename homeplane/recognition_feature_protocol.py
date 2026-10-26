"""Private v2 result/feature messages; four features share the existing 8 KiB bound."""
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .recognition_contracts import (
    FaceFrameProvenance,
    NativeFaceResult,
    RecognitionObservation,
    RecognitionResult,
    RecognitionSession,
)
from .recognition_feature_handoff import (
    MAX_FEATURE_SAMPLES,
    MIN_SAMPLE_INTERVAL,
    FeatureMetadataProposal,
    feature_observation_id,
)

MAX_WORKER_OUTPUT = 8192


class FeatureMetadataMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    message_kind: Literal["feature_metadata"] = "feature_metadata"
    schema_version: Literal[2]
    session: RecognitionSession
    metadata: FeatureMetadataProposal
    observations: list[RecognitionObservation] = Field(min_length=1, max_length=MAX_FEATURE_SAMPLES)

    @model_validator(mode="after")
    def exact_batch(self):
        metadata = self.metadata
        ids = [o.observation_id for o in self.observations]
        grouped = [i for g in metadata.groups for i in g.observation_ids]
        if (not self.session.simulated or self.session.modality != "face"
                or metadata.session_id != self.session.session_id or metadata.generation != self.session.generation
                or metadata.provenance.provider != "opencv_yunet_sface"
                or len(ids) != len(set(ids))
                or len(grouped) != len(set(grouped)) or set(ids) != set(grouped)):
            raise ValueError("invalid_feature_batch_binding")
        for observation in self.observations:
            q = observation.quality
            if (observation.observation_id != feature_observation_id(self.session,
                    observation.frame_sequence, observation.face_slot)
                    or observation.provenance != metadata.provenance or observation.modality != "face"
                    or observation.simulated is not True or not observation.embedding_produced
                    or not q.qualified or not all(v is True for v in (q.pose_ok, q.sharpness_ok, q.occlusion_ok))
                    or any(v is not None for v in (q.speech_present, q.single_speaker, q.acoustic_quality_ok))
                    or observation.timing.observed_monotonic >= self.session.deadline_monotonic):
                raise ValueError("invalid_feature_observation")
            RecognitionObservation.model_validate(observation.model_dump())
        frames, tracks = {}, {}
        for observation in self.observations:
            seq = observation.frame_sequence
            if seq in frames and frames[seq] != observation.timing:
                raise ValueError("feature_frame_timing_mismatch")
            frames[seq] = observation.timing
            if observation.track_id is not None:
                tracks[observation.track_id] = tracks.get(observation.track_id, 0) + 1
        if any(count > 3 for count in tracks.values()):
            raise ValueError("feature_track_sample_budget")
        times = [frames[key].observed_monotonic for key in sorted(frames)]
        if max(times) - min(times) > self.session.seconds:
            raise ValueError("feature_sampling_window")
        if any(later - earlier < MIN_SAMPLE_INTERVAL for earlier, later in zip(times, times[1:])):
            raise ValueError("feature_sampling_interval")
        return self


@dataclass(frozen=True)
class RecognitionWorkerResult:
    result: RecognitionResult | NativeFaceResult
    features: FeatureMetadataMessage | None = None


def encode_worker_output(result):
    if isinstance(result, RecognitionWorkerResult):
        if isinstance(result.result, NativeFaceResult) and result.features is not None:
            raise ValueError("native_people_features_unavailable")
        if isinstance(result.result, NativeFaceResult):
            NativeFaceResult.model_validate_json(result.result.model_dump_json())
        output = result.result.model_dump_json()
        if result.features is not None:
            output += "\n" + result.features.model_dump_json()
    else:
        if isinstance(result, NativeFaceResult):
            NativeFaceResult.model_validate_json(result.model_dump_json())
        output = result.model_dump_json()
    if len(output.encode()) > MAX_WORKER_OUTPUT:
        raise ValueError("worker_protocol_budget")
    return output


def parse_worker_output(output, session, provenance, *, allow_features=False):
    if len(output) > MAX_WORKER_OUTPUT:
        raise ValueError("worker_protocol_budget")
    parts = output.split(b"\n")
    if len(parts) not in (1, 2) or (len(parts) == 2 and not allow_features):
        raise ValueError("unexpected_feature_message")
    header = json.loads(parts[0])
    if isinstance(provenance, FaceFrameProvenance):
        if (len(parts) != 1 or not isinstance(header, dict)
                or type(header.get("schema_version")) is not int or header["schema_version"] != 3):
            raise ValueError("private_native_version_unavailable")
        result = NativeFaceResult.model_validate_json(parts[0]).require_session(session)
        if result.provenance != provenance:
            raise ValueError("recognition_model_binding")
        return RecognitionWorkerResult(result)
    if not isinstance(header, dict) or type(header.get("schema_version")) is not int or header["schema_version"] != 2:
        raise ValueError("private_recognition_version_unavailable")
    result = RecognitionResult.model_validate_json(parts[0]).require_session(session)
    if result.observation is not None and result.observation.provenance != provenance:
        raise ValueError("recognition_model_binding")
    features = FeatureMetadataMessage.model_validate_json(parts[1]) if len(parts) == 2 else None
    if features is not None and (features.session != session or features.metadata.provenance != provenance):
        raise ValueError("feature_owner_binding")
    return RecognitionWorkerResult(result, features)
