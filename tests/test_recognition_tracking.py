import pytest
from tracking_fixtures import detection, quality, tracker, update, vector
from tracking_fixtures import scope as scope

from homeplane.recognition_tracking import Detection, ObservationTracker, TrackingPolicy


def test_actual_scipy_assignment_rectangular_and_unmatched(scope, monkeypatch):
    import scipy.optimize
    real = scipy.optimize.linear_sum_assignment
    calls = []

    def observe(matrix):
        rows, columns = real(matrix)
        calls.append((matrix, rows.tolist(), columns.tolist()))
        return rows, columns

    monkeypatch.setattr(scipy.optimize, "linear_sum_assignment", observe)
    value = tracker(scope)
    first = update(value, [detection(.1), detection(.6)])
    second = update(value, [detection(.61), detection(.11), detection(.85, width=.1)], .1)
    assert len(calls) == 1 and len(calls[0][0]) == 2 and len(calls[0][0][0]) == 5
    assert second.decisions[0].track_id == first.decisions[1].track_id
    assert second.decisions[1].track_id == first.decisions[0].track_id
    assert second.decisions[2].state == "tentative"
    assert all(d.state == "confirmed" for d in second.decisions[:2])


def test_maximum_assignment_matrix_bound_with_eight_detections_and_sixteen_tracks(scope, monkeypatch):
    import scipy.optimize
    real = scipy.optimize.linear_sum_assignment
    shapes = []

    def observe(matrix):
        shapes.append((len(matrix), len(matrix[0])))
        return real(matrix)

    monkeypatch.setattr(scipy.optimize, "linear_sum_assignment", observe)
    value = tracker(scope)
    first = update(value, [detection(.02+.06*i, y=.1, width=.02, height=.02) for i in range(8)])
    second = update(value, [detection(.02+.06*i, y=.8, width=.02, height=.02) for i in range(8)], .1)
    assert len(second.tracks) == 16
    third = update(value, [detection(.02+.06*i, y=.1, width=.02, height=.02) for i in range(8)], .2)
    assert shapes == [(8, 16), (16, 24)]
    assert [d.track_id for d in third.decisions] == [d.track_id for d in first.decisions]


def test_empty_detections_and_all_gated_pairs_use_unmatched_not_forced_assignment(scope):
    value = tracker(scope)
    old = update(value, [detection(.1)]).decisions[0].track_id
    result = update(value, [detection(.7)], .1)
    assert result.decisions[0].state == "tentative" and result.decisions[0].track_id != old
    assert next(t for t in result.tracks if t.track_id == old).state == "lost"


def test_stable_opaque_ids_and_no_authority(scope):
    value = tracker(scope)
    first = update(value, [detection()])
    second = update(value, [detection(.22)], .1)
    assert first.decisions[0].state == "tentative"
    assert second.decisions[0].track_id == first.decisions[0].track_id
    decision = second.decisions[0]
    assert decision.state == "confirmed" and decision.identity_claim == "none"
    assert decision.person_association == decision.av_association == "unavailable"
    assert not decision.action_authorized
    assert decision.track_id.startswith("trk_") and len(decision.track_id) == 36
    other = update(tracker(scope), [detection()]).decisions[0].track_id
    assert other != decision.track_id and scope[0].camera_id not in decision.track_id


def test_crossing_supplied_same_model_embeddings_reversed_order(scope):
    value = tracker(scope)
    a, b = vector(0), vector(1)
    first = update(value, [detection(.28, embedding=a), detection(.44, embedding=b)])
    middle = update(value, [detection(.38, embedding=b), detection(.34, embedding=a)], .1)
    last = update(value, [detection(.32, embedding=b), detection(.40, embedding=a)], .2)
    assert middle.decisions[0].track_id == last.decisions[0].track_id == first.decisions[1].track_id
    assert middle.decisions[1].track_id == last.decisions[1].track_id == first.decisions[0].track_id
    assert all(d.state == "confirmed" for d in last.decisions)


def test_exact_crossing_without_appearance_abstains_and_no_birth(scope):
    value = tracker(scope)
    first = update(value, [detection(.28), detection(.44)])
    crossing = update(value, [detection(.36), detection(.36)], .1)
    assert all(d.state == "unknown" and d.track_id is None for d in crossing.decisions)
    assert crossing.tracks == ()
    assert value._created == 2
    separated = update(value, [detection(.28), detection(.44)], .2)
    assert all(d.track_id not in {v.track_id for v in first.decisions} for d in separated.decisions)


def test_one_detection_competing_tracks_is_unknown(scope):
    value = tracker(scope)
    update(value, [detection(.28), detection(.44)])
    result = update(value, [detection(.36)], .1)
    assert result.decisions[0].reason == "assignment_ambiguous" and result.decisions[0].track_id is None


def test_short_occlusion_prediction_reacquires_and_long_expiry_does_not(scope):
    value = tracker(scope)
    first = update(value, [detection(.2)]).decisions[0].track_id
    assert update(value, [detection(.24)], .1).decisions[0].track_id == first
    lost = update(value, [], .2)
    assert lost.tracks[0].state == "lost"
    assert update(value, [detection(.32)], .3).decisions[0].track_id == first
    expired = update(value, [detection(.32)], 1.2)
    assert expired.decisions[0].track_id != first and expired.decisions[0].state == "tentative"


def test_miss_limit_expires_without_waiting_for_ttl(scope):
    value = tracker(scope, max_misses=1)
    old = update(value, [detection()]).decisions[0].track_id
    assert len(update(value, [], .1).tracks) == 1
    assert update(value, [], .2).tracks == ()
    assert update(value, [detection()], .3).decisions[0].track_id != old


@pytest.mark.parametrize("q", [quality(qualified=False), quality(pose_ok=False), quality(sharpness_ok=None),
                              quality(occlusion_ok=False), quality(speech_present=True)])
def test_unqualified_quality_never_births_or_refreshes(scope, q):
    value = tracker(scope)
    old = update(value, [detection()]).decisions[0].track_id
    result = update(value, [detection(q=q)], .1)
    assert result.decisions[0].track_id is None and result.decisions[0].reason == "quality_unqualified"
    assert result.tracks[0].track_id == old and result.tracks[0].state == "lost"


@pytest.mark.parametrize("embedding", [vector(sha="c"*64), vector(simulated=False), vector(modality="speaker")])
def test_cross_model_or_modality_or_simulation_unknown(scope, embedding):
    value = tracker(scope)
    result = update(value, [detection(embedding=embedding)])
    assert result.decisions[0].reason == "embedding_provenance_mismatch" and result.tracks == ()


def test_low_similarity_and_far_geometry_do_not_reuse_id(scope):
    value = tracker(scope)
    old = update(value, [detection(embedding=vector())]).decisions[0].track_id
    different = update(value, [detection(.22, embedding=vector(1))], .1)
    assert different.decisions[0].state == "tentative" and different.decisions[0].track_id != old
    far = update(value, [detection(.7, embedding=vector())], .2)
    assert far.decisions[0].track_id != old


def test_missing_embedding_drops_retained_reference_and_uses_strict_geometry(scope):
    value = tracker(scope)
    old = update(value, [detection(embedding=vector())]).decisions[0].track_id
    assert update(value, [detection(.21)], .1).decisions[0].track_id == old
    assert value._tracks[0].embedding is None


@pytest.mark.parametrize("kw,reason", [({"reset": True}, "explicit_reset"),
    ({"shot_id": "second"}, "shot_changed"), ({"clock_id": "capture_B"}, "capture_clock_changed"),
    ({"capture": 99.}, "capture_clock_discontinuity"), ({"capture": 100.}, "capture_clock_discontinuity"),
    ({"capture": 103.}, "capture_gap"), ({"source": 3}, "unverified_source_regression")])
def test_resets_do_not_reuse_ids_or_retained_vectors(scope, kw, reason):
    value = tracker(scope)
    old = update(value, [detection(embedding=vector())], source=9).decisions[0].track_id
    result = update(value, [detection()], .1, **kw)
    assert result.reset_reason == reason and result.decisions[0].track_id != old
    assert len(value._tracks) == 1 and value._tracks[0].embedding is None


def test_worker_clock_regression_resets_continuity(scope):
    value = tracker(scope)
    old = update(value, [detection()], .2).decisions[0].track_id
    result = update(value, [detection()], .1, capture=100.3)
    assert result.reset_reason == "worker_clock_discontinuity" and result.decisions[0].track_id != old


def test_active_and_cumulative_track_caps(scope):
    value = tracker(scope, max_tracks=1, max_created=2, max_misses=0)
    first = update(value, [detection(.1), detection(.6)])
    assert first.decisions[1].reason == "track_budget" and len(first.tracks) == 1
    update(value, [], .1)
    second = update(value, [detection(.6)], .2)
    assert second.decisions[0].state == "tentative"
    update(value, [], .3)
    assert update(value, [detection(.1)], .4).decisions[0].reason == "track_budget"
    assert value._created == 2


def test_frame_limit_close_expiry_and_no_restart(scope):
    value = tracker(scope, max_frames=1)
    update(value, [detection(embedding=vector())])
    with pytest.raises(ValueError, match="tracking_frame_budget"):
        update(value, [], .1)
    assert value._tracks == [] and value._latest is None
    with pytest.raises(ValueError, match="tracking_closed"):
        update(value, [])
    value = tracker(scope)
    update(value, [detection()])
    with pytest.raises(ValueError, match="timing_expired"):
        update(value, [], 8.)
    assert value._tracks == []


def test_wrong_session_invalid_budget_and_solver_failure_close(scope, monkeypatch):
    value = tracker(scope)
    update(value, [detection()])
    with pytest.raises(ValueError, match="tracking_owner_mismatch"):
        value.update(scope[0].model_copy(update={"generation": 2}), (), None, None, now_monotonic=10.)
    assert not value._tracks
    value = tracker(scope)
    with pytest.raises(ValueError, match="tracking_input_budget"):
        update(value, [detection()]*9)
    import scipy.optimize

    def failure(*args):
        raise RuntimeError("private_vector_text_must_not_escape")

    monkeypatch.setattr(scipy.optimize, "linear_sum_assignment", failure)
    value = tracker(scope)
    update(value, [detection(embedding=vector())])
    with pytest.raises(ValueError, match="^tracking_assignment_unavailable$"):
        update(value, [detection()], .1)
    assert not value._tracks and value._latest is None


@pytest.mark.parametrize("box", [(0., 0., 0., .2), (.9, 0., .2, .2), (float("nan"), 0., .2, .2),
                                  (-.1, 0., .2, .2), (False, 0., .2, .2)])
def test_invalid_boxes_rejected(box):
    with pytest.raises(ValueError, match="invalid_detection"):
        Detection(box, .95, quality())


@pytest.mark.parametrize("changes", [{"max_tracks": 17}, {"max_frames": 257}, {"max_created": 257},
    {"max_misses": -1}, {"minimum_confidence": .8}, {"center_gate": 2.}, {"minimum_iou": .1},
    {"ambiguity_margin": 0.}, {"minimum_similarity": .1}, {"ttl_seconds": float("inf")}])
def test_unbounded_or_loose_policy_rejected(changes):
    with pytest.raises(ValueError, match="invalid_tracking_policy"):
        TrackingPolicy(**changes)


def test_synthetic_owner_only_and_private_repr(scope):
    with pytest.raises(ValueError, match="tracking_owner_unqualified"):
        ObservationTracker(scope[0].model_copy(update={"simulated": False}), scope[1], started_monotonic=10.)
    assert "values" not in repr(detection(embedding=vector()))
    value = tracker(scope)
    update(value, [detection(embedding=vector())])
    assert scope[0].camera_id not in repr(value)
    value.close()
    value.close()
    assert not value._tracks and value._latest is None
