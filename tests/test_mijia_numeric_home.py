"""从正式设备入口验证米家数值属性，全部设备和中枢为合成。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from homeplane.actions import ActionController
from homeplane.core import HomeCore
from homeplane.embodied.household import DeviceCommands, basic_command
from homeplane.embodied.mijia_inventory import account_household
from homeplane.embodied.mijia_lan import home_input
from homeplane.embodied.mijia_power import family_binding, grant_binding
from homeplane.embodied.ptz_control import atomic
from homeplane.home_experience import create_router
from homeplane.local_camera_home import LocalCameraHome
from homeplane.mijia_control import MijiaPower
from homeplane.mijia_power_home import MijiaPowerHome
from homeplane.settings import SettingsService
from homeplane.store import TraceStore
from tests.test_home_experience import Embodied, Runtime
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import plan, rig
from tests.test_mijia_power_home import Gateway

LIGHT, CLIMATE, SENSOR = '210000001', '220000001', '230000001'


def property_spec(iid, name, fmt, access, unit=None, limits=None):
    row = dict(iid=iid, type=f'urn:miot-spec-v2:property:{name}:00000001', format=fmt, access=access)
    if unit:
        row['unit'] = unit
    if limits:
        row['value-range'] = limits
    return row


async def setup_home(tmp_path, *, label='temperature', bad_setting=None):
    core, rows, ha_calls, _, path, authority = rig(tmp_path)
    legacy = authority.model_copy(update={'external_entity_ids': ['light.simulated', 'climate.simulated']})
    core.actions.authority_path = path.with_name('action-authority.json')
    atomic(core.actions.authority_path, legacy.model_dump(mode='json'))
    saved = {'cloud_server': 'cn', 'user_info': {'uid': 'synthetic-account'}, 'oauth_uuid': 'synthetic-grant'}
    atomic(tmp_path / 'miot-session.json', saved)
    atomic(tmp_path / 'home-room-membership.json', {'home_id': 'synthetic-home'})
    grant = grant_binding(saved)
    models, specs, registry_rows = {}, {}, {}

    def device(domain, did, service, props, suffix):
        urn = f'urn:miot-spec-v2:device:{service}:00000001:fixture-{domain}:1'
        model = f'fixture.{domain}.numeric'
        models[f'cn_{did}'] = {'urn': urn, 'model': model}
        specs[urn] = {'type': urn, 'services': [
            {'iid': 2, 'type': f'urn:miot-spec-v2:service:{service}:00000001', 'properties': props}
        ]}
        registry_rows[f'{domain}.simulated'] = (did, f'xiaomi_home.fixture_cn_{did}_numeric{suffix}')

    both = ['read', 'write']
    on = property_spec(1, 'on', 'bool', both)
    brightness = property_spec(2, 'brightness', 'uint8', both, 'percentage', [5, 100, 5])
    if bad_setting == 'step':
        brightness['value-range'] = [6, 100, 5]
    color = property_spec(3, 'color-temperature', 'uint16', both, 'kelvin', [2000, 6500, 100])
    temp = property_spec(2, 'target-temperature', 'float', both, 'fahrenheit' if bad_setting == 'unit' else 'celsius',
                         [16, 30, .5])
    device('light', LIGHT, 'light', [on, brightness, color], '_s_2_lamp')
    device('climate', CLIMATE, 'air-conditioner', [on, temp], '_s_2_climate')
    device('sensor', SENSOR, 'environment', [property_spec(2, 'temperature', 'float', ['read'], 'celsius')],
           f'_{label}_p_2_2')
    next(r for r in rows if r['entity_id'] == 'light.simulated')['attributes']['friendly_name'] = '客厅灯'
    next(r for r in rows if r['entity_id'] == 'climate.simulated')['attributes']['friendly_name'] = '客厅空调'
    inv = core.adapter._inventory

    async def registry():
        await inv()
        for external, (did, unique) in registry_rows.items():
            core.adapter._registry[external].update(platform='xiaomi_home', unique_id=unique)
            core.adapter._devices[f'device-{external}']['identifiers'] = [['xiaomi_home', f'cn_{did}']]

    core.adapter._inventory = registry
    service = SimpleNamespace(discovery_report=dict(
        state='ready', models=models, grant=grant, family=family_binding(grant, 'synthetic-home'),
        household=account_household(saved, 'synthetic-home'), inputs=home_input(tmp_path)[2],
    ), discovery_checked_at=time.monotonic())
    gateway = Gateway(tmp_path, service, fields={
        (LIGHT, 2, 1): True, (LIGHT, 2, 2): 40, (LIGHT, 2, 3): 3000,
        (CLIMATE, 2, 1): True, (CLIMATE, 2, 2): 23.0, (SENSOR, 2, 2): 22.7,
    })
    power = MijiaPower(service, spec=AsyncMock(side_effect=specs.get),
                       request=AsyncMock(side_effect=AssertionError('不应走云端')), lan=gateway)
    power.specs.update(specs)
    core.actions.mijia = power
    power.home = MijiaPowerHome(core, power)
    baseline = await core.snapshot()
    power.home.migrate()
    await core.adapter._stream_gap('synthetic')
    core.adapter.snapshot = AsyncMock(side_effect=AssertionError('不应回查 HA'))
    return core, baseline, gateway, ha_calls


async def warm(home):
    for _ in range(9):
        await home.sample_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('cold', [False, True])
async def test_controls_and_reported_sensors_are_independent_of_ha(tmp_path, cold):
    core, baseline, gateway, ha_calls = await setup_home(tmp_path)
    try:
        if cold:
            power = core.actions.mijia
            await close(core)
            core = HomeCore(LocalCameraHome(), TraceStore(tmp_path / 'cold.sqlite'))
            core.actions = ActionController(core, tmp_path / 'action-authority.json')
            core.actions.mijia = power
            power.home = MijiaPowerHome(core, power)
        await warm(core.actions.mijia.home)
        snapshot = await core.snapshot()
        expected = {e.kind: e for e in baseline.entities}
        assert {e.id for e in snapshot.entities} == {expected[k].id for k in ('light', 'climate', 'sensor')}
        assert not snapshot.rules and set(snapshot.action_targets) == {expected['light'].id, expected['climate'].id}
        reading = next(s for s in snapshot.states if s.id == expected['sensor'].id + ':temperature')
        assert reading.quality == 'unknown' and reading.value is None and reading.valid_until is None
        assert all(event.kind != 'state-transition' for event in snapshot.events)

        lock = asyncio.Lock()
        app = FastAPI()
        app.include_router(create_router(core, lock, SettingsService(tmp_path / 'web.json'), Embodied(), None,
                                         Runtime()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            async def view():
                result = (await client.get('/api/home/overview')).json()
                assert result['connected'] and result['source'] == 'mijia-lan'
                return {d['kind']: d for d in result['devices']}

            devices = await view()
            info, = devices['sensor']['readings']
            assert info['value'] == 22.7 and info['available'] and info['unit'] == '°C'
            assert info['basis'] == 'reported' and info['measured_at'] is None
            light, = devices['light']['controls']
            assert light['attributes']['brightness'] == 95 and light['value'] == 'on'

            async def change(kind, operation, desired, key):
                control, = (await view())[kind]['controls']
                req = dict(target=control['id'], operation=operation, desired=desired, event_id=key,
                           expected_state_version=control['state_version'])
                result = (await client.post('/api/home/control', json=req)).json()
                assert result['status'] == 'reported_match' and not result['physically_confirmed']
                assert (await client.post('/api/home/control', json=req)).json() == result
                return result

            result = await change('light', 'light.set', {'power': 'on', 'brightness': 128}, 'synthetic-dim')
            assert result['reported']['brightness'] == 135 and gateway.fields[LIGHT, 2, 2] == 55
            await change('light', 'light.color_temperature', {'color_temp_kelvin': 4200}, 'synthetic-color')
            assert (await view())['light']['controls'][0]['attributes']['color_temp_kelvin'] == 4200

        command = basic_command('Friday，把客厅空调设为24度')
        result = await DeviceCommands(core, lock).command(command['device'], command['action'], 'synthetic-voice')
        assert result['status'] == 'reported_match'
        snap = await core.snapshot()
        assert snap.mode == 'mijia-lan'
        assert gateway.manager.set_prop_async.await_count == 3
        assert [tuple(call.args) for call in gateway.manager.set_prop_async.await_args_list] == [
            (LIGHT, 2, 2, 55), (LIGHT, 2, 3, 4200), (CLIMATE, 2, 2, 24),
        ]
        assert not ha_calls
        core.actions.mijia.request.assert_not_called()
    finally:
        await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['another_value', 'out_of_step', 'lamp_off', 'lamp_off_after_plan',
                                  'missing_read_ack', 'lost_write_ack', 'pause_in_child'])
async def test_numeric_plan_has_one_exact_write_or_stops_before_send(tmp_path, case):
    core, _, gateway, ha_calls = await setup_home(tmp_path)
    try:
        await warm(core.actions.mijia.home)
        if case == 'lamp_off':
            gateway.fields[LIGHT, 2, 1] = False
            home = core.actions.mijia.home
            writer = home.writer(next(t.entity.id for t in home.catalog.targets if t.entity.kind == 'light'))
            await writer.readback()
        if case == 'out_of_step':
            trace = await plan(core, 'climate.temperature', {'temperature': 23.2})
        else:
            trace = await plan(core, 'light.set', {'power': 'on', 'brightness': 128})
        if case in {'lamp_off', 'out_of_step'}:
            assert trace.status == 'rejected'
        elif case == 'another_value':
            gateway.fields[LIGHT, 2, 2] = 60
        elif case == 'lamp_off_after_plan':
            gateway.fields[LIGHT, 2, 1] = False
        elif case == 'missing_read_ack':
            gateway.fields[LIGHT, 2, 2] = None
        elif case == 'lost_write_ack':
            gateway.lose_response = True
        elif case == 'pause_in_child':
            gateway.before_dispatch = core.actions.pause
        result = await core.actions.execute(trace.action_id)
        if case == 'lost_write_ack':
            assert result.status == 'unknown_outcome'
            assert (await core.actions.reconcile(result.action_id)).status == 'reported_match'
            assert gateway.manager.set_prop_async.await_count == 1
        else:
            assert result.status in {'rejected', 'unknown_outcome'}
            if case == 'another_value':
                assert result.code == 'source_conflict'
            gateway.manager.set_prop_async.assert_not_called()
        assert not ha_calls
        core.actions.mijia.request.assert_not_called()
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_mismatched_sensor_and_temperature_units_are_excluded(tmp_path):
    core, _, _, _ = await setup_home(tmp_path, label='other', bad_setting='unit')
    try:
        await warm(core.actions.mijia.home)
        snapshot = await core.snapshot()
        assert not any(e.kind == 'sensor' for e in snapshot.entities)
        climate = next(e for e in snapshot.entities if e.kind == 'climate')
        assert {c.name for c in climate.capabilities} == {'climate.state', 'climate.power'}
    finally:
        await close(core)
