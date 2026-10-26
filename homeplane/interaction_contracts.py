"""Private interaction bindings. Focus is context, never an identity or authority grant."""

import math
from dataclasses import dataclass
from typing import Literal

InputKind = Literal["console_text", "ambient"]


def bounded_ref(value: str, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 256:
        raise ValueError("invalid_interaction_reference")
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid_interaction_reference")


@dataclass(frozen=True)
class InteractionBinding:
    """Injected by an owner; never constructed from a public transcript/People label."""

    input_kind: InputKind
    session_id: str
    room_id: str
    camera_id: str
    map_revision: str
    source_revision: str
    authority_revision: str
    boot_id: str
    generation: int
    subject_ref: str | None = None
    track_ref: str | None = None
    track_epoch: str | None = None
    associated: bool = False
    speaker_count: int = 0
    deadline_mono: float | None = None

    def __post_init__(self):
        if self.input_kind not in ("console_text", "ambient"):
            raise ValueError("invalid_input_kind")
        for value in (
            self.session_id, self.room_id, self.camera_id, self.map_revision,
            self.source_revision, self.authority_revision, self.boot_id,
        ):
            bounded_ref(value)
        for value in (self.subject_ref, self.track_ref, self.track_epoch):
            bounded_ref(value, optional=True)
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("invalid_generation")
        if type(self.associated) is not bool:
            raise ValueError("invalid_speaker_association")
        if type(self.speaker_count) is not int or not 0 <= self.speaker_count <= 30:
            raise ValueError("invalid_speaker_count")
        if self.deadline_mono is not None and not math.isfinite(self.deadline_mono):
            raise ValueError("invalid_interaction_deadline")
        if self.input_kind == "console_text" and any((self.subject_ref, self.track_ref, self.track_epoch)):
            raise ValueError("console_is_not_person_evidence")

    @property
    def focus_eligible(self):
        return self.input_kind == "console_text" or bool(
            self.associated and self.speaker_count == 1 and self.subject_ref
            and self.track_ref and self.track_epoch
        )

    @property
    def slot(self):
        return self.input_kind, self.session_id

    @property
    def key(self):
        return (
            self.input_kind, self.session_id, self.subject_ref, self.track_ref,
            self.track_epoch, self.room_id, self.camera_id, self.map_revision,
            self.source_revision, self.authority_revision, self.boot_id, self.generation,
        )


@dataclass(frozen=True)
class AttentionAdmission:
    """Owner-admitted final command evidence, separate from legacy Evidence booleans.

    A fixture admission only proves the source path. It does not qualify a real
    ASR/visual/active-speaker producer or grant device authority.
    """

    producer_ref: str
    source_revision: str
    speaker_ref: str
    track_ref: str
    track_epoch: str
    room_id: str
    camera_id: str
    captured_at: float
    expires_at: float
    source_kind: Literal["live", "fixture", "tv", "playback", "unknown"] = "unknown"
    final_transcript: bool = False
    addressed_to_system: bool = False
    quality_ok: bool = False
    gaze: Literal["looking", "not_looking", "unavailable"] = "unavailable"
    pointing_admitted: bool = False

    def __post_init__(self):
        for value in (
            self.producer_ref, self.source_revision, self.speaker_ref,
            self.track_ref, self.track_epoch, self.room_id, self.camera_id,
        ):
            bounded_ref(value)
        if not all(math.isfinite(v) for v in (self.captured_at, self.expires_at)):
            raise ValueError("invalid_admission_time")
        if any(type(v) is not bool for v in (
            self.final_transcript, self.addressed_to_system, self.quality_ok, self.pointing_admitted,
        )):
            raise ValueError("invalid_admission_quality")
        if self.expires_at <= self.captured_at:
            raise ValueError("invalid_admission_window")
        if self.gaze not in ("looking", "not_looking", "unavailable"):
            raise ValueError("invalid_gaze_state")
        if self.source_kind not in ("live", "fixture", "tv", "playback", "unknown"):
            raise ValueError("invalid_admission_source")

    def matches(self, binding):
        return bool(
            binding and binding.input_kind == "ambient" and binding.focus_eligible
            and self.source_revision == binding.source_revision
            and self.speaker_ref == binding.subject_ref and self.track_ref == binding.track_ref
            and self.track_epoch == binding.track_epoch and self.room_id == binding.room_id
            and self.camera_id == binding.camera_id
        )
