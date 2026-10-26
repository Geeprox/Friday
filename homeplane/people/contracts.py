"""Finite public people contracts; vectors and activation have no HTTP schema."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Ref = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal[1] = 1


class Sample(Contract):
    sample_id: Digest
    source_digest: Digest
    frame_ref: Ref
    camera_ref: Ref
    track_ref: Ref | None = None
    observation_ref: Ref | None = None
    observed_monotonic: float | None = Field(default=None, ge=0)
    source_clock_domain: Literal["unverified"] = "unverified"
    face_slot: int = Field(ge=0, le=7)
    model_sha256: Digest
    implementation_version: Literal["friday-recognition/1"] = "friday-recognition/1"
    state: Literal["candidate", "unknown", "ambiguous", "excluded"]
    active: bool
    simulated: Literal[True] = True


class Person(Contract):
    person_id: Ref
    name: str | None = Field(default=None, max_length=40)
    merged_into: Ref | None = None
    sample_count: int = Field(ge=0, le=512)
    security_authentication: Literal[False] = False
    action_authorized: Literal[False] = False


class Cluster(Contract):
    cluster_id: Digest
    person_id: Ref | None = None
    sample_ids: list[Digest] = Field(min_length=1, max_length=256)
    representatives: list[Digest] = Field(min_length=1, max_length=3)
    state: Literal["candidate", "named", "unknown", "ambiguous"]


class VoiceLink(Contract):
    link_id: Digest
    person_id: Ref
    voice_ref: Ref
    state: Literal["candidate", "confirmed", "rejected", "unlinked", "stale"]
    agreement_count: int = Field(ge=0, le=32)
    independent_windows: int = Field(ge=0, le=32)
    evidence_version: Literal["synthetic-active-speaker/1"] = "synthetic-active-speaker/1"
    simulated: Literal[True] = True


class Run(Contract):
    algorithm: Literal["sklearn-complete-link/1"] = "sklearn-complete-link/1"
    backend: Literal["scikit-learn", "synthetic-contract-fake"]
    model_sha256: Digest
    threshold: float = Field(ge=-1, le=1)
    ambiguity_margin: float = Field(gt=0, le=2)
    policy_reference: Ref
    input_count: int = Field(ge=0, le=256)
    simulated: Literal[True] = True


class PeoplePage(Contract):
    revision: int = Field(ge=0)
    people: list[Person] = Field(max_length=128)
    clusters: list[Cluster] = Field(max_length=256)
    samples: list[Sample] = Field(max_length=512)
    voice_links: list[VoiceLink] = Field(max_length=256)
    last_run: Run | None = None
    undo_available: bool
    active_speaker_dependency: Literal["unavailable"] = "unavailable"
    tracking_dependency: Literal["unavailable"] = "unavailable"
    recognition_state: Literal["disabled"] = "disabled"
    simulated: Literal[True] = True
    original_media_modified: Literal[False] = False
    embeddings_persisted: Literal[False] = False
    security_authentication: Literal[False] = False
    action_authorized: Literal[False] = False


class Edit(Contract):
    expected_revision: int = Field(ge=0)


class Name(Edit):
    cluster_id: Digest
    name: str = Field(min_length=1, max_length=40)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid_person_name")
        return value


class Merge(Edit):
    cluster_ids: list[Digest] = Field(min_length=2, max_length=8)
    target_person_id: Ref | None = None


class Split(Edit):
    cluster_id: Digest
    sample_ids: list[Digest] = Field(min_length=1, max_length=128)


class Correct(Edit):
    sample_id: Digest
    person_id: Ref | None = None
    operation: Literal["move", "separate", "exclude", "restore"]


class ReviewVoice(Edit):
    link_id: Digest
    decision: Literal["confirm", "reject", "unlink"]
