"""Conservative time semantics; transport freshness is not measurement freshness."""
import math
from datetime import datetime, timedelta

from .contracts import CanonicalState, Observation


def resolve(observation: Observation, now: datetime, ttl_seconds: int = 300) -> CanonicalState:
    o = observation
    quality, reasons, value, valid_until = "unknown", [], None, None
    if o.transport_health == "offline":
        reasons = ["transport_gap"]
    elif o.value is None or o.value in ("unavailable", "unknown"):
        reasons = ["source_unavailable"]
    elif o.semantics == "derived":
        reasons = ["derived_observation_requires_resolver"]
    elif o.semantics == "latched_state":
        # Long-held state need not change, but must have recent transport evidence.
        if o.transport_health == "online" and 0 <= (now - o.received_at).total_seconds() <= ttl_seconds:
            quality, value = "healthy", o.value
            reasons = ["latched_state_observed_not_new_transition"]
            valid_until = o.received_at + timedelta(seconds=ttl_seconds)
        else:
            reasons = ["latched_state_transport_unknown"]
    elif o.measured_at is None or o.freshness_basis != "device_sample":
        reasons = ["measurement_time_unknown", "integration_write_is_not_hardware_sample"]
    elif o.cached is True:
        reasons = ["refresh_returned_cache"]
        if (now - o.measured_at).total_seconds() > ttl_seconds:
            quality = "stale"
    elif o.clock_uncertainty_seconds is None or o.clock_uncertainty_seconds > 30:
        reasons = ["measurement_clock_uncertain"]
    elif (now - o.measured_at).total_seconds() < -o.clock_uncertainty_seconds:
        reasons = ["measurement_in_future"]
    elif (now - o.measured_at).total_seconds() + o.clock_uncertainty_seconds > ttl_seconds:
        quality, reasons = "stale", ["measurement_expired"]
    else:
        quality, value = "healthy", o.value
        reasons = ["device_sample_within_validity"]
        valid_until = o.measured_at + timedelta(seconds=ttl_seconds - o.clock_uncertainty_seconds)
    return CanonicalState(
        id=f"{o.entity_id}:{o.capability}", value=value, unit=o.unit, quality=quality,
        reasons=reasons, selected_source=o.physical_source_id if value is not None else None,
        excluded_sources={} if value is not None else {o.physical_source_id: reasons[0]},
        as_of=now, valid_until=valid_until, simulated=o.simulated,
    )


def choose_primary(observations: list[Observation], now: datetime, *, canonical_id: str) -> CanonicalState:
    """Ordered, preconfigured primary sources only. No averaging or name-based deduplication."""
    seen, excluded = set(), {}
    for observation in observations:
        if observation.physical_source_id in seen:
            continue
        seen.add(observation.physical_source_id)
        state = resolve(observation, now)
        if state.quality == "healthy":
            return state.model_copy(update={"id": canonical_id, "excluded_sources": excluded})
        excluded[observation.physical_source_id] = ",".join(state.reasons)
    return CanonicalState(
        id=canonical_id, quality="unknown", reasons=["no_valid_independent_source"],
        excluded_sources=excluded, as_of=now, simulated=all(o.simulated for o in observations),
    )


def greater_than(state: CanonicalState, threshold: float, now: datetime) -> str:
    return compare_numeric(state, "gt", threshold, now)


def compare_numeric(state: CanonicalState, operation: str, threshold: float, now: datetime) -> str:
    """Three-valued comparisons: an uncertainty interval crossing a threshold is unknown."""
    if state.quality != "healthy" or state.valid_until is None or now >= state.valid_until:
        return "unknown"
    if (state.as_of > now or type(state.value) not in (int, float) or not math.isfinite(state.value)
            or type(threshold) not in (int, float) or not math.isfinite(threshold)):
        return "unknown"
    import operator
    compare = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge, "eq": operator.eq}.get(operation)
    if compare is None:
        return "unknown"
    low, high = state.value_interval or (state.value, state.value)
    if not math.isfinite(low) or not math.isfinite(high) or not low <= state.value <= high:
        return "unknown"
    if operation == "eq" and low != high:
        return "unknown" if low <= threshold <= high else "false"
    results = {compare(low, threshold), compare(high, threshold)}
    return "unknown" if len(results) != 1 else "true" if True in results else "false"
