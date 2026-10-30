import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .contracts import Entity, Event, ExternalRuleRecord, Observation, Snapshot
from .quality import resolve
from .simulated_measurements import SimulatedMeasurements
from .event_provenance import entity_source_context

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures/homeplane/house.json"
SCENARIOS = {"calibration-training", "baseline", "stale-temperature", "all-sources-bad", "recovered", "transport-gap",
             "calibrated-fallback", "frozen-temperature", "peer-disagreement", "recovery-hold",
             "battery-stale", "wrong-ac-mode", "calibration-expired", "cache-recovery", "watch-window"}


class Simulator:
    def __init__(self, fixture: Path = FIXTURE, *, wall_now=None, monotonic=None):
        self.now = wall_now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.fixture = json.loads(fixture.read_text())
        if self.fixture.get("simulated") is not True:
            raise ValueError("simulator_requires_explicit_simulated_fixture")
        self.source_fixture_identity = hashlib.sha256(json.dumps(
            self.fixture, ensure_ascii=True, sort_keys=True, separators=(',', ':')
        ).encode('utf8')).hexdigest()
        self.entities = [Entity.model_validate(e) for e in self.fixture["entities"]]
        if not all(e.simulated for e in self.entities):
            raise ValueError("simulator_entities_must_be_simulated")
        self.boot_id = str(uuid4())
        self.version = 0
        self.scenario = "baseline"
        self.started_at = self.now()
        self.generation = self.started_at
        self.events: list[Event] = []
        self.last_quality = {}
        self.managed_values = {}
        self.managed_actions = []
        self.set_scenario("baseline")

    def set_scenario(self, name: str):
        if name not in SCENARIOS:
            raise ValueError("unsupported_scenario")
        self.scenario = name
        self.version += 1
        self.generation = self.now()
        self.measurements = SimulatedMeasurements(self.entities, name, self.generation)
        self.events.append(Event(
            source="simulator", kind="observation-gap" if name == "transport-gap" else "state-snapshot",
            correlation_id=str(uuid4()), wall_time=self.generation, monotonic_time=self.monotonic(),
            boot_id=self.boot_id, simulated=True,
            payload={"scenario": name, "reason": "snapshot_is_not_an_arrival_event", "attribution": "simulated"},
        ))
        self.events = self.events[-200:]

    async def snapshot(self) -> Snapshot:
        from .devices import project_simulated
        now, sampled = self.now(), self.generation
        observations = []
        for entity in self.entities:
            capability = entity.capabilities[0]
            periodic = capability.name == "temperature"
            aged = periodic and self.scenario == "all-sources-bad"
            aged |= entity.id == "sensor.living.temperature" and self.scenario == "stale-temperature"
            measured = sampled - timedelta(hours=8) if aged else sampled
            observations.append(Observation(
                entity_id=entity.id, capability="power" if entity.kind == "device" and not periodic else capability.name,
                value=self.managed_values.get((entity.id, "power" if entity.kind == "device" and not periodic else capability.name),
                                              24.5 if periodic else "off" if entity.id.startswith("light.") else "closed"),
                unit=capability.unit, physical_source_id=entity.physical_source_id,
                ingest_source="ha:sim" if entity.id != "climate.living" else "geek:sim",
                measured_at=measured if periodic else None, integration_written_at=now,
                source_reported_at=measured if periodic else None, received_at=now,
                freshness_basis="device_sample" if periodic else "cache_write", cached=aged,
                clock_uncertainty_seconds=1, semantics="periodic_sample" if periodic else "latched_state",
                transport_health="offline" if self.scenario == "transport-gap" else "online", simulated=True,
            ))
        for (entity_id, name), value in self.managed_values.items():
            if any(o.entity_id == entity_id and o.capability == name for o in observations):
                continue
            entity = next(e for e in self.entities if e.id == entity_id)
            observations.append(Observation(entity_id=entity_id,capability=name,value=value,
                physical_source_id=entity.physical_source_id,ingest_source="simulator:managed-actuator",
                received_at=now,integration_written_at=now,freshness_basis="cache_write",measured_at=None,
                semantics="latched_state",transport_health="offline" if self.scenario == "transport-gap" else "online",
                simulated=True))
        raw = self.fixture["external_graph_raw"]
        rules = [ExternalRuleRecord(
            id="geek:sim:arrival", source="geek:sim", raw=raw,
            fingerprint=hashlib.sha256(raw.encode()).hexdigest(), readable=True,
            coverage_note="Synthetic Graph preserved byte-for-byte; unknown node semantics unsupported.", simulated=True,
        ), ExternalRuleRecord(
            id="mijia:coverage-gap", source="mijia:sim", raw=None, fingerprint=None,
            coverage_note="Ordinary Mijia rules are not enumerable here. A central lease cannot stop unknown writers.",
            simulated=True,
        )]
        states = [resolve(o, now) for o in observations]
        measured_rows, canonical = self.measurements.evaluate(now)
        engine = self.measurements.engine
        if engine:
            replaced = {o.entity_id for o in measured_rows}
            observations = [o for o in observations if o.entity_id not in replaced]+measured_rows
            states = [s for s in states if not any(s.id.startswith(e+":") for e in replaced)]
            states += list(engine.source_states.values())+canonical
            for state in canonical:
                signature = (state.quality, state.selected_source, state.value_interval, tuple(state.reasons))
                if self.last_quality.get(state.id) != signature:
                    sources = [entity for entity in self.measurements.entities if state.id == entity.id
                               or any(state.id == entity.id + ':' + capability.name for capability in entity.capabilities)]
                    self.events.append(Event(source="simulator-quality", kind="quality-change", correlation_id=str(uuid4()),
                        source_context=entity_source_context(self, sources[0]) if len(sources) == 1 else None,
                        wall_time=now, monotonic_time=self.monotonic(), boot_id=self.boot_id, simulated=True,
                        payload={"entity_id": state.id, "quality": state.quality, "selected_source": state.selected_source,
                                 "reasons": state.reasons, "scenario": self.scenario,
                                 "evidence_refs": state.evidence_refs,
                                 "excluded_sources": state.excluded_sources,
                                 "calibration_version": state.calibration_version,
                                 "value_interval": state.value_interval,
                                 "attribution": "synthetic_measurement_history_not_real_samples"}))
                    self.last_quality[state.id] = signature
            self.events = self.events[-200:]
        return Snapshot(
            mode="simulator", version=f"sim-{self.boot_id}-{self.version}", generated_at=now,
            entities=self.entities, observations=observations, states=states, rules=rules, events=self.events,
            devices=project_simulated(self.entities),
            derived_entities=self.measurements.entities, quality_sources=engine.last_sources if engine else [],
            simulator_scenario=self.scenario,
            limitations=["SIMULATED data; no real device/protocol verification.",
                         "Numeric history and relative calibration are explicit synthetic fixtures; refresh is not new sampling.",
                         "AC return-air fallback requires a matching calibrated profile; no hardware calibration is claimed.",
                         "External rules remain partially invisible; own action policy cannot intercept them.",
                         "No offline hardware claim. All real writes disabled."],
        )

    async def close(self):
        pass

    def contact_event(self, value):
        """Explicit synthetic input, never a guessed HA presence transition."""
        if value not in {"open", "closed"}:
            raise ValueError("unsupported_simulated_contact")
        entity = next((e for e in self.entities if e.id == "binary_sensor.door"), None)
        if entity is None or not any(c.name == "contact.transition" and c.access == "event" for c in entity.capabilities):
            raise ValueError("unregistered_simulated_contact")
        old = self.managed_values.get((entity.id,"contact"),"closed")
        if value == old:
            return None
        self.managed_values[(entity.id,"contact")] = value
        self.version += 1
        event = Event(source="simulator:explicit-contact",kind="state-transition",correlation_id=str(uuid4()),
            source_context=entity_source_context(self, entity),
            wall_time=self.now(),monotonic_time=self.monotonic(),boot_id=self.boot_id,simulated=True,
            payload={"entity_id":entity.id,"capability":"contact","from":old,"to":value,"trigger_eligible":True})
        self.events.append(event)
        self.events=self.events[-200:]
        return event

    def apply_managed(self, command, *, run_id):
        """An absolute fake actuator with observable state, not a real Action Gateway."""
        entity = next((e for e in self.entities if e.id == command["target"]),None)
        if entity is None or not any(c.name == command["type"] and c.access == "write" and c.support == "supported"
                                     for c in entity.capabilities):
            raise ValueError("unsupported_simulated_actuator")
        desired = command["desired"]
        if desired.get("power") not in {"on","off"} or set(desired)-{"power","brightness"}:
            raise ValueError("unsupported_simulated_parameters")
        if "brightness" in desired:
            cap = next(c for c in entity.capabilities if c.name == "light.set")
            if (type(desired["brightness"]) is not int or cap.minimum is None or cap.maximum is None
                    or not cap.minimum <= desired["brightness"] <= cap.maximum or desired["power"] != "on"):
                raise ValueError("unsupported_simulated_brightness")
        for name, value in desired.items():
            self.managed_values[(entity.id,name)] = value
        self.version += 1
        self.managed_actions.append({"run_id":run_id,"command":command,"simulated":True})
        self.managed_actions=self.managed_actions[-200:]
