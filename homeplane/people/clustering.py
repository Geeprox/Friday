"""Deterministic bounded complete-link proposals over existing recognition vectors.

Embeddings remain caller-owned memory. No model, dependency, centroid template,
nearest-person fallback, or interpretation of similarity as confidence.
"""

import hashlib
import math
import re
from dataclasses import dataclass, field

from homeplane.recognition.contracts import Embedding, VerificationPolicy

MAX_BATCH = 256


def digest(parts):
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class FaceInput:
    source_digest: str
    frame_ref: str
    face_slot: int
    embedding: Embedding = field(repr=False)
    qualified: bool = True
    camera_ref: str = "synthetic_camera"
    track_ref: str | None = None
    observation_ref: str | None = None
    observed_monotonic: float | None = None

    def __post_init__(self):
        if (
            not isinstance(self.source_digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.source_digest)
            or not isinstance(self.frame_ref, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.frame_ref)
            or type(self.face_slot) is not int
            or not 0 <= self.face_slot <= 7
            or not isinstance(self.camera_ref, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.camera_ref)
            or self.track_ref is not None
            and (
                not isinstance(self.track_ref, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.track_ref)
            )
            or not isinstance(self.embedding, Embedding)
            or type(self.qualified) is not bool
            or self.observation_ref is not None
            and (
                not isinstance(self.observation_ref, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.observation_ref)
            )
            or self.observed_monotonic is not None
            and (
                type(self.observed_monotonic) not in (float, int)
                or not math.isfinite(self.observed_monotonic)
                or self.observed_monotonic < 0
            )
        ):
            raise ValueError("people_invalid_sample")

    @property
    def sample_id(self):
        # Stable admitted observation / frame / slot identity, independent of model and reindex order.
        return digest(
            [
                "friday-face-sample/1",
                self.observation_ref or self.source_digest,
                self.frame_ref,
                str(self.face_slot),
            ]
        )


def proposals(inputs, policy: VerificationPolicy, annotations, blocked, *, _factory=None):
    """Optional mature clustering plus durable review constraints.

    No caller-created dense distance matrix. scikit-learn may use O(n²) internal
    memory, bounded here to 256 samples. Sorting canonical sample IDs makes the
    adapter deterministic for a fixed runtime. _factory is trusted test-only.
    """
    if (
        not isinstance(inputs, tuple)
        or len(inputs) > MAX_BATCH
        or any(not isinstance(x, FaceInput) for x in inputs)
    ):
        raise ValueError("people_batch_budget")
    if not isinstance(policy, VerificationPolicy) or policy.modality != "face":
        raise ValueError("people_face_policy_required")
    if not policy.calibrated or not policy.calibration_reference:
        raise ValueError("people_policy_unqualified")
    ordered = sorted(inputs, key=lambda x: x.sample_id)
    if len({x.sample_id for x in ordered}) != len(ordered):
        raise ValueError("people_duplicate_sample")
    tracks = {}
    for value in ordered:
        if value.track_ref:
            key = (value.camera_ref, value.track_ref)
            tracks[key] = tracks.get(key, 0) + 1
            if tracks[key] > 3:
                raise ValueError("people_track_sample_budget")
    states, valid = {}, []
    for value in ordered:
        vector, sid = value.embedding, value.sample_id
        if not vector.simulated:
            raise ValueError("people_synthetic_only")
        if annotations.get(sid, {}).get("excluded"):
            states[sid] = "excluded"
        elif not value.qualified or vector.modality != "face" or vector.model_sha256 != policy.model_sha256:
            states[sid] = "unknown"
        else:
            states[sid] = "candidate"
            valid.append(value)
    vectors = {
        v.sample_id: tuple(x / math.hypot(*v.embedding.values) for x in v.embedding.values) for v in valid
    }
    if len(valid) >= 2:
        factory = _factory
        if factory is None:
            try:
                from sklearn.cluster import AgglomerativeClustering
            except ImportError:
                raise ValueError("people_clustering_dependency_unavailable") from None
            factory = AgglomerativeClustering
        try:
            model = factory(
                n_clusters=None,
                metric="cosine",
                linkage="complete",
                distance_threshold=1.0 - policy.accept_similarity,
                compute_full_tree=True,
            )
            labels = list(model.fit_predict([vectors[v.sample_id] for v in valid]))
            if len(labels) != len(valid) or any(
                type(x).__name__ not in ("int", "int32", "int64") or int(x) < 0 for x in labels
            ):
                raise ValueError("invalid_partition")
        except Exception:
            raise ValueError("people_clustering_output_unavailable") from None
    else:
        labels = [0] * len(valid)

    def similarity(a, b):
        return max(-1.0, min(1.0, sum(x * y for x, y in zip(vectors[a], vectors[b], strict=True))))

    def anchor(sid):
        return annotations.get(sid, {}).get("person_id")

    anchored, raw = {}, {}
    for value, label in zip(valid, labels, strict=True):
        sid = value.sample_id
        if anchor(sid):
            anchored.setdefault(anchor(sid), []).append(sid)
        else:
            raw.setdefault(int(label), []).append(sid)
    groups = [anchored[key] for key in sorted(anchored)]
    # Review constraints filter library proposals; they never re-cluster vectors.
    for proposed in raw.values():
        remainder = []
        for sid in proposed:
            matches = []
            for pid, group in sorted(anchored.items()):
                if any(frozenset((sid, other)) in blocked for other in group):
                    continue
                score = min(similarity(sid, other) for other in group)
                if score >= policy.accept_similarity:
                    matches.append((score, pid))
            matches.sort(key=lambda x: (-x[0], x[1]))
            if len(matches) > 1 and matches[0][0] - matches[1][0] < policy.ambiguity_margin:
                states[sid] = "ambiguous"
                groups.append([sid])
            elif matches:
                anchored[matches[0][1]].append(sid)
            else:
                remainder.append(sid)
        if remainder:
            conflict = any(frozenset((a, b)) in blocked for a in remainder for b in remainder if a != b)
            # Defensive complete-link output qualification, never trust a fake partition.
            qualified = all(
                similarity(a, b) >= policy.accept_similarity for a in remainder for b in remainder
            )
            if conflict or not qualified:
                for sid in remainder:
                    states[sid] = "ambiguous"
                    groups.append([sid])
            else:
                groups.append(remainder)
    for group in groups:
        if len(group) == 1 and not anchor(group[0]) and states[group[0]] != "ambiguous":
            states[group[0]] = "unknown"
    groups += [[sid] for sid, status in states.items() if status == "unknown" and sid not in vectors]
    result = []
    for group in groups:
        group = sorted(group)
        representatives = sorted(
            group,
            key=lambda sid: (-sum(similarity(sid, other) for other in group) if sid in vectors else 0, sid),
        )[:3]
        result.append((group, representatives))
    return sorted(result, key=lambda item: item[0]), states
