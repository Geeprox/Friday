"""Consume the recognition owner's exact finite result, never invent model evidence."""

import re
from dataclasses import dataclass

from homeplane.recognition.contracts import Embedding

from .catalog import PeopleError
from .clustering import FaceInput


def face_input(
    result,
    expected_session,
    *,
    embedding: Embedding | None,
    source_digest,
    frame_ref,
    camera_ref,
    face_slot=0,
):
    """The serialized result contains no vector: require a separate trusted handoff.

    The recognition_contracts dependency is supplied by its owner. Import it only
    at this seam so the disabled People page works without consumer adoption.
    This source packet admits synthetic owner fixtures only, never real media.
    """
    try:
        from homeplane.recognition_contracts import RecognitionResult, RecognitionSession
    except ImportError:
        raise PeopleError("people_recognition_dependency_unavailable") from None
    if not isinstance(result, RecognitionResult) or not isinstance(expected_session, RecognitionSession):
        raise PeopleError("people_recognition_contract_required")
    try:
        result.require_session(expected_session)
    except ValueError:
        raise PeopleError("people_recognition_session_mismatch") from None
    observation = result.observation
    if (
        result.session.modality != "face"
        or not result.session.simulated
        or result.status.state not in ("ready", "matched")
        or observation is None
        or not observation.quality.qualified
        or not observation.embedding_produced
    ):
        raise PeopleError("people_face_evidence_unqualified")
    if embedding is None:
        raise PeopleError("people_embedding_handoff_unavailable")
    if (
        not isinstance(embedding, Embedding)
        or not embedding.simulated
        or embedding.modality != "face"
        or embedding.model_sha256 != observation.provenance.embedding_model_sha256
    ):
        raise PeopleError("people_embedding_provenance_mismatch")
    return FaceInput(
        source_digest,
        frame_ref,
        face_slot,
        embedding,
        qualified=True,
        camera_ref=camera_ref,
        observation_ref=observation.observation_id,
        observed_monotonic=observation.timing.observed_monotonic,
    )


@dataclass(frozen=True)
class SpeakerAgreement:
    """Synthetic trusted evidence fixture, not a real active-speaker adapter.

    event_id distinguishes independently admitted utterances; window_id binds
    retries. A real producer needs reviewed diarization/active-speaker evidence.
    RecognitionResult.av_association=unavailable cannot create this object.
    """

    face_sample_id: str
    voice_ref: str
    model_sha256: str
    window_id: str
    event_id: str
    speaker_qualified: bool
    single_speaker: bool
    active_speaker_qualified: bool
    av_alignment_qualified: bool
    overlap_free: bool = False
    track_continuity_qualified: bool = False
    speaker_margin_qualified: bool = False
    simulated: bool = True
    evidence_version: str = "synthetic-active-speaker/1"

    def __post_init__(self):
        if (
            any(
                not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)
                for v in (self.face_sample_id, self.model_sha256)
            )
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", v)
                for v in (self.voice_ref, self.window_id, self.event_id)
            )
            or any(
                type(v) is not bool
                for v in (
                    self.speaker_qualified,
                    self.single_speaker,
                    self.active_speaker_qualified,
                    self.av_alignment_qualified,
                    self.overlap_free,
                    self.track_continuity_qualified,
                    self.speaker_margin_qualified,
                    self.simulated,
                )
            )
            or not self.simulated
            or self.evidence_version != "synthetic-active-speaker/1"
        ):
            raise ValueError("people_invalid_speaker_evidence")

    @property
    def qualified(self):
        return all(
            (
                self.speaker_qualified,
                self.single_speaker,
                self.active_speaker_qualified,
                self.av_alignment_qualified,
                self.overlap_free,
                self.track_continuity_qualified,
                self.speaker_margin_qualified,
                self.simulated,
            )
        )
