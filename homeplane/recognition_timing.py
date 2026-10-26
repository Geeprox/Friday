"""Finite synthetic capture timing. Source stamps never establish an AV clock."""

import math
import re
from dataclasses import dataclass, field

from .recognition_contracts import ObservationTiming, RecognitionSession


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        # math.isfinite converts Python integers to float. Unrepresentable values
        # are invalid input, not an exception that may bypass lifecycle cleanup.
        return False


@dataclass(frozen=True)
class CaptureStamp:
    """Trusted supplied monotonic sample, in an explicitly identified clock domain.

    This is not the SDK's currently unverified integer source timestamp. Different
    capture domains cannot be subtracted or compared with worker observation time.
    """

    monotonic: float
    clock_id: str

    def __post_init__(self):
        if (not finite_number(self.monotonic) or not isinstance(self.clock_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.clock_id)):
            raise ValueError("invalid_capture_stamp")


@dataclass(frozen=True)
class TimingDecision:
    timing: ObservationTiming
    capture: CaptureStamp
    reset_reason: str | None = None
    av_sync: str = field(default="unavailable", init=False)


class TimingBoundary:
    """No clock reads, media, wall-time conversion, synchronization or alignment.

    Worker monotonic time bounds admission; supplied capture deltas bound motion.
    Domain changes, regressions, duplicates and gaps break continuity. An integer
    source regression also breaks continuity, but its unit/rate stays unknown.
    """

    def __init__(self, session, *, started_monotonic, max_capture_gap=1.0):
        if (not isinstance(session, RecognitionSession) or session.simulated is not True
                or session.modality != "face" or not finite_number(started_monotonic)
                or started_monotonic >= session.deadline_monotonic
                or not finite_number(max_capture_gap) or not 0 < max_capture_gap <= 2):
            raise ValueError("invalid_timing_owner")
        # Revalidate copied nested owner values; no model_copy bypass.
        self.session = RecognitionSession.model_validate(session.model_dump())
        self.started = float(started_monotonic)
        self.deadline = min(self.started + session.seconds, session.deadline_monotonic)
        self.max_capture_gap = max_capture_gap
        self._previous = None
        self._closed = False

    def close(self):
        self._closed = True
        self._previous = None

    def reset(self):
        self._previous = None

    def accept(self, timing, capture, *, now_monotonic):
        if self._closed:
            raise ValueError("timing_closed")
        if (not isinstance(timing, ObservationTiming) or not isinstance(capture, CaptureStamp)
                or not finite_number(now_monotonic)):
            self.close()
            raise ValueError("invalid_timing")
        try:
            timing = ObservationTiming.model_validate(timing.model_dump())
            CaptureStamp(capture.monotonic, capture.clock_id)
        except ValueError:
            self.close()
            raise ValueError("invalid_timing") from None
        observed = timing.observed_monotonic
        if (not self.started <= observed <= now_monotonic < self.deadline
                or now_monotonic - observed > .25):
            self.close()
            raise ValueError("timing_expired_or_stale")
        # Never let optional wire timing disagree with the private supplied stamp.
        if timing.capture_monotonic is not None and (
                timing.capture_monotonic != capture.monotonic or timing.capture_clock_id != capture.clock_id):
            self.close()
            raise ValueError("capture_binding_mismatch")
        reason = None
        if self._previous is not None:
            prior, old_capture, old_now = self._previous
            if observed <= prior.observed_monotonic or now_monotonic < old_now:
                reason = "worker_clock_discontinuity"
            elif capture.clock_id != old_capture.clock_id:
                reason = "capture_clock_changed"
            elif capture.monotonic <= old_capture.monotonic:
                reason = "capture_clock_discontinuity"
            elif capture.monotonic - old_capture.monotonic > self.max_capture_gap:
                reason = "capture_gap"
            elif (timing.source_timestamp is not None and prior.source_timestamp is not None
                    and timing.source_timestamp < prior.source_timestamp):
                reason = "unverified_source_regression"
        self._previous = timing, capture, now_monotonic
        return TimingDecision(timing, capture, reason)
