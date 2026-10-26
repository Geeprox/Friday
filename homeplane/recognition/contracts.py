"""Bounded in-memory evidence. Similarity is never an authorization or probability."""

import math
import re
from dataclasses import dataclass, field
from typing import Literal

Modality = Literal["face", "speaker"]
State = Literal["disabled", "unavailable", "ready", "unknown", "ambiguous", "matched"]


@dataclass(frozen=True)
class Status:
    state: State
    reason: str

    def __post_init__(self):
        if (self.state not in ("disabled", "unavailable", "ready", "unknown", "ambiguous", "matched")
                or not isinstance(self.reason, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", self.reason)):
            raise ValueError("invalid_status")


@dataclass(frozen=True)
class Embedding:
    modality: Modality
    model_sha256: str
    values: tuple[float, ...] = field(repr=False)
    simulated: bool = True

    def __post_init__(self):
        size = {"face": 128, "speaker": 256}.get(self.modality)
        if (type(self.simulated) is not bool
                or not isinstance(self.model_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.model_sha256)
                or not isinstance(self.values, tuple) or len(self.values) != size
                or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6
                       for v in self.values)
                or math.hypot(*self.values) <= 1e-12):
            raise ValueError("invalid_embedding")


@dataclass(frozen=True)
class EmbeddingResult:
    status: Status
    embedding: Embedding | None = field(default=None, repr=False)

    def __post_init__(self):
        if (not isinstance(self.status, Status)
                or (self.embedding is not None and not isinstance(self.embedding, Embedding))
                or (self.status.state == "ready") != (self.embedding is not None)):
            raise ValueError("invalid_embedding_result")


@dataclass(frozen=True)
class Candidate:
    """Caller-supplied opaque reference; this does not enroll or save a template."""

    reference: str
    embedding: Embedding = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.embedding, Embedding) or not isinstance(self.reference, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.reference)):
            raise ValueError("invalid_candidate_reference")


@dataclass(frozen=True)
class VerificationPolicy:
    modality: Modality
    model_sha256: str
    accept_similarity: float
    ambiguity_margin: float
    calibrated: bool = False
    calibration_reference: str | None = None

    def __post_init__(self):
        if (self.modality not in ("face", "speaker")
                or not isinstance(self.model_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.model_sha256)
                or type(self.accept_similarity) not in (int, float)
                or not math.isfinite(self.accept_similarity) or not -1 <= self.accept_similarity <= 1
                or type(self.ambiguity_margin) not in (int, float)
                or not math.isfinite(self.ambiguity_margin) or not 0 < self.ambiguity_margin <= 2
                or type(self.calibrated) is not bool
                or (self.calibrated and (not isinstance(self.calibration_reference, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.calibration_reference)))):
            raise ValueError("invalid_verification_policy")


@dataclass(frozen=True)
class IdentityEvidence:
    status: Status
    reference: str | None = None
    similarity: float | None = None
    simulated: bool = True
    security_authentication: bool = field(default=False, init=False)


def compare_candidates(observed: EmbeddingResult, candidates: tuple[Candidate, ...],
                       policy: VerificationPolicy) -> IdentityEvidence:
    """One candidate is verification; several are bounded open-set identification.

    No nearest-person fallback, threshold defaults, cross-modal fusion or storage.
    Calibration declarations must come from the trusted owner, not request JSON.
    """
    if observed.status.state != "ready" or observed.embedding is None:
        return IdentityEvidence(Status(observed.status.state, observed.status.reason))
    if not policy.calibrated:
        return IdentityEvidence(Status("unavailable", "threshold_unqualified"))
    if not isinstance(candidates, tuple) or not 1 <= len(candidates) <= 32:
        return IdentityEvidence(Status("unknown", "candidate_budget_or_missing"))
    if any(not isinstance(c, Candidate) for c in candidates):
        return IdentityEvidence(Status("unknown", "invalid_candidate"))
    vector = observed.embedding
    identity = (policy.modality, policy.model_sha256)
    if (vector.modality, vector.model_sha256) != identity:
        return IdentityEvidence(Status("unknown", "model_mismatch"))
    if len({c.reference for c in candidates}) != len(candidates):
        return IdentityEvidence(Status("unknown", "duplicate_candidate"))
    scores = []
    norm = math.hypot(*vector.values)
    for candidate in candidates:
        target = candidate.embedding
        if target.simulated != vector.simulated:
            return IdentityEvidence(Status("unknown", "simulation_provenance_mismatch"))
        if (target.modality, target.model_sha256) != identity:
            return IdentityEvidence(Status("unknown", "model_mismatch"))
        denominator = norm * math.hypot(*target.values)
        score = sum(a * b for a, b in zip(vector.values, target.values, strict=True)) / denominator
        scores.append((max(-1.0, min(1.0, score)), candidate.reference))
    scores.sort(reverse=True)
    if scores[0][0] < policy.accept_similarity:
        return IdentityEvidence(Status("unknown", "below_calibrated_threshold"))
    if len(scores) > 1 and scores[0][0] - scores[1][0] < policy.ambiguity_margin:
        return IdentityEvidence(Status("ambiguous", "candidate_margin_insufficient"))
    return IdentityEvidence(Status("matched", "similarity_evidence_only"), scores[0][1], scores[0][0],
                            vector.simulated)
