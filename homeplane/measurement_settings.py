"""Private, source-bound local quality configuration. No model/device/media clients.

Only Celsius temperature profiles are editable in this first vertical path.
Samples and recovery history stay in bounded RAM; persisted metadata cannot turn
HA cache writes into hardware samples or prove independent source identity.
"""
import hashlib
import json
import sqlite3
import time
from datetime import timedelta
from uuid import uuid4

from .contracts import CanonicalState, Capability, Entity, Event
from .measurement_enrollment import MeasurementEnrollment
from .measurement_profiles import MeasurementProfile
from .measurement_settings_contracts import (
    MeasurementConfiguration,
    MeasurementSettingsView,
    MeasurementSource,
    SavedMeasurement,
)
from .sensor_quality import SensorQualityEngine
from .simulator import Simulator


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_key(entity):
    return digest([entity.id, entity.physical_source_id, "temperature"])


def binding_fingerprint(entity):
    return digest([entity.id, entity.physical_source_id, entity.room, entity.simulated,
                   sorted((b.source, b.external_id) for b in entity.bindings),
                   sorted((c.name, c.access, c.unit, c.support) for c in entity.capabilities
                          if c.name == "temperature")])


class MeasurementSettings:
    def __init__(self, store):
        self.store = store
        self.db = store.connection
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS measurement_settings (realm TEXT PRIMARY KEY, body TEXT)")
        self.engine = None
        self.signature = None
        self.adapter_token = None
        self.epoch = 0
        self.event_signatures = {}
        self.boot_id = str(uuid4())
        self.enrollment = MeasurementEnrollment(self)

    def _realm(self, adapter, snapshot):
        if isinstance(adapter, Simulator) and snapshot.mode == "simulator":
            return digest(["simulated-fixture", sorted((e.id, e.physical_source_id) for e in adapter.entities)])
        from .ha import HAReadOnlyAdapter
        if isinstance(adapter, HAReadOnlyAdapter) and snapshot.mode == "ha-read-only":
            return digest(["ha-read-only", adapter.instance_id, adapter.url])
        raise ValueError("unsupported_measurement_source")

    def _config(self, realm):
        row = self.db.execute("SELECT body FROM measurement_settings WHERE realm=?", (realm,)).fetchone()
        if row:
            if len(row[0].encode()) > 65536:
                raise ValueError("measurement_profile_budget")
            return MeasurementConfiguration.model_validate_json(row[0])
        # Deterministic empty revision; no writes during reads and no global grant.
        return MeasurementConfiguration(revision="empty-"+realm[:32])

    @staticmethod
    def _inventory(snapshot):
        candidates = [e for e in snapshot.entities if len(e.id) <= 128 and len(e.physical_source_id) <= 128
                      and any(c.name == "temperature" and c.unit == "°C" and c.access == "read"
                              and c.support == "supported" for c in e.capabilities)]
        candidates.sort(key=lambda e: e.id)
        # Conflicting identities cannot be edited or bound by list position.
        ids = [e.id for e in candidates]
        candidates = [e for e in candidates if ids.count(e.id) == 1]
        return candidates[:256], max(0, len(candidates)-256)

    @staticmethod
    def _fixture(adapter):
        return adapter.measurements.engine if isinstance(adapter, Simulator) else None

    def view(self, adapter, snapshot):
        realm = self._realm(adapter, snapshot)
        config = self._config(realm)
        records = {r.source_key: r for r in config.sources}
        candidates, omitted = self._inventory(snapshot)
        fixture = self._fixture(adapter)
        sources = []
        profiles = dict(fixture.profiles) if fixture else {}
        for entity in candidates:
            key, binding = source_key(entity), binding_fingerprint(entity)
            saved = records.get(key)
            valid = saved is None or saved.binding_fingerprint == binding
            profile = (saved.profile if valid else None) if saved else fixture.profiles.get(entity.physical_source_id) if fixture else None
            state = next((s for s in snapshot.states if s.id == entity.id+":temperature"), None)
            if profile is not None:
                profiles[profile.source] = profile
            sources.append(MeasurementSource(source_key=key, binding_fingerprint=binding,
                entity_id=entity.id, physical_source_id=entity.physical_source_id, name=entity.name, room=entity.room,
                configured=bool(saved and saved.edited and valid), binding_valid=valid,
                profile=profile, excluded_until=saved.excluded_until if saved else None,
                excluded=bool(saved and saved.excluded_until and snapshot.generated_at < saved.excluded_until),
                state=state, simulated=entity.simulated, identity_fingerprint=self.enrollment.identity(entity, snapshot),
                representativeness=self.enrollment.role(adapter, snapshot, entity, saved if valid else None)))
        return MeasurementSettingsView(realm=realm, revision=config.revision, generated_at=snapshot.generated_at,
            simulated=snapshot.mode == "simulator", sources=sources, omitted_count=omitted,
            groups=self.enrollment.views(adapter, snapshot, config, profiles),
            limits=["Celsius temperature profiles only; at most 32 local profiles and 256 listed sources.",
                    "Placement/reporting/context are user declarations, not sample-time or identity evidence.",
                    "Profile/binding/exclusion changes reset qualified recovery; refresh is not a new sample.",
                    "Distinct upstream source identifiers are required for a multi-source group; physical independence remains unverified.",
                    "Calibration is relative to a reviewed room reference, uses qualified paired RAM samples, and needs explicit approval.",
                    "Settings metadata persists locally until replaced; sample history is RAM only."])

    def change(self, adapter, snapshot, request, operation):
        realm = self._realm(adapter, snapshot)
        config = self._config(realm)
        if request.realm != realm or request.expected_revision != config.revision:
            raise ValueError("source_conflict")
        candidates, _ = self._inventory(snapshot)
        entity = next((e for e in candidates if source_key(e) == request.source_key), None)
        if entity is None or request.binding_fingerprint != binding_fingerprint(entity):
            raise ValueError("source_conflict")
        records = {r.source_key: r for r in config.sources}
        previous = records.get(request.source_key)
        if operation == "resume" and previous is None:
            raise ValueError("source_conflict")
        if len(records) >= 32 and previous is None:
            raise ValueError("measurement_profile_budget")
        fixture = self._fixture(adapter)
        base = previous.profile if previous and previous.binding_fingerprint == request.binding_fingerprint else (
            fixture.profiles.get(entity.physical_source_id) if fixture else None)
        if base is None:
            # Unknown context is never used as evidence of an actual AC mode.
            base = MeasurementProfile(source=entity.physical_source_id, entity=entity.id, location=entity.room or "unassigned",
                placement="unconfigured", reporting="periodic_sample", context="unknown", version="unconfigured-v1",
                simulated=entity.simulated)
        base = MeasurementProfile.model_validate({**base.model_dump(), "entity":entity.id,
            "source":entity.physical_source_id, "location":entity.room or "unassigned", "simulated":entity.simulated})
        edited = bool(previous and previous.edited and previous.binding_fingerprint == request.binding_fingerprint)
        role = self.enrollment.role(adapter, snapshot, entity, previous)
        excluded_until = previous.excluded_until if previous else None
        if operation == "edit":
            base = MeasurementProfile.model_validate({**base.model_dump(), **request.fields.model_dump(exclude={"schema_version", "expected_context", "representativeness"}),
                "context":request.fields.expected_context, "version":"local-"+str(uuid4())})
            from .measurement_enrollment import contextual_device
            role = "contextual_air" if contextual_device(entity, snapshot) else request.fields.representativeness
            edited = True
        elif operation == "exclude":
            excluded_until = snapshot.generated_at+timedelta(seconds=request.duration_seconds)
        elif operation == "resume":
            excluded_until = None
        else:
            raise ValueError("invalid_measurement_operation")
        records[request.source_key] = SavedMeasurement(source_key=request.source_key,
            binding_fingerprint=request.binding_fingerprint, profile=base, edited=edited, excluded_until=excluded_until,
            representativeness=role)
        if len(set(fixture.profiles if fixture else {}) | {r.profile.source for r in records.values()}) > 32:
            raise ValueError("measurement_profile_budget")
        updated = config.model_copy(update={"revision":str(uuid4()), "sources":list(records.values())})
        self.persist(realm, request.expected_revision, updated, snapshot, operation, entity.id)
        self.enrollment.close()

    def persist(self, realm, expected_revision, updated, snapshot, operation, entity_id):
        # model_copy does not validate collection budgets; validate before storage.
        updated = MeasurementConfiguration.model_validate(updated.model_dump())
        body = updated.model_dump_json()
        if len(body.encode()) > 65536:
            raise ValueError("measurement_profile_budget")
        with self.db:
            row = self.db.execute("SELECT body FROM measurement_settings WHERE realm=?", (realm,)).fetchone()
            # Storage CAS also protects two service objects on the same local database.
            if (MeasurementConfiguration.model_validate_json(row[0]).revision if row else "empty-"+realm[:32]) != expected_revision:
                raise ValueError("source_conflict")
            if row is None and self.db.execute("SELECT count(*) FROM measurement_settings").fetchone()[0] >= 8:
                raise ValueError("measurement_realm_budget")
            self.db.execute("INSERT INTO measurement_settings VALUES (?,?) ON CONFLICT(realm) DO UPDATE SET body=excluded.body", (realm, body))
            event = Event(source="measurement-settings", kind="quality-change", correlation_id=str(uuid4()),
                wall_time=snapshot.generated_at, received_at=snapshot.generated_at, time_basis="local_event",
                monotonic_time=time.monotonic(), boot_id=self.boot_id, simulated=snapshot.mode == "simulator",
                payload={"entity_id":entity_id+":temperature", "configuration_operation":operation,
                         "configuration_revision":updated.revision, "hardware_write_attempted":False,
                         "calibration_invalidated":operation in {"edit", "group"}})
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (event.event_id, event.wall_time.isoformat(), event.model_dump_json()))

    def apply(self, adapter, snapshot):
        try:
            return self._apply(adapter, snapshot)
        except (ValueError, sqlite3.DatabaseError):
            # A broken numeric configuration cannot disable unrelated latch
            # controls. Fail temperature rules closed and keep raw evidence.
            self.engine, self.signature = None, None
            self.enrollment.close()
            numeric = {e.id+":temperature" for e in snapshot.entities+snapshot.derived_entities
                       if any(c.name == "temperature" for c in e.capabilities)}
            states = [s.model_copy(update={"quality":"unknown", "value":None, "value_interval":None,
                       "selected_source":None, "valid_until":None, "reasons":["measurement_configuration_unavailable"]})
                      if s.id in numeric else s for s in snapshot.states]
            return snapshot.model_copy(update={"states":states,"quality_sources":[],
                "version":snapshot.version+":quality:configuration-unavailable"})

    def _apply(self, adapter, snapshot):
        realm = self._realm(adapter, snapshot)
        config = self._config(realm)
        fixture = self._fixture(adapter)
        token = (id(adapter), id(adapter.measurements) if isinstance(adapter, Simulator) else None)
        selected_scenario = self.adapter_token is not None and self.adapter_token != token
        self.adapter_token = token
        self.enrollment._prune(snapshot.generated_at)
        if not config.sources and not config.groups:
            self.engine, self.signature = None, None
            self.enrollment.close()
            return snapshot
        candidates, _ = self._inventory(snapshot)
        entities = {source_key(e):e for e in candidates}
        profiles = dict(fixture.profiles) if fixture else {}
        exclusions, invalid = set(), set()
        for record in config.sources:
            entity = entities.get(record.source_key)
            if entity is None or binding_fingerprint(entity) != record.binding_fingerprint:
                invalid.add(record.profile.source)
            else:
                if (record.profile.source != entity.physical_source_id or record.profile.entity != entity.id
                        or record.profile.capability != "temperature" or record.profile.unit != "°C"
                        or record.profile.simulated != entity.simulated
                        or record.profile.location != (entity.room or "unassigned")):
                    raise ValueError("invalid_persisted_measurement_binding")
                profiles[record.profile.source] = record.profile
            if record.excluded_until and snapshot.generated_at < record.excluded_until:
                exclusions.add(record.profile.source)
        # Missing registered sources have no eligible observation. Never restore a
        # fixture primary after an edited binding becomes invalid.
        for record in config.sources:
            if record.profile.source not in profiles:
                profiles[record.profile.source] = record.profile
        all_groups = dict(fixture.groups) if fixture else {}
        all_groups.update({g.group.entity:g.group for g in config.groups})
        configured = {g.group.entity:g for g in config.groups}
        groups = [g for g in all_groups.values() if profiles[g.primary].location == g.location
                  and (g.entity not in configured or self.enrollment.group_valid(adapter, snapshot, configured[g.entity], profiles, config))]
        # A managed group never inherits an authored Simulator calibration.
        calibrations = [c for c in fixture.calibrations.values()
                        if not any(g.group.primary == c.reference and c.source in g.group.sources for g in configured.values())] if fixture else []
        calibrations += [c.calibration for c in config.calibrations if c.group_entity in configured
                        and configured[c.group_entity].group in groups
                        and c.group_fingerprint == self.enrollment.group_fingerprint(configured[c.group_entity], profiles)]
        signature = (realm, config.revision, token, tuple(sorted(exclusions)),
                     tuple(sorted(invalid)), tuple(sorted(binding_fingerprint(e) for e in candidates)))
        if signature != self.signature:
            carry = self.enrollment.approval_carry
            if (carry and carry[:2] == (realm, config.revision) and carry[2] is not None
                    and carry[2][:1]+carry[2][2:] == signature[:1]+signature[2:]
                    and carry[3] is self.engine):
                # The exact reviewed approval alone may retain still-qualified
                # RAM history. Any source/configuration/lease change resets it.
                self.engine.calibrations = {(c.reference, c.source):c for c in calibrations}
            else:
                self.engine = SensorQualityEngine(list(profiles.values()), groups, calibrations)
            self.enrollment.close()
            self.signature = signature
            self.epoch += 1
            # Only an explicitly selected *new* Simulator scenario may seed its
            # labelled synthetic history. Edits/resume/expiry never reuse it.
            if selected_scenario and isinstance(adapter, Simulator):
                for at, rows, contexts, diagnostics in adapter.measurements.history_frames:
                    self.engine.evaluate(rows, at, contexts=contexts, diagnostics=diagnostics,
                                         excluded_sources=(exclusions | invalid) & profiles.keys())
        engine = self.engine
        managed = {(p.entity, p.capability, p.source) for p in profiles.values()}
        observations = [o for o in snapshot.observations if (o.entity_id, o.capability, o.physical_source_id) in managed]
        contexts = adapter.measurements.contexts if isinstance(adapter, Simulator) else {}
        diagnostics = adapter.measurements.diagnostics if isinstance(adapter, Simulator) else {}
        canonical = engine.evaluate(observations, snapshot.generated_at, contexts=contexts, diagnostics=diagnostics,
                                    excluded_sources=(exclusions | invalid) & profiles.keys())
        canonical += [CanonicalState(id=g.entity+":"+profiles[g.primary].capability,
            unit=profiles[g.primary].unit, quality="unknown", reasons=["measurement_group_binding_changed" if g.entity in configured else "measurement_group_location_changed"],
            as_of=snapshot.generated_at, inferred=True, simulated=snapshot.mode == "simulator")
            for g in all_groups.values() if g not in groups]
        for source in invalid:
            state = engine.source_states[source]
            state.reasons = ["measurement_binding_changed"]
            next(e for e in engine.last_sources if e.physical_source_id == source).reasons = state.reasons
        replace_ids = {p.entity+":"+p.capability for p in profiles.values()} | {s.id for s in canonical}
        states = [s for s in snapshot.states if s.id not in replace_ids]+list(engine.source_states.values())+canonical
        quality_sources = [s for s in snapshot.quality_sources if s.physical_source_id not in profiles]+engine.last_sources
        events = [e for e in snapshot.events if not (e.source == "simulator-quality" and e.payload.get("entity_id") in replace_ids)]
        for state in list(engine.source_states.values())+canonical:
            key = (realm, state.id)
            current = digest([state.quality, state.reasons, state.selected_source, state.calibration_version, state.excluded_sources])
            if self.event_signatures.get(key) != current:
                events.append(Event(source="measurement-settings", kind="quality-change", correlation_id=str(uuid4()),
                    wall_time=snapshot.generated_at, received_at=snapshot.generated_at, time_basis="local_event",
                    monotonic_time=time.monotonic(), boot_id=self.boot_id, simulated=state.simulated,
                    payload={"entity_id":state.id, "quality":state.quality, "reasons":state.reasons,
                             "selected_source":state.selected_source, "configuration_revision":config.revision,
                             "excluded_sources":state.excluded_sources, "attribution":"local_quality_projection"}))
                self.event_signatures[key] = current
        if len(self.event_signatures) > 512:
            self.event_signatures = {k:v for k,v in self.event_signatures.items() if k[0] == realm}
        derived = {e.id:e for e in snapshot.derived_entities}
        for saved in config.groups:
            g = saved.group
            derived[g.entity] = Entity(id=g.entity, name=g.name, kind="sensor", room=g.location,
                physical_source_id="derived:"+digest([realm, g.entity])[:32], simulated=snapshot.mode == "simulator",
                capabilities=[Capability(name="temperature", access="read", support="supported", unit="°C",
                    verification="simulated" if snapshot.mode == "simulator" else "unknown", wan_dependency="unknown")])
        return snapshot.model_copy(update={"states":states, "quality_sources":quality_sources, "events":events[-200:],
            "derived_entities":list(derived.values()),
            "version":snapshot.version+":quality:"+config.revision+":"+str(self.epoch)})
