import pytest
from tracking_fixtures import scope as scope

from homeplane.recognition_contracts import ObservationTiming
from homeplane.recognition_timing import CaptureStamp, TimingBoundary


def test_unverified_source_wall_clock_is_never_capture_or_av_alignment(scope):
    value = TimingBoundary(scope[0], started_monotonic=10.)
    result = value.accept(ObservationTiming(observed_monotonic=10., source_timestamp=1790000000000),
        CaptureStamp(500., "capture_A"), now_monotonic=10.)
    assert result.capture.monotonic == 500. and result.timing.observed_monotonic == 10.
    assert result.timing.source_clock_domain == "unverified"
    assert result.timing.av_sync == result.av_sync == "unavailable"
    result = value.accept(ObservationTiming(observed_monotonic=10.1, source_timestamp=2**64-1),
        CaptureStamp(500.1, "capture_A"), now_monotonic=10.1)
    assert result.reset_reason is None  # source deltas have no known units/rate


@pytest.mark.parametrize("observed,now", [(9., 10.), (10.1, 10.), (10., 10.3), (18., 18.),
                                          (10., float("nan")), (10., True)])
def test_timing_admission_deadline_freshness_and_finite_now(scope, observed, now):
    value = TimingBoundary(scope[0], started_monotonic=10.)
    with pytest.raises(ValueError):
        value.accept(ObservationTiming(observed_monotonic=observed), CaptureStamp(500., "A"), now_monotonic=now)
    assert value._closed


def test_short_session_bound_is_not_extended_by_capture_domain_or_reset(scope):
    session = scope[0].model_copy(update={"seconds": 2.})
    value = TimingBoundary(session, started_monotonic=10.)
    value.accept(ObservationTiming(observed_monotonic=10.), CaptureStamp(1e12, "A"), now_monotonic=10.)
    value.reset()
    with pytest.raises(ValueError, match="timing_expired"):
        value.accept(ObservationTiming(observed_monotonic=12.), CaptureStamp(1., "B"), now_monotonic=12.)


def test_capture_pair_and_binding_cannot_be_forged(scope):
    with pytest.raises(ValueError):
        ObservationTiming(observed_monotonic=10., capture_monotonic=5.)
    with pytest.raises(ValueError):
        ObservationTiming(observed_monotonic=10., capture_clock_id="A")
    with pytest.raises(ValueError):
        ObservationTiming(observed_monotonic=10., source_clock_domain="UTC")
    with pytest.raises(ValueError):
        ObservationTiming(observed_monotonic=10., av_sync="verified")
    value = TimingBoundary(scope[0], started_monotonic=10.)
    with pytest.raises(ValueError, match="capture_binding_mismatch"):
        value.accept(ObservationTiming(observed_monotonic=10., capture_monotonic=5., capture_clock_id="A", capture_evidence="synthetic_owner_fixture"),
                     CaptureStamp(6., "A"), now_monotonic=10.)


@pytest.mark.parametrize("monotonic,clock_id", [(float("inf"), "A"), (-1., "A"), (True, "A"), (1., ""),
                                               (1., "a/b"), (1., "x"*65)])
def test_capture_stamp_validation(monotonic, clock_id):
    with pytest.raises(ValueError, match="invalid_capture_stamp"):
        CaptureStamp(monotonic, clock_id)
