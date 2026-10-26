import importlib.util
import sys
import threading
from pathlib import Path

import pytest

from homeplane.people.fixture import sample
from homeplane.recognition_contracts import (
    ObservationQuality,
    ObservationTiming,
    RecognitionObservation,
    RecognitionProvenance,
    RecognitionSession,
    observation_id,
)

path = Path(__file__).resolve().parents[1] / "homeplane/recognition_feature_handoff.py"
spec = importlib.util.spec_from_file_location("feature_handoff_proposal", path)
proposal = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proposal
spec.loader.exec_module(proposal)


def fixture():
    session = RecognitionSession(
        session_id="12345678-1234-1234-1234-123456789012",
        generation=1,
        owner_operation="owner_fixture",
        camera_id="synthetic_camera",
        modality="face",
        deadline_monotonic=8.0,
        simulated=True,
    )
    provenance = RecognitionProvenance(provider="opencv_yunet_sface", embedding_model_sha256="1" * 64)
    observation = RecognitionObservation(
        observation_id=observation_id(session),
        modality="face",
        provenance=provenance,
        quality=ObservationQuality(qualified=True, pose_ok=True, sharpness_ok=True, occlusion_ok=True),
        timing=ObservationTiming(observed_monotonic=1.0),
        embedding_produced=True,
    )
    sink = proposal.EphemeralFeatureSink(session, provenance, base_people_revision=7, clock=lambda: 2.0)
    return sink, session, observation, sample(100, 0.1).embedding


def metadata(features):
    ids = [f.observation.observation_id for f in features]
    return [proposal.FeatureGroupProposal(observation_ids=ids, representative_ids=ids, state="unknown")]


def test_feature_lifetime_metadata_only_and_retry_deduplication():
    sink, session, observation, embedding = fixture()
    sink.observe(session, observation, embedding)
    sink.observe(session, observation, embedding)
    output = sink.finish(metadata)
    assert output.base_people_revision == 7 and len(output.groups[0].observation_ids) == 1
    assert "values" not in output.model_dump_json() and "embedding=" not in repr(sink)
    with pytest.raises(proposal.FeatureHandoffError, match="revoked"):
        sink.observe(session, observation, embedding)


def test_feature_wrong_session_model_quality_and_retry_conflict():
    sink, session, observation, embedding = fixture()
    with pytest.raises(proposal.FeatureHandoffError, match="provenance_mismatch"):
        sink.observe(session.model_copy(update={"generation": 2}), observation, embedding)
    with pytest.raises(proposal.FeatureHandoffError, match="provenance_mismatch"):
        sink.observe(session, observation, sample(101, 0.1, model="3" * 64).embedding)
    with pytest.raises(proposal.FeatureHandoffError, match="provenance_mismatch"):
        sink.observe(
            session,
            observation.model_copy(update={"quality": ObservationQuality(qualified=False)}),
            embedding,
        )
    sink.observe(session, observation, embedding)
    with pytest.raises(proposal.FeatureHandoffError, match="retry_conflict"):
        sink.observe(session, observation, sample(102, 0.2).embedding)


def test_revocation_during_adapter_drops_late_output():
    sink, session, observation, embedding = fixture()
    sink.observe(session, observation, embedding)
    started, release = threading.Event(), threading.Event()
    errors = []

    def blocked(features):
        started.set()
        assert release.wait(2)
        return metadata(features)

    def run():
        try:
            sink.finish(blocked)
        except proposal.FeatureHandoffError as error:
            errors.append(str(error))

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(2)
    sink.revoke()
    release.set()
    worker.join(2)
    assert not worker.is_alive() and errors == ["feature_scope_revoked"]


def test_callback_cannot_export_extra_fields_or_forge_current_singleton_cluster():
    sink, session, observation, embedding = fixture()
    sink.observe(session, observation, embedding)
    with pytest.raises(proposal.FeatureHandoffError, match="operation_failed"):
        sink.finish(
            lambda features: [
                {
                    "observation_ids": [observation.observation_id],
                    "representative_ids": [observation.observation_id],
                    "state": "unknown",
                    "embedding": embedding.values,
                }
            ]
        )
    sink, session, observation, embedding = fixture()
    sink.observe(session, observation, embedding)
    with pytest.raises(proposal.FeatureHandoffError, match="operation_failed"):
        sink.finish(
            lambda features: [
                {
                    "observation_ids": [observation.observation_id],
                    "representative_ids": [observation.observation_id],
                    "state": "candidate",
                }
            ]
        )
