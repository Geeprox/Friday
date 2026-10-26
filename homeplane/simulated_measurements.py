"""Explicit synthetic history for the same quality engine used by home-domain rules.

Loading/refreshing a snapshot never creates a new hardware sample. Fixture history is
only seeded on scenario selection, and cannot be loaded into an HA adapter.
"""
import json
from datetime import timedelta
from pathlib import Path

from .contracts import Capability, Entity, Observation
from .measurement_profiles import DiagnosticReading, MeasurementGroup, MeasurementProfile, RelativeCalibration
from .sensor_quality import SensorQualityEngine

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/homeplane/measurement-quality.json"


class SimulatedMeasurements:
    def __init__(self, entities, scenario, anchor):
        self.history_frames = []
        body = json.loads(FIXTURE.read_text())
        if body.get("simulated") is not True or body.get("schema_version") != 1:
            raise ValueError("simulated_measurement_fixture_required")
        profiles = [MeasurementProfile.model_validate(p) for p in body["profiles"]]
        self.reference_roles = body.get("reference_roles", {})
        if (not set(self.reference_roles) <= {p.source for p in profiles}
                or any(r not in {"unreviewed", "room_reference", "contextual_air"} for r in self.reference_roles.values())):
            raise ValueError("invalid_simulated_reference_roles")
        group = MeasurementGroup.model_validate(body["group"])
        if any(not p.simulated for p in profiles):
            raise ValueError("simulated_profiles_required")
        if any(not any(e.id == p.entity and e.physical_source_id == p.source and e.simulated
                       and any(c.name == p.capability and c.unit == p.unit and c.access == "read"
                               and c.support == "supported" for c in e.capabilities)
                       for e in entities) for p in profiles):
            # Custom simulator inventories must supply their own qualified mapping later.
            self.engine, self.entities, self.rows, self.contexts, self.diagnostics = None, [], [], {}, {}
            return
        config = body["scenarios"][scenario]
        calibrations = []
        if config["calibrated"]:
            fixture = body["relative_calibration"]
            calibrations = [RelativeCalibration.model_validate({**fixture["template"],
                "valid_until": anchor+timedelta(seconds=-1 if scenario == "calibration-expired"
                                               else fixture["valid_for_seconds"])})]
        self.engine = SensorQualityEngine(profiles, [group], calibrations)
        self.entities = [Entity(id=group.entity, name=group.name, kind="sensor", room=group.location,
            physical_source_id="sim:derived:room-temperature", simulated=True, capabilities=[
                Capability(name=profiles[0].capability, access="read", support="supported",
                           unit=profiles[0].unit, verification="simulated", wan_dependency="unknown")])]
        for frame in config["frames"]:
            at = anchor+timedelta(seconds=frame["offset_seconds"])
            self.rows, self.contexts, self.diagnostics = [], {}, {}
            for prefix, profile in zip(("room", "ac"), profiles, strict=True):
                measured = at-timedelta(seconds=frame.get(prefix+"_age_seconds", 0))
                self.rows.append(Observation(entity_id=profile.entity, capability=profile.capability,
                    value=frame[prefix], unit=profile.unit, physical_source_id=profile.source,
                    ingest_source="ha:sim" if prefix == "room" else "geek:sim", measured_at=measured,
                    source_reported_at=measured, received_at=at, integration_written_at=at,
                    freshness_basis="device_sample", cached=frame.get(prefix+"_cached", False),
                    clock_uncertainty_seconds=1, semantics="periodic_sample",
                    transport_health=frame.get(prefix+"_transport", "online"), simulated=True))
                self.contexts[profile.source] = frame.get(prefix+"_context", profile.context)
            if "battery" in frame:
                self.diagnostics[profiles[0].source] = {"battery": DiagnosticReading(value=frame["battery"],
                    measured_at=at-timedelta(seconds=frame["battery_age_seconds"]), freshness_basis="device_sample")}
            self.history_frames.append((at, self.rows, self.contexts, self.diagnostics))
            self.engine.evaluate(self.rows, at, contexts=self.contexts, diagnostics=self.diagnostics)

    def evaluate(self, now):
        if self.engine is None:
            return [], []
        # Only the transport/cache receipt time advances on refresh.
        rows = [row.model_copy(update={"received_at": now, "integration_written_at": now}) for row in self.rows]
        states = self.engine.evaluate(rows, now, contexts=self.contexts, diagnostics=self.diagnostics)
        return rows, states
