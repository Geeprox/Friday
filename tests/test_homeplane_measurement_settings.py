"""Synthetic source-bound edits; no household credentials, cloud or actuators."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from homeplane.api import create_app
from homeplane.core import HomeCore
from homeplane.measurement_settings import MeasurementSettings
from homeplane.measurement_settings_contracts import (
    EditMeasurement,
    ExcludeMeasurement,
    MeasurementChange,
    ProfileFields,
)
from homeplane.simulator import Simulator
from homeplane.store import TraceStore

START = datetime(2026, 9, 30, 14, tzinfo=UTC)
ORIGIN = {"origin":"http://127.0.0.1:8740"}
FIELDS = {"placement":"occupied_zone", "installation_height_m":1.2,
          "reporting":"periodic_sample", "expected_context":"ambient", "ttl_seconds":300}


def request(view, index=0, **extra):
    source = view.sources[index]
    return dict(realm=view.realm, expected_revision=view.revision, source_key=source.source_key,
                binding_fingerprint=source.binding_fingerprint, **extra)


def state(snapshot, entity="sensor.living.temperature"):
    return next(s for s in snapshot.states if s.id == entity+":temperature")


def setup(tmp_path, clock):
    sim = Simulator(wall_now=lambda:clock[0])
    store = TraceStore(tmp_path/"SIMULATED-settings.sqlite")
    core = HomeCore(sim, store)
    core.measurements = MeasurementSettings(store)
    return sim, store, core


@pytest.mark.asyncio
async def test_edit_resets_recovery_never_reuses_old_fixture_and_invalidates_calibration(tmp_path):
    clock = [START]
    sim, store, core = setup(tmp_path, clock)
    try:
        before = await core.snapshot()
        assert state(before).quality == "healthy"
        view = core.measurements.view(sim, before)
        index = next(i for i,s in enumerate(view.sources) if s.entity_id == 'sensor.living.temperature')
        core.measurements.change(sim, before, EditMeasurement(**request(view,index,fields=FIELDS)), "edit")
        after = await core.snapshot()
        assert state(after).quality == "suspect" and state(after).value is None
        assert after.version != before.version
        assert next(e for e in after.quality_sources if e.entity_id=='sensor.living.temperature').distinct_sample_count == 1
        for _ in range(5):
            again = await core.snapshot()
            assert state(again).quality == "suspect"
            assert [o.measured_at for o in again.observations] == [o.measured_at for o in before.observations]
        # An explicit new scenario is synthetic sample history, not a refresh.
        sim.set_scenario('calibrated-fallback')
        fallback = await core.snapshot()
        assert state(fallback,'room.living').quality == 'unknown'
        assert fallback.states[-1].excluded_sources['sim:ac'] == 'calibration_scope_expired_or_changed'
        saved = core.measurements.view(sim, fallback).sources[index]
        assert saved.configured and saved.profile.version.startswith('local-')
        assert not sim.managed_actions
    finally:
        store.close()


@pytest.mark.asyncio
async def test_exclusion_hides_from_rule_and_query_without_affecting_lamp(tmp_path):
    from homeplane.query import HomeQueries
    from homeplane.query_contracts import HomeQuery, ReadScope
    from homeplane.rule_compiler import evaluate_rule
    from tests.test_homeplane_measurement_integration import numeric_rule
    from tests.test_homeplane_measurement_integration import request as trial
    clock = [START]
    sim, store, core = setup(tmp_path, clock)
    try:
        before = await core.snapshot()
        view = core.measurements.view(sim,before)
        index = next(i for i,s in enumerate(view.sources) if s.entity_id == 'sensor.living.temperature')
        core.measurements.change(sim,before,ExcludeMeasurement(**request(view,index,duration_seconds=60)), 'exclude')
        after = await core.snapshot()
        assert state(after).quality == 'unknown' and state(after).reasons == ['temporarily_excluded']
        assert state(after,'room.living').quality == 'unknown'
        assert [s for s in before.states if s.id.startswith('light.')] == [s for s in after.states if s.id.startswith('light.')]
        assert evaluate_rule(numeric_rule(20),after,trial(after,START),now=START).outcome == 'suppressed'
        q = HomeQueries(core, wall_now=lambda:START)
        result = await q.read(HomeQuery(kind='state',entities=['sensor.living.temperature']),
            ReadScope('console','fixture-v1',START+timedelta(seconds=5), boundary='loopback_console', all_home=True, permitted=True))
        assert result.status == 'ok', result
        assert result.items[0]['value'] is None and result.items[0]['quality'] == 'unknown'
        # No automatic healthy promotion at lease expiry, even with fresh receipt.
        clock[0] += timedelta(seconds=61)
        expired = await core.snapshot()
        assert state(expired).quality == 'suspect' and not core.measurements.view(sim,expired).sources[index].excluded
        assert not sim.managed_actions
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_requires_distinct_new_samples_and_clears_old_recovery(tmp_path):
    clock = [START]
    sim, store, core = setup(tmp_path, clock)
    try:
        before = await core.snapshot()
        view = core.measurements.view(sim,before)
        index = next(i for i,s in enumerate(view.sources) if s.entity_id == 'sensor.living.temperature')
        core.measurements.change(sim,before,ExcludeMeasurement(**request(view,index,duration_seconds=3600)), 'exclude')
        excluded = await core.snapshot()
        v = core.measurements.view(sim,excluded)
        core.measurements.change(sim,excluded,MeasurementChange(**request(v,index)), 'resume')
        resumed = await core.snapshot()
        assert state(resumed).quality == 'suspect'
        # Explicit synthetic new samples, same scenario/engine, spaced across window.
        for offset in (15,30):
            clock[0] = START+timedelta(seconds=offset)
            sim.measurements.rows = [o.model_copy(update={'measured_at':clock[0], 'source_reported_at':clock[0]})
                                     for o in sim.measurements.rows]
            recovered = await core.snapshot()
        assert state(recovered).quality == 'healthy'
        assert not sim.managed_actions
    finally:
        store.close()


@pytest.mark.asyncio
async def test_room_binding_change_fails_closed_until_explicit_new_binding_save(tmp_path):
    clock = [START]
    sim, store, core = setup(tmp_path, clock)
    try:
        before = await core.snapshot()
        view = core.measurements.view(sim,before)
        index = next(i for i,s in enumerate(view.sources) if s.entity_id == 'sensor.living.temperature')
        core.measurements.change(sim,before,EditMeasurement(**request(view,index,fields=FIELDS)), 'edit')
        sim.entities = [e.model_copy(update={'room':'SIMULATED-moved-room'}) if e.id=='sensor.living.temperature' else e for e in sim.entities]
        moved = await core.snapshot()
        assert state(moved).quality == 'unknown' and state(moved).reasons == ['measurement_binding_changed']
        v = core.measurements.view(sim,moved)
        assert v.realm == view.realm and not v.sources[index].binding_valid
        with pytest.raises(ValueError,match='source_conflict'):
            core.measurements.change(sim,moved,EditMeasurement(**request(view,index,fields=FIELDS)), 'edit')
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_persists_metadata_but_not_samples_or_ready_state(tmp_path):
    clock = [START]
    sim, store, core = setup(tmp_path, clock)
    before = await core.snapshot()
    view = core.measurements.view(sim,before)
    index = next(i for i,s in enumerate(view.sources) if s.entity_id == 'sensor.living.temperature')
    core.measurements.change(sim,before,EditMeasurement(**request(view,index,fields=FIELDS)), 'edit')
    body = store.connection.execute('SELECT body FROM measurement_settings').fetchone()[0]
    assert 'measured_at' not in body and '24.5' not in body and 'qualified' not in body
    store.close()
    _, reopened, restored = setup(tmp_path,clock)
    try:
        after = await restored.snapshot()
        assert state(after).quality == 'suspect'
        assert restored.measurements.view(restored.adapter,after).sources[index].configured
    finally:
        reopened.close()


@pytest.mark.parametrize('extra', [{'measured_at':START},{'source':'forged'},{'calibration':{}},{'simulated':False},{'approved':True}])
def test_client_cannot_supply_identity_sampling_calibration_or_authorization(extra):
    with pytest.raises(ValidationError):
        ProfileFields(**FIELDS, **extra)


def test_api_source_cas_csrf_no_unknown_fields_and_journal(tmp_path):
    app = create_app(db_path=tmp_path/'SIMULATED-api.sqlite',settings_path=tmp_path/'settings.json')
    with TestClient(app,base_url='http://127.0.0.1:8740') as c:
        v = c.get('/api/settings/measurements').json()
        s = next(s for s in v['sources'] if s['entity_id']=='sensor.living.temperature')
        body = {k:v[k] for k in ('realm',)} | {'expected_revision':v['revision'], 'source_key':s['source_key'],
               'binding_fingerprint':s['binding_fingerprint'], 'fields':FIELDS}
        assert c.post('/api/settings/measurements/profile',json=body).status_code == 403
        assert c.post('/api/settings/measurements/profile',json=body,headers={'origin':'https://simulated.invalid'}).status_code == 403
        saved = c.post('/api/settings/measurements/profile',json=body,headers=ORIGIN)
        assert saved.status_code == 200, saved.json()
        assert saved.json()['revision'] != v['revision']
        assert c.post('/api/settings/measurements/profile',json=body,headers=ORIGIN).status_code == 409
        malicious = c.post('/api/settings/measurements/profile',json=body|{'token':'SYNTHETIC-SECRET'},headers=ORIGIN)
        assert malicious.status_code == 422 and 'SYNTHETIC' not in malicious.text
        snapshot = c.get('/api/snapshot').json()
        assert any(e['payload'].get('configuration_operation')=='edit' for e in snapshot['events'])
        assert app.state.core.adapter.managed_actions == []


@pytest.mark.asyncio
async def test_corrupt_configuration_fails_numeric_closed_keeps_lamp_contract(tmp_path):
    clock = [START]
    sim, store, core = setup(tmp_path,clock)
    try:
        before = await core.snapshot()
        v = core.measurements.view(sim,before)
        with store.connection:
            store.connection.execute('INSERT INTO measurement_settings VALUES (?,?)',(v.realm,'{}'))
        failed = await core.snapshot()
        assert state(failed).quality == 'unknown' and state(failed,'room.living').quality == 'unknown'
        assert state(failed).reasons == ['measurement_configuration_unavailable']
        assert [s for s in before.states if s.id.startswith('light.')] == [s for s in failed.states if s.id.startswith('light.')]
        assert not sim.managed_actions
    finally:
        store.close()


def test_api_read_deadline_cancels_without_settings_mutation(tmp_path,monkeypatch):
    import asyncio

    from homeplane import api
    class SlowSimulator(Simulator):
        cancelled = False
        async def snapshot(self):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
    sim = SlowSimulator()
    monkeypatch.setattr(api,'SNAPSHOT_TIMEOUT_SECONDS',.02)
    app = create_app(sim,db_path=tmp_path/'SIMULATED-deadline.sqlite',settings_path=tmp_path/'settings.json')
    with TestClient(app,base_url='http://127.0.0.1:8740') as c:
        result = c.get('/api/settings/measurements')
        assert result.status_code == 503 and result.json()['code']=='measurement_configuration_unavailable'
        assert sim.cancelled
        assert app.state.core.store.connection.execute('SELECT count(*) FROM measurement_settings').fetchone()[0] == 0
        assert sim.managed_actions == []


@pytest.mark.asyncio
async def test_ha_cache_remains_unknown_after_configuration_no_hardware_calls(tmp_path):
    from tests.test_homeplane_ha import adapter
    ha, _, requests, _, _ = adapter()
    store = TraceStore(tmp_path/'SIMULATED-ha.sqlite')
    core = HomeCore(ha,store)
    core.measurements = MeasurementSettings(store)
    try:
        before = await core.snapshot()
        v = core.measurements.view(ha,before)
        core.measurements.change(ha,before,EditMeasurement(**request(v,fields=FIELDS)), 'edit')
        after = await core.snapshot()
        assert after.states[0].quality == 'unknown' and after.states[0].value is None
        assert after.observations[0].measured_at is None and after.observations[0].cached
        assert after.quality_sources[0].context == 'unknown' and after.quality_sources[0].distinct_sample_count == 0
        assert not after.derived_entities
        assert all(r.method=='GET' and r.url.path in ('/api/states','/api/services') for r in requests)
    finally:
        await ha.close()
        store.close()
