"""Bounded, evidence-based sensor health and relative fallback selection.

No sample timestamps, sensor identity, placement or AC context are inferred from
HA cache writes, names, power or availability. Restart begins a recovery window.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from .contracts import CanonicalState, Observation, SourceQualityEvidence
from .measurement_profiles import DiagnosticReading, MeasurementGroup, MeasurementProfile, RelativeCalibration
from .quality import resolve


@dataclass
class Sample:
    at: datetime
    value: float
    reference: str
    context: str
    sequence: str | None = None
    qualified: bool = False


@dataclass
class History:
    samples: deque = field(default_factory=lambda: deque(maxlen=256))
    recovery_start: datetime | None = None
    recovery_count: int = 0
    ready: bool = False
    frozen: bool = False
    valid_since: datetime | None = None


class SensorQualityEngine:
    def __init__(self, profiles: list[MeasurementProfile], groups: list[MeasurementGroup],
                 calibrations: list[RelativeCalibration] | None = None):
        if len(profiles) > 32 or len(groups) > 16 or len(calibrations or []) > 32:
            raise ValueError("measurement_profile_budget")
        self.profiles = {p.source: p for p in profiles}
        if len(self.profiles) != len(profiles):
            raise ValueError("duplicate_physical_source_profile")
        self.groups = {g.entity: g for g in groups}
        if len(self.groups) != len(groups):
            raise ValueError("duplicate_measurement_group")
        for group in groups:
            if any(s not in self.profiles for s in group.sources):
                raise ValueError("unbound_measurement_profile")
            primary = self.profiles[group.primary]
            if primary.location != group.location:
                raise ValueError("primary_location_mismatch")
            if any(self.profiles[s].unit != primary.unit or self.profiles[s].capability != primary.capability
                   for s in group.sources):
                raise ValueError("measurement_group_unit_or_capability_mismatch")
            if any(self.profiles[s].simulated != primary.simulated for s in group.sources):
                raise ValueError("measurement_group_scope_mismatch")
        self.calibrations = {(c.reference, c.source): c for c in calibrations or []}
        if len(self.calibrations) != len(calibrations or []):
            raise ValueError("duplicate_calibration")
        self.history = {p.source: History() for p in profiles}
        self.selected: dict[str, str] = {}
        self.switched: dict[str, datetime] = {}
        self.last_at: datetime | None = None
        self.last_sources: list[SourceQualityEvidence] = []
        self.source_states: dict[str, CanonicalState] = {}
        self.profile_fingerprints = {source: profile.fingerprint() for source, profile in self.profiles.items()}

    def _adjust(self, group, source, value, now, simulated):
        profile = self.profiles[source]
        primary = self.profiles[group.primary]
        if source == group.primary:
            return value, 0, None, None
        calibration = self.calibrations.get((group.primary, source))
        if calibration is None:
            return None, None, None, "fallback_calibration_required"
        if (calibration.valid_until <= now or calibration.source_profile != profile.fingerprint()
                or calibration.reference_profile != primary.fingerprint()
                or calibration.context != profile.context or calibration.simulated != simulated
                or profile.location != group.location or profile.simulated != simulated):
            return None, None, None, "calibration_scope_expired_or_changed"
        corrected = value-calibration.bias
        if not calibration.reference_minimum <= corrected <= calibration.reference_maximum:
            return None, None, None, "outside_calibrated_measurement_range"
        return corrected, calibration.residual_bound, calibration.version, None

    def _frozen(self, group, source, now, raw):
        profile = self.profiles[source]
        if profile.reporting != "periodic_sample" or raw[source].quality != "healthy":
            return False, []
        samples = [s for s in self.history[source].samples
                   if now-timedelta(seconds=profile.freeze_window_seconds+profile.ttl_seconds) <= s.at <= now
                   and (self.history[source].valid_since is None or s.at >= self.history[source].valid_since)]
        if (len(samples) < 3 or (samples[-1].at-samples[0].at).total_seconds() < profile.freeze_window_seconds
                or max(s.value for s in samples)-min(s.value for s in samples) > profile.resolution/10):
            return False, []
        for peer in group.sources:
            if peer == source or raw[peer].quality != "healthy":
                continue
            other = self.profiles[peer]
            if other.reporting != "periodic_sample" or other.location != profile.location:
                continue
            # A return-air peer cannot adjudicate room temperature before calibration.
            if peer != group.primary and self._adjust(group, peer, raw[peer].value, now, raw[peer].simulated)[3]:
                continue
            if source != group.primary and self._adjust(group, source, raw[source].value, now, raw[source].simulated)[3]:
                continue
            pairs, used_peer = [], set()
            for sample in samples:
                peer_sample = next((p for p in self.history[peer].samples
                    if abs((p.at-sample.at).total_seconds()) <= 2 and p.context == other.context
                    and p.reference not in used_peer
                    and (self.history[peer].valid_since is None or p.at >= self.history[peer].valid_since)), None)
                if peer_sample:
                    pairs.append((sample, peer_sample))
                    used_peer.add(peer_sample.reference)
            changed = [pair for pair in pairs if abs(pair[1].value-pairs[0][1].value) >= profile.freeze_change]
            if (len(changed) >= 3 and (pairs[-1][0].at-pairs[0][0].at).total_seconds() >= profile.freeze_window_seconds):
                return True, [s.reference for pair in pairs[-8:] for s in pair]
        return False, []

    def evaluate(self, observations: list[Observation], now: datetime, *, contexts: dict[str, str],
                 diagnostics: dict[str, dict[str, DiagnosticReading]] | None = None,
                 excluded_sources: set[str] | None = None):
        if now.tzinfo is None or len(observations) > 256:
            raise ValueError("bounded_aware_quality_frame_required")
        fingerprints = {source: profile.fingerprint() for source, profile in self.profiles.items()}
        if fingerprints != self.profile_fingerprints:
            self.history = {p: History() for p in self.profiles}
            self.selected.clear()
            self.switched.clear()
            self.profile_fingerprints = fingerprints
        if self.last_at is not None and now < self.last_at:
            self.history = {p: History() for p in self.profiles}
            self.selected.clear()
            self.switched.clear()
            self.last_at = now
            self.source_states = {}
            self.last_sources = [SourceQualityEvidence(physical_source_id=p.source, entity_id=p.entity,
                quality="unknown", reasons=["quality_clock_regressed"], profile_version=p.version,
                location=p.location, placement=p.placement, context="unknown", distinct_sample_count=0,
                recovery_samples=0, simulated=p.simulated) for p in self.profiles.values()]
            return [self._unknown(g, now, {}, ["quality_clock_regressed"]) for g in self.groups.values()]
        self.last_at = now
        diagnostics = diagnostics or {}
        excluded_sources = excluded_sources or set()
        if not excluded_sources <= self.profiles.keys():
            raise ValueError("unbound_measurement_exclusion")
        if len(diagnostics) > 32 or any(set(metrics)-{"battery", "rssi"} for metrics in diagnostics.values()):
            raise ValueError("bounded_diagnostics_required")
        raw, new_samples, advisories = {}, set(), {}
        for source, profile in self.profiles.items():
            candidates = [o for o in observations if o.physical_source_id == source and o.entity_id == profile.entity
                          and o.capability == profile.capability and o.unit == profile.unit]
            candidates.sort(key=lambda o: (o.measured_at or o.received_at, o.cached is not True), reverse=True)
            observation = candidates[0] if candidates else None
            state = resolve(observation, now, profile.ttl_seconds) if observation else CanonicalState(
                id=profile.entity+":"+profile.capability, quality="unknown", reasons=["missing_report"], as_of=now,
                simulated=profile.simulated)
            if source in excluded_sources:
                state = state.model_copy(update={"quality":"unknown", "value":None,
                    "selected_source":None, "valid_until":None, "reasons":["temporarily_excluded"]})
            advisory = []
            for name, metric in diagnostics.get(source, {}).items():
                if (metric.measured_at is None or metric.freshness_basis != "device_sample"
                        or not 0 <= (now-metric.measured_at).total_seconds() <= profile.ttl_seconds):
                    advisory.append(name+"_reading_stale_or_unknown")
                elif name == "battery" and 0 <= metric.value <= 15:
                    advisory.append("battery_warning")
            advisories[source] = advisory
            if observation and state.quality == "healthy":
                if (type(observation.value) not in (int, float) or observation.simulated != profile.simulated
                        or profile.reporting != observation.semantics
                        or profile.reporting not in {"periodic_sample", "change_triggered"}
                        or observation.measured_at is None):
                    state = state.model_copy(update={"quality": "unknown", "value": None,
                        "reasons": ["measurement_profile_or_semantics_mismatch"]})
                elif contexts.get(source) != profile.context:
                    state = state.model_copy(update={"quality": "unknown", "value": None,
                        "reasons": ["measurement_context_unknown_or_changed"]})
                elif not profile.plausible_minimum <= observation.value <= profile.plausible_maximum:
                    state = state.model_copy(update={"quality": "faulty", "value": None,
                        "reasons": ["physically_implausible"]})
                elif any(o.measured_at == observation.measured_at and o.cached is not True
                         and o.value != observation.value for o in candidates):
                    state = state.model_copy(update={"quality": "suspect", "value": None,
                        "reasons": ["conflicting_duplicate_sample"]})
                else:
                    history = self.history[source]
                    if (observation.device_sequence is not None and history.samples
                            and any(s.sequence == observation.device_sequence and s.at != observation.measured_at for s in history.samples)):
                        state = state.model_copy(update={"quality": "unknown", "value": None,
                            "reasons": ["duplicate_device_sequence"]})
                    elif history.samples and observation.measured_at == history.samples[-1].at and observation.value != history.samples[-1].value:
                        state = state.model_copy(update={"quality": "suspect", "value": None,
                            "reasons": ["conflicting_duplicate_sample"]})
                    elif not history.samples or observation.measured_at > history.samples[-1].at:
                        import hashlib
                        identity = source+"|"+observation.measured_at.isoformat()+"|"+(observation.device_sequence or "")
                        reference = "sample:"+hashlib.sha256(identity.encode()).hexdigest()[:24]
                        history.samples.append(Sample(observation.measured_at, float(observation.value), reference,
                                                      profile.context, observation.device_sequence))
                        new_samples.add(source)
                    elif observation.measured_at < history.samples[-1].at:
                        state = state.model_copy(update={"quality": "unknown", "value": None,
                            "reasons": ["out_of_order_sample"]})
            raw[source] = state
        source_states = {}
        for source, state in raw.items():
            history, profile = self.history[source], self.profiles[source]
            groups = [g for g in self.groups.values() if source in g.sources]
            frozen, evidence = False, []
            for group in groups:
                detected, refs = self._frozen(group, source, now, raw)
                frozen |= detected
                evidence += refs
            if frozen:
                history.frozen = True
                state = state.model_copy(update={"quality": "suspect", "value": None,
                    "reasons": ["frozen_under_change"], "evidence_refs": evidence[-32:]})
            elif history.frozen:
                # Only a genuine new changed sample can begin recovery from freezing.
                if source in new_samples and len(history.samples) > 1 and history.samples[-1].value != history.samples[-2].value:
                    history.frozen = False
                else:
                    state = state.model_copy(update={"quality": "suspect" if state.quality == "healthy" else state.quality,
                        "value": None, "reasons": state.reasons+["frozen_under_change"]})
            if state.quality != "healthy":
                history.ready, history.recovery_count, history.recovery_start = False, 0, None
                history.valid_since = now
            elif not history.ready:
                if source in new_samples:
                    if history.recovery_start is None:
                        history.recovery_start = history.samples[-1].at
                    history.recovery_count += 1
                seconds = max((g.recovery_seconds for g in groups), default=30)
                count = max((g.recovery_samples for g in groups), default=3)
                if (source in new_samples and history.recovery_start is not None and history.recovery_count >= count
                        and (history.samples[-1].at-history.recovery_start).total_seconds() >= seconds):
                    history.ready = True
                else:
                    state = state.model_copy(update={"quality": "suspect", "value": None,
                        "reasons": ["recovery_requires_distinct_samples_and_window"]})
            if history.samples and source in new_samples:
                history.samples[-1].qualified = state.quality == "healthy" and not advisories[source]
            if history.samples:
                state.evidence_refs = list(dict.fromkeys(state.evidence_refs+[s.reference for s in list(history.samples)[-8:]]))[-32:]
            state.reasons += advisories[source]
            if state.quality != "healthy":
                state = state.model_copy(update={"value": None, "selected_source": None, "valid_until": None})
            source_states[source] = state
        self.last_sources = [SourceQualityEvidence(physical_source_id=source, entity_id=self.profiles[source].entity,
            quality=state.quality, reasons=state.reasons, profile_version=self.profiles[source].version,
            location=self.profiles[source].location, placement=self.profiles[source].placement,
            installation_height_m=self.profiles[source].installation_height_m, reporting=self.profiles[source].reporting,
            ttl_seconds=self.profiles[source].ttl_seconds, profile_fingerprint=self.profiles[source].fingerprint(),
            context=contexts.get(source, "unknown"), distinct_sample_count=len(self.history[source].samples),
            recovery_samples=self.history[source].recovery_count, evidence_refs=state.evidence_refs, simulated=state.simulated)
            for source, state in source_states.items()]
        self.source_states = source_states
        return [self._select(group, source_states, now) for group in self.groups.values()]

    def _unknown(self, group, now, excluded, reasons, interval=None):
        return CanonicalState(id=group.entity+":"+self.profiles[group.primary].capability,
            unit=self.profiles[group.primary].unit, quality="unknown", reasons=reasons,
            excluded_sources=excluded, as_of=now, inferred=True, value_interval=interval,
            resolver_version="sensor-quality-v1", simulated=all(self.profiles[s].simulated for s in group.sources))

    def _select(self, group, states, now):
        candidates, excluded = {}, {}
        for source in group.sources:
            state = states[source]
            if state.quality != "healthy":
                excluded[source] = ",".join(state.reasons)
                continue
            value, bound, calibration, reason = self._adjust(group, source, state.value, now, state.simulated)
            if reason:
                excluded[source] = reason
            else:
                candidates[source] = (state, value, bound, calibration)
        if not candidates:
            self.selected.pop(group.entity, None)
            return self._unknown(group, now, excluded, ["no_valid_qualified_measurement_source"])
        lower = min(value-bound for _, value, bound, _ in candidates.values())
        upper = max(value+bound for _, value, bound, _ in candidates.values())
        if len(candidates) > 1 and upper-lower > group.disagreement_tolerance:
            result = self._unknown(group, now, excluded, ["peer_disagreement_unresolved"], (lower, upper))
            result.evidence_refs = list(dict.fromkeys(ref for state, _, _, _ in candidates.values() for ref in state.evidence_refs))[-32:]
            return result
        previous = self.selected.get(group.entity)
        chosen = previous if previous in candidates else next(iter(candidates))
        if (group.primary in candidates and chosen != group.primary and
                (group.entity not in self.switched or (now-self.switched[group.entity]).total_seconds() >= group.switch_hold_seconds)):
            chosen = group.primary
        if chosen != previous:
            self.switched[group.entity] = now
        self.selected[group.entity] = chosen
        state, value, bound, calibration = candidates[chosen]
        return state.model_copy(update={"id": group.entity+":"+self.profiles[chosen].capability,
            "value": value, "inferred": True, "value_interval": (value-bound, value+bound) if bound else None,
            "selected_source": chosen, "excluded_sources": excluded, "calibration_version": calibration,
            "resolver_version": "sensor-quality-v1", "reasons": ["primary_profile_selected" if chosen == group.primary
                else "relative_calibrated_fallback_not_absolute_truth"]+state.reasons})

    def learn_relative(self, group_id: str, source: str, now: datetime, *, version: str):
        """Return an uninstalled relative profile from paired qualified samples only.

        Caller must review/approve before constructing a new engine with this profile.
        No inference confidence, cached values, aliases or absolute truth is learned.
        """
        group = self.groups[group_id]
        if source not in group.sources or source == group.primary:
            raise ValueError("distinct_group_source_required")
        profile, primary = self.profiles[source], self.profiles[group.primary]
        if (profile.location != primary.location or profile.simulated != primary.simulated
                or not self.history[source].ready or not self.history[group.primary].ready
                or self.history[source].frozen or self.history[group.primary].frozen):
            raise ValueError("calibration_requires_healthy_comparable_sources")
        pairs, used_peer = [], set()
        for sample in self.history[group.primary].samples:
            if (not sample.qualified or not now-timedelta(seconds=primary.ttl_seconds) <= sample.at <= now
                    or self.history[group.primary].valid_since is not None and sample.at < self.history[group.primary].valid_since):
                continue
            peer = next((p for p in self.history[source].samples if p.qualified
                and p.reference not in used_peer and abs((p.at-sample.at).total_seconds()) <= 2
                and p.context == profile.context), None)
            if peer and now-timedelta(seconds=profile.ttl_seconds) <= peer.at <= now:
                if self.history[source].valid_since is None or peer.at >= self.history[source].valid_since:
                    pairs.append((sample, peer))
                    used_peer.add(peer.reference)
        if len(pairs) < 8 or (pairs[-1][0].at-pairs[0][0].at).total_seconds() < 60:
            raise ValueError("insufficient_distinct_calibration_pairs")
        bias = median(peer.value-sample.value for sample, peer in pairs)
        bound = max(abs(peer.value-sample.value-bias) for sample, peer in pairs)+max(primary.resolution, profile.resolution)
        low, high = min(s.value for s, _ in pairs), max(s.value for s, _ in pairs)
        if high-low < max(primary.resolution, profile.resolution)*2:
            raise ValueError("calibration_temperature_coverage_insufficient")
        return RelativeCalibration(source=source, reference=group.primary, source_profile=profile.fingerprint(),
            reference_profile=primary.fingerprint(), context=profile.context, version=version, bias=bias,
            residual_bound=bound, reference_minimum=low, reference_maximum=high, sample_count=len(pairs),
            valid_until=now+timedelta(days=7), simulated=profile.simulated)
