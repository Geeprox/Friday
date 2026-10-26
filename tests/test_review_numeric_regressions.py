"""Exact independent-review inputs and fail-closed numeric lifecycle regressions."""

import asyncio
import sys

import pytest
from test_consumer_tracking_integration import configure, send
from tracking_fixtures import detection, quality, tracker, update, vector
from tracking_fixtures import scope as scope

from homeplane.recognition_consumer import SuppliedFaceBatch
from homeplane.recognition_contracts import ObservationTiming
from homeplane.recognition_timing import CaptureStamp, TimingBoundary, finite_number
from homeplane.recognition_tracking import Detection, TrackingPolicy

HUGE = 10**1000


def assert_closed(value):
    assert value._closed and value._tracks == [] and value._latest is None
    assert value._timing._closed and value._timing._previous is None


@pytest.mark.parametrize('width,height', [(1e-200, 1e-200), (1e-160, 1e-160),
                                         (5e-324, 1.), (1., 5e-324)])
def test_unusable_area_rejected_before_birth(width, height):
    with pytest.raises(ValueError, match='^invalid_detection$'):
        Detection((0., 0., width, height), .95, quality())


def test_exact_old_geometry_revalidated_after_normal_frame_closes_and_clears(scope):
    value = tracker(scope)
    update(value, [detection(embedding=vector())])
    assert value._latest is not None and value._tracks[0].embedding is not None
    old_accepted = detection()
    # Simulate an old accepted/falsified object at the existing revalidation seam.
    object.__setattr__(old_accepted, 'box', (0., 0., 1e-200, 1e-200))
    with pytest.raises(ValueError, match='^invalid_detection$'):
        update(value, [old_accepted], .1)
    assert_closed(value)
    with pytest.raises(ValueError, match='^tracking_closed$'):
        update(value, [detection()], .2)


def test_exact_oversized_integer_now_closes_and_clears(scope):
    value = tracker(scope)
    update(value, [detection(embedding=vector())])
    with pytest.raises(ValueError, match='^invalid_timing$'):
        value.update(value.session, (detection(),), ObservationTiming(observed_monotonic=10.1),
                     CaptureStamp(100.1, 'capture_A'), now_monotonic=HUGE)
    assert_closed(value)
    with pytest.raises(ValueError, match='^tracking_closed$'):
        update(value, [detection()], .2)


@pytest.mark.parametrize('value', [HUGE, -HUGE], ids=['huge_positive', 'huge_negative'])
def test_predicate_rejects_unrepresentable_integer_without_throwing(value):
    assert finite_number(value) is False


def test_shared_numeric_validation_for_capture_detection_policy_and_timing(scope):
    with pytest.raises(ValueError, match='^invalid_capture_stamp$'):
        CaptureStamp(HUGE, 'capture_A')
    with pytest.raises(ValueError, match='^invalid_detection$'):
        Detection((HUGE, 0., .2, .2), .95, quality())
    with pytest.raises(ValueError, match='^invalid_detection$'):
        Detection((0., 0., .2, .2), HUGE, quality())
    with pytest.raises(ValueError, match='^invalid_tracking_policy$'):
        TrackingPolicy(ttl_seconds=HUGE)
    with pytest.raises(ValueError, match='^invalid_timing_owner$'):
        TimingBoundary(scope[0], started_monotonic=HUGE)
    with pytest.raises(ValueError, match='^invalid_timing_owner$'):
        TimingBoundary(scope[0], started_monotonic=10., max_capture_gap=HUGE)
    assert finite_number(int(sys.float_info.max)) is True
    assert finite_number(True) is False


@pytest.mark.parametrize('error', [ZeroDivisionError, OverflowError, FloatingPointError])
def test_cost_arithmetic_failure_is_sanitized_and_retires_old_batch(scope, monkeypatch, error):
    value = tracker(scope)
    update(value, [detection(embedding=vector())])

    def fail(*args):
        raise error('private_numeric_detail_must_not_escape')

    monkeypatch.setattr(value, '_cost', fail)
    with pytest.raises(ValueError, match='^tracking_numeric_invalid$'):
        update(value, [detection()], .1)
    assert_closed(value)


def test_post_assignment_arithmetic_failure_also_closes(scope, monkeypatch):
    value = tracker(scope)
    update(value, [detection(embedding=vector())])
    # Zero delta would fail velocity calculation after the actual SciPy solver.
    # Alter only internal test state after cost calculation, not assignment.
    original = value._cost

    def corrupt_after_cost(track, supplied, capture):
        result = original(track, supplied, capture)
        track.capture = capture
        return result

    monkeypatch.setattr(value, '_cost', corrupt_after_cost)
    with pytest.raises(ValueError, match='^tracking_numeric_invalid$'):
        update(value, [detection()], .1)
    assert_closed(value)


def test_ordinary_continuity_control_still_uses_actual_assignment(scope):
    value = tracker(scope)
    first = update(value, [detection(embedding=vector())])
    second = update(value, [detection(.21, embedding=vector())], .1)
    assert second.decisions[0].track_id == first.decisions[0].track_id
    assert second.decisions[0].state == 'confirmed'
    value.close()
    assert_closed(value)


def test_minimum_normal_area_still_has_valid_continuity(scope):
    value = tracker(scope)
    minimum = detection(0., y=0., width=sys.float_info.min, height=1.)
    first = update(value, [minimum])
    second = update(value, [minimum], .1)
    assert second.decisions[0].track_id == first.decisions[0].track_id
    assert second.decisions[0].state == 'confirmed'
    value.close()
    assert_closed(value)


def test_consumer_rejects_old_unusable_geometry_and_revokes_feature_state(scope):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            await send(consumer, clock)
            assert consumer._feature_sink._features and consumer._tracker._latest is not None
            old_accepted = detection(embedding=vector())
            batch = SuppliedFaceBatch((old_accepted,), CaptureStamp(500.3, 'synthetic_capture_A'))
            object.__setattr__(old_accepted, 'box', (0., 0., 1e-200, 1e-200))
            clock.now = 10.3
            await consumer.video(consumer.session.camera_id, batch, 0)
            assert consumer.status().reason == 'native_failed'
            assert_closed(consumer._tracker)
            assert not consumer._feature_sink._features
            result = await consumer.finish()
            assert result.schema_version == 2 and consumer._feature_message is None
            assert not catalog.page().samples
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())
