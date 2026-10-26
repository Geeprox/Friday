"""Simulated independent physical sources; no household calibration or hardware claims."""
from datetime import UTC, datetime, timedelta

import pytest

from homeplane.contracts import Observation
from homeplane.measurement_profiles import (
    DiagnosticReading,
    MeasurementGroup,
    MeasurementProfile,
    RelativeCalibration,
)
from homeplane.quality import compare_numeric, greater_than, resolve
from homeplane.sensor_quality import SensorQualityEngine

START = datetime(2026, 9, 30, 12, tzinfo=UTC)


def profiles():
    return [MeasurementProfile(source="sim:room", entity="sensor.room", location="living", placement="occupied_zone",
        installation_height_m=1.2, reporting="periodic_sample", context="ambient", version="simulated-room-v1", simulated=True),
        MeasurementProfile(source="sim:ac", entity="climate.room", location="living", placement="return_air",
        installation_height_m=2.2, reporting="periodic_sample", context="fan-only", version="simulated-ac-v1", simulated=True)]


def calibration(items):
    return RelativeCalibration(source=items[1].source, reference=items[0].source, source_profile=items[1].fingerprint(),
        reference_profile=items[0].fingerprint(), context=items[1].context, version="SIMULATED-relative-v1", bias=2,
        residual_bound=.2, reference_minimum=20, reference_maximum=30, sample_count=12,
        valid_until=START+timedelta(days=1), simulated=True)


def engine(calibrated=True):
    items = profiles()
    group = MeasurementGroup(entity="room.living", name="Simulated room temperature", location="living",
                             primary="sim:room", sources=[p.source for p in items])
    return SensorQualityEngine(items, [group], [calibration(items)] if calibrated else [])


def observations(seconds, a=24.5, b=26.5, **change):
    at = START+timedelta(seconds=seconds)
    result = []
    for p, value in zip(profiles(), (a, b), strict=True):
        fields = dict(entity_id=p.entity, capability="temperature", physical_source_id=p.source,
            ingest_source="simulated:ha" if p.source == "sim:room" else "simulated:geek", value=value, unit="°C",
            measured_at=at, source_reported_at=at, received_at=at, freshness_basis="device_sample", cached=False,
            clock_uncertainty_seconds=1, semantics="periodic_sample", transport_health="online", simulated=True)
        if p.source == "sim:room":
            fields.update(change)
        result.append(Observation(**fields))
    return result


def frame(instance, seconds, a=24.5, b=26.5, *, contexts=None, diagnostics=None, **change):
    return instance.evaluate(observations(seconds, a, b, **change), START+timedelta(seconds=seconds),
        contexts=contexts or {"sim:room": "ambient", "sim:ac": "fan-only"}, diagnostics=diagnostics)[0]


def warm(instance):
    for seconds in (0, 15, 30):
        result = frame(instance, seconds)
    return result


def evidence(instance, source="sim:room"):
    return next(item for item in instance.last_sources if item.physical_source_id == source)


def test_stable_temperature_with_real_repeated_samples_is_healthy_not_frozen():
    instance = engine()
    assert warm(instance).quality == "healthy"
    for seconds in (60, 90, 120):
        assert frame(instance, seconds).selected_source == "sim:room"
    assert evidence(instance).quality == "healthy" and evidence(instance).distinct_sample_count == 6
    assert "frozen_under_change" not in evidence(instance).reasons


def test_cache_ha_write_and_forwarded_alias_do_not_advance_recovery():
    instance = engine()
    at = START
    for seconds in (0, 30, 60):
        rows = observations(seconds, measured_at=at, received_at=START+timedelta(seconds=seconds))
        forwarded = rows[0].model_copy(update={"ingest_source": "simulated:second-adapter"})
        instance.evaluate(rows+[forwarded], START+timedelta(seconds=seconds), contexts={"sim:room":"ambient","sim:ac":"fan-only"})
    assert evidence(instance).distinct_sample_count == 1 and evidence(instance).recovery_samples == 1
    assert evidence(instance).quality == "suspect"
    frame(instance, 90, cached=True, integration_written_at=START+timedelta(seconds=90))
    assert evidence(instance).quality == "unknown" and evidence(instance).recovery_samples == 0
    assert "refresh_returned_cache" in evidence(instance).reasons


def test_single_disagreement_never_adjudicates_bad_source_but_repeated_independent_change_does():
    instance = engine()
    warm(instance)
    first = frame(instance, 60, b=29)
    assert first.quality == "unknown" and first.value_interval == (24.5, 27.2)
    assert evidence(instance).quality == "healthy"
    frame(instance, 90, b=29.5)
    assert evidence(instance).quality == "healthy"
    selected = frame(instance, 120, b=30)
    assert evidence(instance).quality == "suspect" and "frozen_under_change" in evidence(instance).reasons
    assert len(evidence(instance).evidence_refs) >= 6
    assert selected.quality == "healthy" and selected.selected_source == "sim:ac" and selected.value == 28
    assert selected.inferred and selected.calibration_version == "SIMULATED-relative-v1"


def test_no_average_when_disagreement_cannot_be_adjudicated():
    instance = engine()
    warm(instance)
    for seconds, a, b in [(60, 24.6, 29), (90, 24.4, 30), (120, 24.7, 31)]:
        result = frame(instance, seconds, a, b)
    assert result.quality == "unknown" and result.value is None and result.selected_source is None
    assert result.value_interval == (24.7, 29.2) and "peer_disagreement_unresolved" in result.reasons
    assert all(item.quality == "healthy" for item in instance.last_sources)


def test_ac_freshness_alone_is_not_room_representativeness():
    instance = engine(calibrated=False)
    warm(instance)
    result = frame(instance, 60, cached=True, measured_at=START-timedelta(hours=8))
    assert result.quality == "unknown" and result.value is None
    assert result.excluded_sources["sim:ac"] == "fallback_calibration_required"
    assert evidence(instance).quality == "stale"


def test_valid_relative_fallback_exposes_interval_scope_and_expiry():
    instance = engine()
    warm(instance)
    result = frame(instance, 60, transport_health="offline")
    assert result.selected_source == "sim:ac" and result.value == 24.5 and result.value_interval == (24.3, 24.7)
    assert "relative_calibrated_fallback_not_absolute_truth" in result.reasons
    assert greater_than(result, 24.6, START+timedelta(seconds=60)) == "unknown"
    assert greater_than(result, 24, START+timedelta(seconds=60)) == "true"
    assert greater_than(result, 25, START+timedelta(seconds=60)) == "false"
    assert greater_than(result, 24, result.valid_until) == "unknown"


@pytest.mark.parametrize("mutation", ["context", "profile", "expired", "hardware", "range"])
def test_calibration_never_transfers_between_modes_profiles_or_real_hardware(mutation):
    instance = engine()
    warm(instance)
    if mutation == "profile":
        instance.profiles["sim:ac"] = instance.profiles["sim:ac"].model_copy(update={"installation_height_m":3})
    elif mutation == "expired":
        instance.calibrations[("sim:room","sim:ac")].valid_until = START
    elif mutation == "hardware":
        instance.calibrations[("sim:room","sim:ac")].simulated = False
    result = frame(instance, 60, b=40 if mutation == "range" else 26.5, transport_health="offline",
        contexts={"sim:room":"ambient","sim:ac":"cooling"} if mutation == "context" else None)
    assert result.quality == "unknown" and result.selected_source is None


def test_recovery_and_switch_hysteresis_do_not_oscillate_on_transport_flaps():
    instance = engine()
    warm(instance)
    assert frame(instance, 60, transport_health="offline").selected_source == "sim:ac"
    for seconds in (65, 80, 95):
        result = frame(instance, seconds)
    assert evidence(instance).quality == "healthy"
    assert result.selected_source == "sim:ac"  # Recovery succeeded; hold window has not.
    assert frame(instance, 120).selected_source == "sim:room"
    assert frame(instance, 125, transport_health="offline").selected_source == "sim:ac"
    assert frame(instance, 130).selected_source == "sim:ac"
    assert frame(instance, 135, cached=True).selected_source == "sim:ac"
    assert evidence(instance).recovery_samples == 0


@pytest.mark.parametrize("stale_value", [12,100])
def test_stale_battery_is_advisory_not_proof_of_good_or_bad_current_measurement(stale_value):
    instance = engine()
    warm(instance)
    result = frame(instance, 60, diagnostics={"sim:room":{"battery":DiagnosticReading(
        value=stale_value, measured_at=START-timedelta(days=180), freshness_basis="device_sample")}})
    assert result.quality == "healthy" and "battery_warning" not in evidence(instance).reasons
    assert "battery_reading_stale_or_unknown" in evidence(instance).reasons
    frame(instance, 90, diagnostics={"sim:room":{"battery":DiagnosticReading(
        value=12, measured_at=START+timedelta(seconds=90), freshness_basis="device_sample")}})
    assert "battery_warning" in evidence(instance).reasons


def test_duplicate_conflict_sequence_replay_clock_regression_and_implausible_values_fail_closed():
    instance = engine()
    warm(instance)
    result = frame(instance, 30, a=25)
    assert evidence(instance).quality == "suspect" and "conflicting_duplicate_sample" in evidence(instance).reasons
    result = frame(instance, 60, a=100)
    assert evidence(instance).quality == "faulty" and "physically_implausible" in evidence(instance).reasons
    frame(instance, 90, device_sequence="SIMULATED-fixed-seq")
    frame(instance, 120, device_sequence="SIMULATED-fixed-seq")
    assert "duplicate_device_sequence" in evidence(instance).reasons
    result = frame(instance, 60)
    assert result.quality == "unknown" and result.reasons == ["quality_clock_regressed"]
    assert not instance.selected and not instance.history["sim:room"].samples
    assert all(s.quality == "unknown" and s.distinct_sample_count == 0 for s in instance.last_sources)


def test_relative_bias_learned_only_from_qualified_distinct_pairs_and_never_auto_installed():
    instance = engine(calibrated=False)
    for n in range(16):
        frame(instance, n*10, a=23+n*.1, b=25+n*.1)
    learned = instance.learn_relative("room.living", "sim:ac", START+timedelta(seconds=150), version="SIMULATED-learned-v1")
    assert learned.sample_count >= 8 and learned.bias == pytest.approx(2)
    assert learned.relative_only and learned.residual_bound == pytest.approx(.1)
    assert not instance.calibrations
    frame(instance, 160, transport_health="offline")
    with pytest.raises(ValueError, match="healthy_comparable"):
        instance.learn_relative("room.living", "sim:ac", START+timedelta(seconds=160), version="SIMULATED-v2")


def test_latched_contact_keeps_transport_semantics_and_no_thermometer_freeze_rule():
    item = observations(0)[0].model_copy(update={"value":"closed", "semantics":"latched_state", "measured_at":None})
    item.received_at = START+timedelta(days=20)
    state = resolve(item, item.received_at)
    assert state.quality == "healthy" and state.value == "closed"


def test_polling_elapsed_time_does_not_replace_a_sample_spanning_recovery_window():
    instance = engine()
    for seconds in (0, 1, 2):
        frame(instance, seconds)
    # Transport is fresh but the latest hardware sample remains at second 2.
    frame(instance, 60, measured_at=START+timedelta(seconds=2))
    assert evidence(instance).quality == "suspect" and evidence(instance).recovery_samples == 3
    frame(instance, 61)
    assert evidence(instance).quality == "healthy"


def test_measurement_profile_change_resets_prior_recovery_and_invalidates_calibration():
    instance = engine()
    warm(instance)
    instance.profiles["sim:ac"] = instance.profiles["sim:ac"].model_copy(update={"installation_height_m":3})
    result = frame(instance, 60, transport_health="offline")
    assert result.quality == "unknown"
    assert evidence(instance,"sim:ac").recovery_samples == 1
    for seconds in (75, 90):
        result = frame(instance, seconds, transport_health="offline")
    assert result.excluded_sources["sim:ac"] == "calibration_scope_expired_or_changed"


def test_change_triggered_reports_and_derived_values_have_distinct_semantics():
    items = profiles()
    items[0].reporting = "change_triggered"
    instance = SensorQualityEngine(items, [MeasurementGroup(entity="room.living",name="SIMULATED room",
        location="living",primary="sim:room",sources=[p.source for p in items])])
    for seconds in (0, 30, 60, 90):
        rows = observations(seconds, b=26.5+seconds/10)
        rows[0].semantics = "change_triggered"
        instance.evaluate(rows, START+timedelta(seconds=seconds),contexts={"sim:room":"ambient","sim:ac":"fan-only"})
    assert evidence(instance).quality == "healthy" and "frozen_under_change" not in evidence(instance).reasons
    derived = observations(90)[0].model_copy(update={"semantics":"derived"})
    assert resolve(derived,derived.received_at).quality == "unknown"


def test_one_peer_sample_cannot_be_reused_to_meet_calibration_pair_count():
    instance = engine(calibrated=False)
    for seconds in range(151):
        peer_at = seconds//30*30
        rows = observations(seconds,a=24+seconds*.01,b=26+peer_at*.01)
        rows[1].measured_at = START+timedelta(seconds=peer_at)
        instance.evaluate(rows,START+timedelta(seconds=seconds),contexts={"sim:room":"ambient","sim:ac":"fan-only"})
    assert evidence(instance,"sim:ac").distinct_sample_count == 6
    with pytest.raises(ValueError,match="insufficient_distinct_calibration_pairs"):
        instance.learn_relative("room.living","sim:ac",START+timedelta(seconds=150),version="SIMULATED-v1")


def test_real_and_simulated_profiles_cannot_share_a_measurement_group():
    items = profiles()
    items[1].simulated = False
    with pytest.raises(ValueError,match="measurement_group_scope_mismatch"):
        SensorQualityEngine(items,[MeasurementGroup(entity="room.living",name="SIMULATED room",
            location="living",primary="sim:room",sources=[p.source for p in items])])


@pytest.mark.parametrize("operation,threshold,expected", [
    ("gt",24.5,"unknown"),("ge",24.3,"true"),("lt",24.5,"unknown"),("le",24.7,"true"),
    ("eq",24.5,"unknown"),("eq",30,"false"),
])
def test_interval_comparisons_never_use_midpoint_as_certainty(operation, threshold, expected):
    instance = engine()
    warm(instance)
    result = frame(instance, 60, transport_health="offline")
    assert compare_numeric(result, operation, threshold, START+timedelta(seconds=60)) == expected


def test_numeric_interval_and_expiry_properties_over_finite_domain():
    import operator

    from homeplane.contracts import CanonicalState
    operations = {"gt":operator.gt,"ge":operator.ge,"lt":operator.lt,"le":operator.le,"eq":operator.eq}
    for low in (-10,0,20,24,30,50):
        for width in (0,.2,1,5):
            high = low+width
            midpoint = (low+high)/2
            state = CanonicalState(id="SIMULATED-room:temperature",value=midpoint,value_interval=(low,high),
                unit="°C",quality="healthy",reasons=["SIMULATED-bounded-interval"],as_of=START,
                valid_until=START+timedelta(seconds=5),simulated=True)
            for threshold in (low-1,low,midpoint,high,high+1):
                for name, compare in operations.items():
                    result = compare_numeric(state,name,threshold,START)
                    facts = [compare(v,threshold) for v in (low,midpoint,high)]
                    if result == "true":
                        assert all(facts)
                    elif result == "false":
                        assert not any(facts)
                    for age in (5,6,100):
                        assert compare_numeric(state,name,threshold,START+timedelta(seconds=age)) == "unknown"
                    assert compare_numeric(state.model_copy(update={"quality":"unknown"}),name,threshold,START) == "unknown"
