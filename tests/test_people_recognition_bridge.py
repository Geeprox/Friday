import importlib.util
from dataclasses import replace

import pytest
from pydantic import ValidationError

from homeplane.people.catalog import PeopleError
from homeplane.people.fixture import MODEL, sample
from homeplane.people.recognition_bridge import SpeakerAgreement, face_input
from homeplane.recognition.contracts import Embedding
from homeplane.recognition_contracts import (
    ObservationQuality,
    ObservationTiming,
    RecognitionObservation,
    RecognitionProvenance,
    RecognitionResult,
    RecognitionSession,
    RecognitionStatus,
    observation_id,
)


def result():
    session = RecognitionSession(
        session_id="12345678-1234-1234-1234-123456789012",
        generation=1,
        owner_operation="fixture_owner",
        camera_id="fixture_camera",
        modality="face",
        deadline_monotonic=50.0,
        simulated=True,
    )
    provenance = RecognitionProvenance(provider="opencv_yunet_sface", embedding_model_sha256=MODEL)
    observation = RecognitionObservation(
        observation_id=observation_id(session),
        modality="face",
        provenance=provenance,
        quality=ObservationQuality(qualified=True, pose_ok=True, sharpness_ok=True, occlusion_ok=True),
        embedding_produced=True,
        timing=ObservationTiming(observed_monotonic=20.0),
    )
    return RecognitionResult(
        session=session,
        status=RecognitionStatus(state="ready", reason="embedding_only", simulated=True),
        observation=observation,
    )


def test_exact_recognition_result_to_ephemeral_input_and_no_ipc_vector():
    report = result()
    fixture = sample(80, 0.1)
    kwargs = dict(
        source_digest=fixture.source_digest, frame_ref=fixture.frame_ref, camera_ref="fixture_camera"
    )
    value = face_input(report, report.session, embedding=fixture.embedding, **kwargs)
    assert value.embedding is fixture.embedding
    assert value.camera_ref == "fixture_camera" and value.track_ref is None
    assert "values" not in report.model_dump_json()
    assert report.status.av_association == "unavailable"
    with pytest.raises(PeopleError, match="handoff_unavailable"):
        face_input(report, report.session, embedding=None, **kwargs)
    with pytest.raises(PeopleError, match="session_mismatch"):
        face_input(
            report, report.session.model_copy(update={"generation": 2}), embedding=fixture.embedding, **kwargs
        )
    with pytest.raises(PeopleError, match="provenance_mismatch"):
        face_input(
            report,
            report.session,
            embedding=Embedding("face", "4" * 64, fixture.embedding.values, True),
            **kwargs,
        )


def test_unqualified_or_real_recognition_not_admitted():
    report = result()
    fixture = sample(81, 0.1)
    report = report.model_copy(
        update={"status": RecognitionStatus(state="unavailable", reason="policy_unqualified", simulated=True)}
    )
    with pytest.raises(PeopleError, match="evidence_unqualified"):
        face_input(
            report,
            report.session,
            embedding=fixture.embedding,
            source_digest=fixture.source_digest,
            frame_ref=fixture.frame_ref,
            camera_ref="fixture_camera",
        )
    with pytest.raises(ValidationError):
        RecognitionResult.model_validate(
            {**result().model_dump(), "embedding": list(fixture.embedding.values)}
        )


@pytest.mark.parametrize(
    "flag",
    [
        "speaker_qualified",
        "single_speaker",
        "active_speaker_qualified",
        "av_alignment_qualified",
        "overlap_free",
        "track_continuity_qualified",
        "speaker_margin_qualified",
    ],
)
def test_voice_requires_all_evidence_no_overlap_drift_or_track_switch(flag):
    evidence = SpeakerAgreement(
        "1" * 64,
        "voice_fixture",
        "2" * 64,
        "window",
        "utterance",
        True,
        True,
        True,
        True,
        overlap_free=True,
        track_continuity_qualified=True,
        speaker_margin_qualified=True,
    )
    assert evidence.qualified
    assert not replace(evidence, **{flag: False}).qualified
    with pytest.raises(ValueError, match="invalid_speaker_evidence"):
        replace(evidence, simulated=False)


def test_real_sklearn_optional_integration_when_already_installed():
    if importlib.util.find_spec("sklearn") is None:
        pytest.skip("scikit-learn is not installed; no dependency install authorized")
    import sqlite3

    from homeplane.people.catalog import PeopleCatalog
    from homeplane.people.fixture import POLICY

    observations = (sample(90, 0), sample(91, 0.05), sample(92, 1.5), sample(93, 1.55))
    p = PeopleCatalog(sqlite3.connect(":memory:")).reindex(observations, POLICY, expected_revision=0)
    assert sorted(len(c.sample_ids) for c in p.clusters) == [2, 2]
    assert p.last_run.backend == "scikit-learn"
