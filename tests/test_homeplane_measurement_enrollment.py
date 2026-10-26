"""Reviewed relative calibration from explicit simulated history; no hardware writes."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeplane.api import create_app
from homeplane.contracts import Binding, PhysicalDevice
from homeplane.measurement_settings_contracts import (
    ApproveRelativeCalibration,
    EditMeasurement,
    EditMeasurementGroup,
    ExcludeMeasurement,
    PreviewRelativeCalibration,
)
from tests.test_homeplane_measurement_settings import FIELDS, ORIGIN, START, request, setup, state


def group_request(view):
    return EditMeasurementGroup(
        realm=view.realm,
        expected_revision=view.revision,
        name="SIMULATED living temperature",
        primary_key=next(s.source_key for s in view.sources if s.entity_id == "sensor.living.temperature"),
        members=[
            {
                "source_key": s.source_key,
                "binding_fingerprint": s.binding_fingerprint,
                "identity_fingerprint": s.identity_fingerprint,
            }
            for s in view.sources
        ],
    )


def preview_request(view):
    return PreviewRelativeCalibration(
        realm=view.realm,
        expected_revision=view.revision,
        group_entity=view.groups[0].entity,
        source_key=next(s.source_key for s in view.sources if s.entity_id == "climate.living"),
    )


def approval(preview):
    return ApproveRelativeCalibration(
        realm=preview.realm,
        expected_revision=preview.revision,
        candidate_id=preview.candidate_id,
        fingerprint=preview.fingerprint,
        reviewed_relative_only=True,
    )


async def training(tmp_path, clock):
    sim, store, core = setup(tmp_path, clock)
    snap = await core.snapshot()
    view = core.measurements.view(sim, snap)
    core.measurements.enrollment.edit_group(sim, snap, group_request(view))
    await core.snapshot()  # A group edit resets recovery, without replaying history.
    sim.set_scenario("calibration-training")  # Explicit simulated data selection.
    snap = await core.snapshot()
    view = core.measurements.view(sim, snap)
    return sim, store, core, snap, view


@pytest.mark.asyncio
async def test_preview_review_install_fallback_interval_and_rule_use_same_core(tmp_path):
    from homeplane.rule_compiler import evaluate_rule
    from tests.test_homeplane_measurement_integration import numeric_rule
    from tests.test_homeplane_measurement_integration import request as trial

    clock = [START]
    sim, store, core, snap, view = await training(tmp_path, clock)
    try:
        enroll = core.measurements.enrollment
        before = core.measurements._config(view.realm).model_dump_json()
        preview = enroll.preview(sim, snap, preview_request(view))
        assert not preview.installed and not preview.sample_history_persisted
        assert preview.calibration.simulated and preview.calibration.relative_only
        assert preview.calibration.bias == 2 and preview.calibration.sample_count == 13
        assert preview.calibration.reference_minimum == 23.3 and preview.calibration.reference_maximum == 24.5
        assert core.measurements._config(view.realm).model_dump_json() == before
        assert not core.measurements.engine.calibrations
        enroll.approve(sim, snap, approval(preview))
        approved = await core.snapshot()
        v = core.measurements.view(sim, approved)
        assert v.groups[0].calibration_status == "active"
        assert (
            state(approved).quality == "healthy"
        )  # Exact approval retains qualified RAM, not fake new samples.
        assert len(core.measurements.engine.history["sim:ac"].samples) == 16
        sim.set_scenario("calibrated-fallback")
        fallback = await core.snapshot()
        room = state(fallback, "room.living")
        assert room.quality == "healthy" and room.selected_source == "sim:ac"
        assert room.calibration_version == preview.calibration.version
        assert room.calibration_version != "SIMULATED-relative-v1"
        assert room.value_interval == pytest.approx((24.4, 24.6))
        assert (
            evaluate_rule(numeric_rule(24.5), fallback, trial(fallback, START), now=START).outcome
            == "suppressed"
        )
        assert (
            evaluate_rule(numeric_rule(24), fallback, trial(fallback, START), now=START).outcome == "matched"
        )
        assert not sim.managed_actions  # Evaluation and calibration do not execute commands.
        persisted = core.measurements.db.execute("SELECT body FROM measurement_settings").fetchone()[0]
        assert '"samples":' not in persisted and "sample:" not in persisted
    finally:
        store.close()


def test_api_preview_approval_cas_origin_and_cannot_inject_samples(tmp_path):
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app, base_url="http://127.0.0.1:8740") as c:
        from homeplane.measurement_settings_contracts import MeasurementSettingsView

        view = MeasurementSettingsView.model_validate(c.get("/api/settings/measurements").json())
        group = group_request(view).model_dump(mode="json")
        assert c.post("/api/settings/measurements/group", json=group).status_code == 403
        assert c.post("/api/settings/measurements/group", json=group, headers=ORIGIN).status_code == 200
        assert c.post("/api/settings/measurements/group", json=group, headers=ORIGIN).status_code == 409
        c.post("/api/simulator/scenario/calibration-training", headers=ORIGIN)
        view = MeasurementSettingsView.model_validate(c.get("/api/settings/measurements").json())
        req = preview_request(view).model_dump(mode="json")
        assert (
            c.post(
                "/api/settings/measurements/calibration/preview",
                json={**req, "samples": [{"value": 24}]},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        response = c.post("/api/settings/measurements/calibration/preview", json=req, headers=ORIGIN)
        assert response.status_code == 200, response.json()
        from homeplane.measurement_settings_contracts import CalibrationPreview

        preview = CalibrationPreview.model_validate(response.json())
        approve = approval(preview).model_dump(mode="json")
        assert (
            c.post(
                "/api/settings/measurements/calibration/approve",
                json={**approve, "reviewed_relative_only": False},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/settings/measurements/calibration/approve",
                json={**approve, "calibration": preview.calibration.model_dump(mode="json")},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        assert (
            c.post("/api/settings/measurements/calibration/approve", json=approve, headers=ORIGIN).status_code
            == 200
        )
        assert (
            c.post("/api/settings/measurements/calibration/approve", json=approve, headers=ORIGIN).status_code
            == 409
        )
        assert not app.state.core.adapter.managed_actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["profile", "exclude", "new_sample", "context", "expiry", "scenario", "room", "restart"]
)
async def test_preview_is_rejected_after_evidence_or_scope_changes(tmp_path, change):
    clock = [START]
    sim, store, core, snap, view = await training(tmp_path, clock)
    try:
        service = core.measurements.enrollment
        preview = service.preview(sim, snap, preview_request(view))
        if change == "profile":
            index = next(i for i, s in enumerate(view.sources) if s.entity_id == "sensor.living.temperature")
            core.measurements.change(
                sim,
                snap,
                EditMeasurement(
                    **request(view, index, fields={**FIELDS, "representativeness": "room_reference"})
                ),
                "edit",
            )
        elif change == "exclude":
            core.measurements.change(
                sim, snap, ExcludeMeasurement(**request(view, 0, duration_seconds=60)), "exclude"
            )
        elif change == "new_sample":
            clock[0] += timedelta(seconds=10)
            sim.measurements.rows = [
                o.model_copy(update={"measured_at": clock[0], "source_reported_at": clock[0]})
                for o in sim.measurements.rows
            ]
        elif change == "context":
            sim.measurements.contexts["sim:ac"] = "cooling"
        elif change == "expiry":
            clock[0] += timedelta(seconds=121)
        elif change == "scenario":
            sim.set_scenario("baseline")
        elif change == "room":
            sim.entities = [
                e.model_copy(update={"room": "other-room"}) if e.id == "sensor.living.temperature" else e
                for e in sim.entities
            ]
        elif change == "restart":
            from homeplane.measurement_settings import MeasurementSettings

            core.measurements = MeasurementSettings(store)
            service = core.measurements.enrollment
        current = await core.snapshot()
        with pytest.raises(ValueError):
            service.approve(sim, current, approval(preview))
        assert not core.measurements._config(view.realm).calibrations
        assert not sim.managed_actions
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_expiry_group_change_and_role_change_disable_approved_fallback(tmp_path):
    from homeplane.measurement_settings import MeasurementSettings

    clock = [START]
    sim, store, core, snap, view = await training(tmp_path, clock)
    try:
        preview = core.measurements.enrollment.preview(sim, snap, preview_request(view))
        core.measurements.enrollment.approve(sim, snap, approval(preview))
        await core.snapshot()
        core.measurements = MeasurementSettings(store)
        restarted = await core.snapshot()
        assert state(restarted, "room.living").quality == "unknown"
        assert len(core.measurements.engine.history["sim:ac"].samples) == 1
        # Stored calibration is metadata only; real sample history is not restored.
        assert core.measurements.view(sim, restarted).groups[0].calibration_status == "active"
        clock[0] += timedelta(days=8)
        sim.set_scenario("calibrated-fallback")
        expired = await core.snapshot()
        assert core.measurements.view(sim, expired).groups[0].calibration_status == "expired"
        assert state(expired, "room.living").quality == "unknown"
        sim.set_scenario("calibration-training")
        snap = await core.snapshot()
        view = core.measurements.view(sim, snap)
        index = next(i for i, s in enumerate(view.sources) if s.entity_id == "sensor.living.temperature")
        core.measurements.change(
            sim,
            snap,
            EditMeasurement(
                **request(view, index, fields={**FIELDS, "representativeness": "contextual_air"})
            ),
            "edit",
        )
        invalid = await core.snapshot()
        v = core.measurements.view(sim, invalid)
        assert not v.groups[0].binding_valid
        assert "measurement_group_binding_changed" in state(invalid, "room.living").reasons
        with pytest.raises(ValueError, match="room_reference_position_required"):
            core.measurements.enrollment.edit_group(sim, invalid, group_request(v))
        assert not sim.managed_actions
    finally:
        store.close()


@pytest.mark.asyncio
async def test_current_bad_diagnostics_and_flat_coverage_do_not_qualify(tmp_path):
    from homeplane.measurement_profiles import DiagnosticReading

    clock = [START]
    sim, store, core, snap, view = await training(tmp_path, clock)
    try:
        sim.measurements.diagnostics = {
            "sim:thermometer": {
                "battery": DiagnosticReading(value=10, measured_at=START, freshness_basis="device_sample")
            }
        }
        snap = await core.snapshot()
        with pytest.raises(ValueError, match="healthy_comparable"):
            core.measurements.enrollment.preview(sim, snap, preview_request(view))
        sim.set_scenario("baseline")
        snap = await core.snapshot()
        # Refreshing the same four samples never creates 8 paired points.
        for _ in range(10):
            snap = await core.snapshot()
        with pytest.raises(ValueError, match="insufficient_distinct"):
            core.measurements.enrollment.preview(sim, snap, preview_request(view))
        for i in range(12):
            clock[0] += timedelta(seconds=10)
            sim.measurements.rows = [
                o.model_copy(update={"measured_at": clock[0]}) for o in sim.measurements.rows
            ]
            snap = await core.snapshot()
        with pytest.raises(ValueError, match="temperature_coverage_insufficient"):
            core.measurements.enrollment.preview(sim, snap, preview_request(view))
    finally:
        store.close()


@pytest.mark.asyncio
async def test_upstream_identifier_aliases_and_generic_registry_are_not_independent(tmp_path):
    from homeplane.measurement_settings import binding_fingerprint, source_key
    from homeplane.measurement_settings_contracts import MeasurementConfiguration, SavedMeasurement

    clock = [START]
    sim, store, core = setup(tmp_path, clock)
    try:
        snap = await core.snapshot()
        entities = [
            e.model_copy(
                update={
                    "simulated": False,
                    "kind": "sensor",
                    "capabilities": [c for c in e.capabilities if c.name == "temperature"],
                }
            )
            for e in snap.entities
            if any(c.name == "temperature" for c in e.capabilities)
        ]
        devices = [
            PhysicalDevice(
                id="SIMULATED-real-identity-fixture",
                name="SIMULATED shared DID",
                kind="sensor",
                room="living-room",
                function_ids=[e.id for e in entities],
                bindings=[Binding(source="xiaomi-identity", external_id="cn_123")],
                identity_basis="xiaomi_identifier",
            )
        ]
        real = snap.model_copy(update={"mode": "ha-read-only", "entities": entities, "devices": devices})
        realm = "a" * 64
        # Synthetic HA-like identity snapshot; no network or account credentials.
        core.measurements._realm = lambda *_: realm
        saved = [
            SavedMeasurement(
                source_key=source_key(e),
                binding_fingerprint=binding_fingerprint(e),
                edited=True,
                representativeness="room_reference",
                profile=sim.measurements.engine.profiles[e.physical_source_id].model_copy(
                    update={"simulated": False}
                ),
            )
            for e in entities
        ]
        config = MeasurementConfiguration(revision="SIMULATED-real-r1", sources=saved)
        core.measurements.persist(realm, "empty-" + realm[:32], config, real, "edit", "SIMULATED")
        view = core.measurements.view(object(), real)
        primary = view.sources[0].source_key
        req = EditMeasurementGroup(
            realm=realm,
            expected_revision=view.revision,
            name="SIMULATED shared functions",
            primary_key=primary,
            members=[
                {
                    "source_key": s.source_key,
                    "binding_fingerprint": s.binding_fingerprint,
                    "identity_fingerprint": s.identity_fingerprint,
                }
                for s in view.sources
            ],
        )
        with pytest.raises(ValueError, match="aliased_measurement_sources"):
            core.measurements.enrollment.edit_group(object(), real, req)
        generic = real.model_copy(
            update={
                "devices": [
                    devices[0].model_copy(update={"identity_basis": "ha_device_registry", "bindings": []})
                ]
            }
        )
        req = req.model_copy(
            update={"members": [m.model_copy(update={"identity_fingerprint": None}) for m in req.members]}
        )
        with pytest.raises(ValueError, match="independent_source_identity_required"):
            core.measurements.enrollment.edit_group(object(), generic, req)
        assert not core.measurements._config(realm).groups
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ha_single_source_requires_review_and_still_cannot_calibrate_cache(tmp_path):
    from homeplane.core import HomeCore
    from homeplane.measurement_settings import MeasurementSettings
    from homeplane.store import TraceStore
    from tests.test_homeplane_ha import adapter

    ha, _, requests, *_ = adapter()
    store = TraceStore(tmp_path / "SIMULATED-ha.sqlite")
    core = HomeCore(ha, store)
    core.measurements = MeasurementSettings(store)
    try:
        snap = await core.snapshot()
        view = core.measurements.view(ha, snap)
        s = view.sources[0]
        req = EditMeasurementGroup(
            realm=view.realm,
            expected_revision=view.revision,
            name="SIMULATED single room",
            primary_key=s.source_key,
            members=[
                {
                    "source_key": s.source_key,
                    "binding_fingerprint": s.binding_fingerprint,
                    "identity_fingerprint": s.identity_fingerprint,
                }
            ],
        )
        with pytest.raises(ValueError, match="position_review_required"):
            core.measurements.enrollment.edit_group(ha, snap, req)
        core.measurements.change(
            ha,
            snap,
            EditMeasurement(**request(view, fields={**FIELDS, "representativeness": "room_reference"})),
            "edit",
        )
        snap = await core.snapshot()
        view = core.measurements.view(ha, snap)
        req = req.model_copy(update={"expected_revision": view.revision})
        core.measurements.enrollment.edit_group(ha, snap, req)
        snap = await core.snapshot()
        view = core.measurements.view(ha, snap)
        assert view.groups[0].binding_valid and not view.groups[0].physical_independence_verified
        assert view.groups[0].state.quality == "unknown" and len(snap.derived_entities) == 1
        with pytest.raises(ValueError, match="healthy_comparable"):
            core.measurements.enrollment.preview(
                ha,
                snap,
                PreviewRelativeCalibration(
                    realm=view.realm,
                    expected_revision=view.revision,
                    group_entity=view.groups[0].entity,
                    source_key=view.sources[0].source_key,
                ),
            )
        assert all(r.method == "GET" for r in requests)
    finally:
        await ha.close()
        store.close()


@pytest.mark.asyncio
async def test_candidate_budget_and_contextual_primary_cannot_be_overridden(tmp_path):
    clock = [START]
    sim, store, core, snap, view = await training(tmp_path, clock)
    try:
        req = group_request(view)
        req = req.model_copy(
            update={
                "primary_key": next(s.source_key for s in view.sources if s.entity_id == "climate.living")
            }
        )
        with pytest.raises(ValueError, match="room_reference_position_required"):
            core.measurements.enrollment.edit_group(sim, snap, req)
        for _ in range(16):
            core.measurements.enrollment.preview(sim, snap, preview_request(view))
        with pytest.raises(ValueError, match="calibration_candidate_budget"):
            core.measurements.enrollment.preview(sim, snap, preview_request(view))
        with pytest.raises(ValidationError):
            EditMeasurementGroup(
                **{**group_request(view).model_dump(), "members": group_request(view).members * 2}
            )
    finally:
        store.close()
