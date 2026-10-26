"""Worker-local, bounded synthetic feature handoff; vectors never leave the worker.

Adapted from the exact People proposal. Four samples across spaced frames fit the
finite worker and 8 KiB budgets. Slots may share a frame; active speaker is absent.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Annotated, Callable, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from homeplane.recognition.contracts import Embedding
from homeplane.recognition_contracts import (
    RecognitionObservation,
    RecognitionProvenance,
    RecognitionSession,
    observation_id,
)

MAX_FEATURE_SAMPLES = 4
MIN_SAMPLE_INTERVAL = .25


def feature_observation_id(session, index, face_slot=0):
    if type(index) is not int or not 0 <= index <= 127 or type(face_slot) is not int or not 0 <= face_slot <= 7:
        raise FeatureHandoffError("feature_batch_budget")
    if index == 0 and face_slot == 0:
        return observation_id(session)
    return str(uuid5(NAMESPACE_URL,
        f"friday-feature-frame/2:{session.session_id}:{session.generation}:frame:{index}:face:{face_slot}"))


ObservationID = Annotated[
    str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
]


class FeatureHandoffError(ValueError):
    pass


class FeatureGroupProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    observation_ids: list[ObservationID] = Field(min_length=1, max_length=256)
    representative_ids: list[ObservationID] = Field(min_length=1, max_length=3)
    state: Literal["candidate", "unknown", "ambiguous"]

    @model_validator(mode="after")
    def membership(self):
        if (
            len(set(self.observation_ids)) != len(self.observation_ids)
            or len(set(self.representative_ids)) != len(self.representative_ids)
            or not set(self.representative_ids) <= set(self.observation_ids)
            or self.state == "candidate"
            and len(self.observation_ids) < 2
        ):
            raise ValueError("invalid_feature_group")
        return self


class FeatureMetadataProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1
    session_id: ObservationID
    generation: int = Field(ge=1)
    base_people_revision: int = Field(ge=0)
    provenance: RecognitionProvenance
    groups: list[FeatureGroupProposal] = Field(max_length=256)
    simulated: Literal[True] = True
    embeddings_exported: Literal[False] = False
    action_authorized: Literal[False] = False

    @field_validator("simulated", "embeddings_exported", "action_authorized", mode="before")
    @classmethod
    def fixed_flags(cls, value, info):
        if value is not (info.field_name == "simulated"):
            raise ValueError("invalid_feature_flags")
        return value


@dataclass(frozen=True)
class PrivateFeature:
    observation: RecognitionObservation = field(repr=False)
    embedding: Embedding = field(repr=False)


class EphemeralFeatureSink:
    """Trusted worker-local callback receiver, no serialization/activation seam.

    observe(session, observation, embedding) is called on the existing serialized
    native thread after qualified embedding production, before tuple disposal.
    finish(operation) calls an owner-supplied clustering adapter once; it admits
    only metadata proposals and then drops owned references. No templates.

    Revocation is immediate for future admission/publication. If a synchronous
    adapter is still running, it owns local feature references until completion;
    the existing parent worker's terminate/kill/reap provides the hard bound.
    Python reference release is not a memory-zeroization or native-stop claim.
    """

    def __init__(
        self,
        session: RecognitionSession,
        provenance: RecognitionProvenance,
        *,
        base_people_revision: int,
        clock=time.monotonic,
        max_samples=MAX_FEATURE_SAMPLES,
    ):
        if (
            not isinstance(session, RecognitionSession)
            or not session.simulated
            or not isinstance(provenance, RecognitionProvenance)
            or session.modality != "face"
            or provenance.provider != "opencv_yunet_sface"
            or type(base_people_revision) is not int
            or base_people_revision < 0
            or type(max_samples) is not int
            or not 1 <= max_samples <= MAX_FEATURE_SAMPLES
        ):
            raise FeatureHandoffError("feature_owner_binding_unqualified")
        self._session, self._provenance = session, provenance
        self._revision, self._clock, self._max = base_people_revision, clock, max_samples
        self._features, self._revoked, self._finishing = {}, False, False
        self._lock = threading.Lock()

    def __repr__(self):
        return "EphemeralFeatureSink(private,worker-local)"

    def require_owner(self, session, provenance):
        if session != self._session or provenance != self._provenance:
            raise FeatureHandoffError("feature_owner_binding_unqualified")

    def metadata_observations(self):
        with self._lock:
            if not self._active():
                raise FeatureHandoffError("feature_scope_revoked")
            return tuple(self._features[key].observation for key in sorted(self._features))

    def revoke(self):
        with self._lock:
            self._revoked = True
            self._features.clear()

    def _active(self):
        return not self._revoked and self._clock() < self._session.deadline_monotonic

    def observe(self, session, observation, embedding):
        with self._lock:
            if not self._active() or self._finishing:
                raise FeatureHandoffError("feature_scope_revoked")
            if (
                session != self._session
                or not isinstance(observation, RecognitionObservation)
                or observation.observation_id != feature_observation_id(session,
                    observation.frame_sequence, observation.face_slot)
                or observation.modality != "face"
                or not observation.simulated
                or observation.provenance != self._provenance
                or not observation.quality.qualified
                or not all(
                    v is True
                    for v in (
                        observation.quality.pose_ok,
                        observation.quality.sharpness_ok,
                        observation.quality.occlusion_ok,
                    )
                )
                or observation.timing.observed_monotonic >= session.deadline_monotonic
                or not observation.embedding_produced
                or not isinstance(embedding, Embedding)
                or embedding.modality != "face"
                or not embedding.simulated
                or embedding.model_sha256 != self._provenance.embedding_model_sha256
            ):
                raise FeatureHandoffError("feature_provenance_mismatch")
            try:
                RecognitionObservation.model_validate(observation.model_dump())
            except ValueError:
                raise FeatureHandoffError("feature_provenance_mismatch") from None
            previous = self._features.get(observation.observation_id)
            if previous is not None:
                if previous.observation != observation or previous.embedding != embedding:
                    self._features.clear()
                    self._revoked = True
                    raise FeatureHandoffError("feature_retry_conflict")
                return
            if len(self._features) >= self._max:
                raise FeatureHandoffError("feature_batch_budget")
            same_frame = [f.observation for f in self._features.values()
                          if f.observation.frame_sequence == observation.frame_sequence]
            if any(o.timing != observation.timing for o in same_frame):
                raise FeatureHandoffError("feature_frame_timing_mismatch")
            earlier = [f.observation for f in self._features.values()
                       if f.observation.frame_sequence != observation.frame_sequence]
            if any(abs(o.timing.observed_monotonic-observation.timing.observed_monotonic) < MIN_SAMPLE_INTERVAL
                   for o in earlier):
                raise FeatureHandoffError("feature_sampling_interval")
            if observation.track_id is not None and sum(
                    f.observation.track_id == observation.track_id for f in self._features.values()) >= 3:
                raise FeatureHandoffError("feature_track_sample_budget")
            self._features[observation.observation_id] = PrivateFeature(observation, embedding)

    def finish(self, operation: Callable[[tuple[PrivateFeature, ...]], list[FeatureGroupProposal]]):
        with self._lock:
            if not self._active() or self._finishing:
                raise FeatureHandoffError("feature_scope_revoked")
            self._finishing = True
            features = tuple(self._features[key] for key in sorted(self._features))
            self._features.clear()
        try:
            groups = operation(features)
            proposal = FeatureMetadataProposal(
                session_id=self._session.session_id,
                generation=self._session.generation,
                base_people_revision=self._revision,
                provenance=self._provenance,
                groups=groups,
            )
            ids = [sid for group in proposal.groups for sid in group.observation_ids]
            expected = {feature.observation.observation_id for feature in features}
            if len(ids) != len(set(ids)) or set(ids) != expected:
                raise FeatureHandoffError("feature_metadata_membership_mismatch")
            with self._lock:
                if not self._active():
                    raise FeatureHandoffError("feature_scope_revoked")
            return proposal
        except FeatureHandoffError:
            raise
        except Exception:
            # Validation errors and provider text must not echo vectors.
            raise FeatureHandoffError("feature_operation_failed") from None
        finally:
            self.revoke()
            features = ()
