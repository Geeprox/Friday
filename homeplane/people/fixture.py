"""Procedural synthetic vectors and metadata, no face photos or voiceprints."""

import hashlib
import math

from homeplane.recognition.contracts import Embedding, VerificationPolicy

from .clustering import FaceInput
from .contracts import Name
from .recognition_bridge import SpeakerAgreement

MODEL = "1" * 64
POLICY = VerificationPolicy(
    "face", MODEL, 0.90, 0.04, calibrated=True, calibration_reference="synthetic_policy_v1"
)


def sample(index, angle, *, qualified=True, model=MODEL):
    values = (math.cos(angle), math.sin(angle)) + (0.0,) * 126
    return FaceInput(
        hashlib.sha256(f"synthetic-live-observation-{index}".encode()).hexdigest(),
        f"synthetic_frame_{index}",
        0,
        Embedding("face", model, values, True),
        qualified,
        "synthetic_camera",
        f"synthetic_track_{index // 2}",
    )


def inputs():
    return tuple(
        [
            sample(i, angle)
            for i, angle in enumerate(
                [0.00, 0.04, 0.08, 0.11, 0.14, 1.40, 1.44, 1.48, 1.51, 1.55, 2.72, 2.75, 2.78, 2.82]
            )
        ]
        + [sample(14, -1.3), sample(15, -2.2, qualified=False)]
    )


def partition_factory(observations, partitions):
    """Contract fake with declared fixture memberships, no clustering algorithm."""
    labels = {sid: index for index, group in enumerate(partitions) for sid in group}
    valid = sorted(
        (v for v in observations if v.qualified and v.embedding.model_sha256 == MODEL),
        key=lambda v: v.sample_id,
    )
    vector_labels = {
        tuple(x / math.hypot(*v.embedding.values) for x in v.embedding.values): labels[v.sample_id]
        for v in valid
    }

    class FixturePartition:
        def __init__(self, **kwargs):
            assert kwargs["metric"] == "cosine" and kwargs["linkage"] == "complete"
            assert kwargs["n_clusters"] is None and kwargs["compute_full_tree"] is True

        def fit_predict(self, vectors):
            return [vector_labels[tuple(vector)] for vector in vectors]

    return FixturePartition


def demo_factory():
    values = inputs()
    groups = [
        [v.sample_id for v in values[:5]],
        [v.sample_id for v in values[5:10]],
        [v.sample_id for v in values[10:14]],
        [values[14].sample_id],
    ]
    return partition_factory(values, groups)


def seed(catalog):
    page = catalog.reindex(inputs(), POLICY, expected_revision=0)
    group = next(c for c in page.clusters if inputs()[0].sample_id in c.sample_ids)
    page = catalog.name(Name(expected_revision=page.revision, cluster_id=group.cluster_id, name="林同学"))
    for i in range(3):
        page = catalog.propose_voice(
            SpeakerAgreement(
                inputs()[0].sample_id,
                "synthetic_voice_A",
                "2" * 64,
                f"window_{i}",
                f"utterance_{i}",
                True,
                True,
                True,
                True,
                overlap_free=True,
                track_continuity_qualified=True,
                speaker_margin_qualified=True,
            ),
            expected_revision=page.revision,
        )
    return page
