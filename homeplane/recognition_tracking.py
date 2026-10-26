"""Bounded synthetic box continuity on the existing recognition observation seam.

SciPy owns assignment. No inference, identity comparisons, capture, persistence,
threads, queues, clock discovery, active speaker or device-authority callbacks.
"""

import math
import sys
from dataclasses import dataclass, field
from uuid import uuid4

from .recognition.contracts import Embedding
from .recognition_contracts import (
    ObservationQuality,
    RecognitionObservation,
    RecognitionProvenance,
    RecognitionSession,
)
from .recognition_feature_handoff import feature_observation_id
from .recognition_timing import TimingBoundary, finite_number


@dataclass(frozen=True)
class Detection:
    """Supplied normalized xywh geometry, per-box quality and optional face vector."""

    box: tuple[float, float, float, float]
    confidence: float
    quality: ObservationQuality
    embedding: Embedding | None = field(default=None, repr=False)

    def __post_init__(self):
        if (not isinstance(self.box, tuple) or len(self.box) != 4
                or any(not finite_number(v) or v > 1 for v in self.box)
                or min(self.box[2:]) <= 0 or self.box[0] + self.box[2] > 1
                or self.box[1] + self.box[3] > 1
                # Association divides by area and sqrt(area). Reject zero or
                # subnormal products rather than permitting unusable geometry.
                or self.box[2] * self.box[3] < sys.float_info.min
                or not finite_number(self.confidence) or self.confidence > 1
                or not isinstance(self.quality, ObservationQuality)
                or self.embedding is not None and not isinstance(self.embedding, Embedding)):
            raise ValueError("invalid_detection")


@dataclass(frozen=True)
class TrackingPolicy:
    """Explicit synthetic thresholds; no deployment/calibration claim."""

    max_tracks: int = 16
    max_frames: int = 128
    max_created: int = 128
    max_misses: int = 3
    ttl_seconds: float = .8
    minimum_confidence: float = .9
    minimum_iou: float = .25
    center_gate: float = .75
    embedding_iou: float = .05
    embedding_center_gate: float = 1.5
    minimum_similarity: float = .85
    ambiguity_margin: float = .08
    area_ratio: float = 2.0

    def __post_init__(self):
        bounds = {"max_tracks": (1, 16), "max_frames": (1, 256), "max_created": (1, 256),
                  "max_misses": (0, 8)}
        if any(type(getattr(self, key)) is not int or not low <= getattr(self, key) <= high
               for key, (low, high) in bounds.items()):
            raise ValueError("invalid_tracking_policy")
        for key in ("ttl_seconds", "minimum_confidence", "minimum_iou", "center_gate",
                    "embedding_iou", "embedding_center_gate", "minimum_similarity",
                    "ambiguity_margin", "area_ratio"):
            if not finite_number(getattr(self, key)):
                raise ValueError("invalid_tracking_policy")
        if (not 0 < self.ttl_seconds <= 2 or not .9 <= self.minimum_confidence <= 1
                or not .25 <= self.minimum_iou <= 1 or not 0 < self.center_gate <= .75
                or not .05 <= self.embedding_iou <= self.minimum_iou
                or not self.center_gate <= self.embedding_center_gate <= 1.5
                or not .85 <= self.minimum_similarity <= 1 or not .01 <= self.ambiguity_margin <= .5
                or not 1 <= self.area_ratio <= 2):
            raise ValueError("invalid_tracking_policy")


@dataclass(frozen=True)
class TrackDecision:
    detection_index: int
    track_id: str | None
    state: str
    reason: str
    identity_claim: str = field(default="none", init=False)
    person_association: str = field(default="unavailable", init=False)
    action_authorized: bool = field(default=False, init=False)
    av_association: str = field(default="unavailable", init=False)


@dataclass(frozen=True)
class TrackSnapshot:
    track_id: str
    state: str
    misses: int


@dataclass(frozen=True)
class TrackBatch:
    decisions: tuple[TrackDecision, ...]
    tracks: tuple[TrackSnapshot, ...]
    reset_reason: str | None


@dataclass
class _Track:
    track_id: str
    box: tuple
    capture: float
    observed: float
    embedding: Embedding | None = field(repr=False)
    velocity: tuple = (0.0, 0.0)
    hits: int = 1
    misses: int = 0


def _center(box):
    return box[0] + box[2] / 2, box[1] + box[3] / 2


def _iou(a, b):
    intersection = max(0., min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(
        0., min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
    return intersection / (a[2]*a[3] + b[2]*b[3] - intersection)


class ObservationTracker:
    """One finite synthetic session, serialized calls on the owner's existing worker.

    Confirmation means two geometric observations, never named-person identity.
    Ambiguous rows/columns stay unknown and cannot create replacement tracks.
    All retained vectors are bounded, private references, dropped on close/reset.
    """

    def __init__(self, session, provenance, *, started_monotonic, policy=None):
        policy = TrackingPolicy() if policy is None else policy
        if (not isinstance(session, RecognitionSession) or session.simulated is not True
                or session.modality != "face" or not isinstance(provenance, RecognitionProvenance)
                or provenance.provider != "opencv_yunet_sface" or not isinstance(policy, TrackingPolicy)):
            raise ValueError("tracking_owner_unqualified")
        self.session = RecognitionSession.model_validate(session.model_dump())
        self.provenance = RecognitionProvenance.model_validate(provenance.model_dump())
        self.policy = policy
        self._timing = TimingBoundary(self.session, started_monotonic=started_monotonic)
        self._tracks = []
        self._frames = self._created = 0
        self._closed = False
        self._shot = None
        self._latest = None

    def __repr__(self):
        return "ObservationTracker(private,synthetic,finite)"

    def close(self):
        self._tracks.clear()
        self._latest = None
        self._timing.close()
        self._closed = True

    def _clear(self):
        self._tracks.clear()
        self._latest = None

    def reset_continuity(self):
        """Serialized owner hook for unavailable capture truth; budgets survive."""
        if self._closed:
            raise ValueError("tracking_closed")
        self._clear()
        self._timing.reset()

    def _qualified(self, detection):
        q, e = detection.quality, detection.embedding
        if (not q.qualified or not all(v is True for v in (q.pose_ok, q.sharpness_ok, q.occlusion_ok))
                or any(v is not None for v in (q.speech_present, q.single_speaker, q.acoustic_quality_ok))
                or detection.confidence < self.policy.minimum_confidence):
            return "quality_unqualified"
        if e is not None and (e.modality != "face" or e.simulated is not True
                             or e.model_sha256 != self.provenance.embedding_model_sha256):
            return "embedding_provenance_mismatch"
        return None

    def _cost(self, track, detection, capture):
        p = self.policy
        dt = min(.5, capture-track.capture)
        # Predict translation only, with a short horizon and bounded velocity.
        box = (track.box[0]+track.velocity[0]*dt, track.box[1]+track.velocity[1]*dt, *track.box[2:])
        ratio = detection.box[2]*detection.box[3]/(box[2]*box[3])
        center = math.dist(_center(box), _center(detection.box))/math.sqrt(box[2]*box[3])
        overlap = _iou(box, detection.box)
        both = track.embedding is not None and detection.embedding is not None
        iou_gate = p.embedding_iou if both else p.minimum_iou
        center_gate = p.embedding_center_gate if both else p.center_gate
        if not 1/p.area_ratio <= ratio <= p.area_ratio or overlap < iou_gate or center > center_gate:
            return None
        if both:
            a, b = track.embedding.values, detection.embedding.values
            score = sum(x*y for x, y in zip(a, b, strict=True))/(math.hypot(*a)*math.hypot(*b))
            score = max(-1., min(1., score))
            if score < p.minimum_similarity:
                return None
            return .4*(1-overlap)+.2*center/center_gate+.4*(1-score)/2
        return .7*(1-overlap)+.3*center/center_gate

    def update(self, session, detections, timing, capture, *, now_monotonic, shot_id=None, reset=False):
        try:
            return self._update(session, detections, timing, capture,
                now_monotonic=now_monotonic, shot_id=shot_id, reset=reset)
        except ArithmeticError:
            # Cover validation, costs and post-assignment arithmetic alike; no
            # previous ID/latest batch can survive a failed numeric update.
            self.close()
            raise ValueError("tracking_numeric_invalid") from None

    def _update(self, session, detections, timing, capture, *, now_monotonic, shot_id, reset):
        if self._closed:
            raise ValueError("tracking_closed")
        if session != self.session:
            self.close()
            raise ValueError("tracking_owner_mismatch")
        if (not isinstance(detections, tuple) or len(detections) > 8
                or any(not isinstance(d, Detection) for d in detections)
                or type(reset) is not bool or shot_id is not None and (
                    not isinstance(shot_id, str) or not 1 <= len(shot_id) <= 64
                    or not all(c.isascii() and (c.isalnum() or c in "_-") for c in shot_id))):
            self.close()
            raise ValueError("tracking_input_budget_or_shape")
        if self._frames >= self.policy.max_frames:
            self.close()
            raise ValueError("tracking_frame_budget")
        try:
            for d in detections:
                Detection(d.box, d.confidence, ObservationQuality.model_validate(d.quality.model_dump()), d.embedding)
                if d.embedding is not None:
                    Embedding(d.embedding.modality, d.embedding.model_sha256, d.embedding.values, d.embedding.simulated)
            stamp = self._timing.accept(timing, capture, now_monotonic=now_monotonic)
        except ValueError:
            self.close()
            raise
        self._frames += 1
        reason = "explicit_reset" if reset else "shot_changed" if self._frames > 1 and shot_id != self._shot else (
            stamp.reset_reason)
        self._shot = shot_id
        if reason is not None:
            self._clear()
        self._tracks = [t for t in self._tracks if timing.observed_monotonic-t.observed <= self.policy.ttl_seconds
                        and t.misses <= self.policy.max_misses]
        bad = {i: self._qualified(d) for i, d in enumerate(detections)}
        costs = [[None if bad[i] else self._cost(t, d, capture.monotonic)
                  for i, d in enumerate(detections)] for t in self._tracks]
        ambiguous = set()
        # Abstain before solver tie-breaking: opaque IDs must not hide uncertainty.
        for row in costs:
            choices = sorted((c, i) for i, c in enumerate(row) if c is not None)
            if len(choices) > 1 and choices[1][0]-choices[0][0] < self.policy.ambiguity_margin:
                ambiguous.update(i for c, i in choices if c-choices[0][0] < self.policy.ambiguity_margin)
        for i in range(len(detections)):
            choices = sorted(row[i] for row in costs if row[i] is not None)
            if len(choices) > 1 and choices[1]-choices[0] < self.policy.ambiguity_margin:
                ambiguous.add(i)
        uncertain_rows = {r for r, row in enumerate(costs) if any(row[i] is not None for i in ambiguous)}
        matched = {}
        if costs and detections:
            try:
                from scipy.optimize import linear_sum_assignment
                # Every row has an independent unmatched option. Gated-out pairs
                # cannot steal columns from valid pairs in rectangular assignment.
                matrix = [[1e6 if c is None or i in ambiguous or r in uncertain_rows else c
                           for i, c in enumerate(row)] + [.95]*len(costs) for r, row in enumerate(costs)]
                rows, columns = linear_sum_assignment(matrix)
                matched = {int(col): int(row) for row, col in zip(rows, columns, strict=True)
                           if col < len(detections) and matrix[row][col] < .95}
            except Exception:
                self.close()
                raise ValueError("tracking_assignment_unavailable") from None
        decisions = []
        old_tracks = tuple(self._tracks)
        for i, detection in enumerate(detections):
            if bad[i] or i in ambiguous:
                decisions.append(TrackDecision(i, None, "unknown", bad[i] or "assignment_ambiguous"))
                continue
            if i in matched:
                track = old_tracks[matched[i]]
                dt = capture.monotonic-track.capture
                a, b = _center(track.box), _center(detection.box)
                track.velocity = tuple(max(-2., min(2., (y-x)/dt)) for x, y in zip(a, b, strict=True))
                track.box, track.capture, track.observed = detection.box, capture.monotonic, timing.observed_monotonic
                # Missing appearance never preserves an older vector indefinitely.
                track.embedding = detection.embedding
                track.hits += 1
                track.misses = 0
                decisions.append(TrackDecision(i, track.track_id, "confirmed", "box_continuity_only"))
            elif any(row[i] is not None for row in costs):
                decisions.append(TrackDecision(i, None, "unknown", "assignment_competition"))
            elif len(self._tracks) >= self.policy.max_tracks or self._created >= self.policy.max_created:
                decisions.append(TrackDecision(i, None, "unknown", "track_budget"))
            else:
                track = _Track("trk_"+uuid4().hex, detection.box, capture.monotonic,
                               timing.observed_monotonic, detection.embedding)
                self._tracks.append(track)
                self._created += 1
                decisions.append(TrackDecision(i, track.track_id, "tentative", "new_box"))
        for index, track in enumerate(old_tracks):
            if index not in matched.values():
                track.misses += 1
        # An unresolved crossing is not an ordinary missing observation. Retire
        # every participating old track; later separation must get fresh IDs.
        uncertain_ids = {old_tracks[row].track_id for row in uncertain_rows}
        self._tracks = [t for t in self._tracks if t.misses <= self.policy.max_misses
                        and t.track_id not in uncertain_ids]
        tracks = tuple(TrackSnapshot(t.track_id, "lost" if t.misses else "confirmed" if t.hits >= 2 else "tentative",
                                     t.misses) for t in self._tracks)
        batch = TrackBatch(tuple(decisions), tracks, reason)
        self._latest = batch, stamp, detections
        return batch

    def bind_observation(self, session, observation, batch, *, now_monotonic, detection_index=0):
        """Private hook for the exact selected existing observation, no new IPC.

        Multi-box callers must supply the observation/slot mapping themselves.
        Current single-face consumer has no reviewed multi-face embedding mapping.
        """
        if (self._closed or self._latest is None or session != self.session
                or batch is not self._latest[0] or not isinstance(observation, RecognitionObservation)
                or type(detection_index) is not int or not 0 <= detection_index < len(batch.decisions)):
            raise ValueError("tracking_observation_binding")
        _, stamp, detections = self._latest
        if (not finite_number(now_monotonic) or not stamp.timing.observed_monotonic <= now_monotonic < self._timing.deadline
                or now_monotonic-stamp.timing.observed_monotonic > .25):
            self.close()
            raise ValueError("tracking_observation_expired")
        detection = detections[detection_index]
        embedding = detection.embedding
        produced = (embedding is not None and embedding.modality == "face" and embedding.simulated is True
                    and embedding.model_sha256 == self.provenance.embedding_model_sha256)
        if (observation.modality != "face" or observation.provenance != self.provenance
                or observation.observation_id != feature_observation_id(session,
                    observation.frame_sequence, observation.face_slot)
                or observation.quality != detection.quality or observation.timing != stamp.timing
                or observation.embedding_produced != produced):
            raise ValueError("tracking_observation_binding")
        decision = batch.decisions[detection_index]
        body = observation.model_dump()
        body.update(track_id=decision.track_id, tracking=decision.state)
        body['timing'].update(capture_monotonic=float(stamp.capture.monotonic),
                              capture_clock_id=stamp.capture.clock_id,
                              capture_evidence="synthetic_owner_fixture")
        return RecognitionObservation.model_validate(body)
