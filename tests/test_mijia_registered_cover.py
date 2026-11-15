"""从已登记设备迁移到中枢直连的窗帘回归，只使用合成设备。"""

import asyncio
from datetime import UTC, datetime, timedelta
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

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
from homeplane.mijia_power_home import MijiaPowerHome, catalog_available
from homeplane.mijia_registered_cover import read_cover_catalog, registered_cover_operation
from homeplane.rule_contracts import RuleIR
from homeplane.settings import SettingsService
from homeplane.store import TraceStore
from tests.test_home_experience import Embodied, Runtime
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import plan, rig
from tests.test_mijia_numeric_home import property_spec
from tests.test_mijia_power_home import Gateway


DID = '260000001'
ALTERNATIVE = '260000002'
IDENTITY = f'cn_{DID}'
URN = 'urn:miot-spec-v2:device:curtain:00000001:fixture-shade:1'
MODEL = 'fixture.curtain.shade'


class CoverGateway(Gateway):
    def __init__(self, path, service):
        fields = {(did, 2, prop): value for did in (DID, ALTERNATIVE)
                  for prop, value in enumerate((None, 0, 0, 0, 9, 0)) if prop}
        super().__init__(path, service, fields=fields)
        self.states = {30: (100, 9), 10: (0, 9), 20: (self.fields[DID, 2, 1], 9)}

    def write(self, did, siid, piid, value):
        if piid == 2:
            self.fields[did, siid, 1] = value
        elif piid == 3:
            position, status = self.states[value]
            if value != 20:
                self.fields[did, siid, 1] = position
            self.fields[did, siid, 4] = status
        return super().write(did, siid, piid, value)

    async def request(self, payload, *, family, guard=None):
        async with self.io:
            self.calls.append(dict(payload))
            if guard is not None:
                assert guard()
                self.before_dispatch()
            return await registered_cover_operation(self.manager, payload, self.service.discovery_report['grant'],
                                                    family, 'cn', 'synthetic-group', self.dids, lambda: True,
                                                    self.path)


def specification(*, status=True, variant=None):
    current = property_spec(1, 'current-position', 'uint8', ['read', 'notify'], 'percentage', [0, 100, 1])
    target = property_spec(2, 'target-position', 'uint8', ['write'], 'percentage', [0, 100, 5])
    motor = property_spec(3, 'motor-control', 'uint8', ['write'])
    motor['value-list'] = [dict(value=30, description='Open'), dict(value=10, description='Close'),
                            dict(value=20, description='Stop')]
    fault = property_spec(5, 'fault', 'uint8', ['read'])
    fault['value-list'] = [dict(value=0, description='No Faults'), dict(value=1, description='Motor Blocked')]
    props = [current, target, motor, fault]
    if status:
        movement = property_spec(4, 'status', 'uint8', ['read', 'notify'])
        movement['value-list'] = [dict(value=7, description='Opening'), dict(value=8, description='Closing'),
                                   dict(value=9, description='Stopped')]
        props.append(movement)
    services = [{'iid': 2, 'type': 'urn:miot-spec-v2:service:curtain:00000001', 'properties': props}]
    if variant == 'angle':
        current['unit'] = 'arcdegrees'
    elif variant == 'ambiguous':
        services.append({**services[0], 'iid': 3})
    elif variant == 'no_actual':
        current['access'] = ['notify']
    elif variant == 'target_only':
        props.remove(current)
        target['access'].append('read')
    elif variant == 'window':
        services[0]['type'] = 'urn:miot-spec-v2:service:window-opener:00000002'
    return {'type': URN, 'services': services}


async def setup(tmp_path, *, variant=None, status=True, protected=False, duplicate=False):
    core, rows, ha_calls, _, path, original = rig(tmp_path)
    authority = original.model_copy(update={'external_entity_ids': ['cover.simulated']})
    core.actions.authority_path = path.with_name('action-authority.json')
    atomic(core.actions.authority_path, authority.model_dump(mode='json'))
    saved = {'cloud_server': 'cn', 'user_info': {'uid': 'synthetic-account'}, 'oauth_uuid': 'synthetic-grant'}
    atomic(tmp_path / 'miot-session.json', saved)
    atomic(tmp_path / 'home-room-membership.json', {'home_id': 'synthetic-home'})
    if protected:
        atomic(tmp_path / 'config.json', {'version': 1, 'cameras': [{'device': {'did': '259999999'}}],
                                          'miot_covers': {'slats': {'did': DID, 'position_semantics': 'slat_angle'}}})
    grant = grant_binding(saved)
    row = next(r for r in rows if r['entity_id'] == 'cover.simulated')
    row['attributes'].update(supported_features=15, friendly_name='客厅窗帘')
    if duplicate:
        rows.append({**row, 'entity_id': 'cover.second'})
    collect = core.adapter._inventory

    async def registry():
        await collect()
        core.adapter._services.append({'domain': 'cover', 'services': {
            'open_cover': {}, 'close_cover': {}, 'stop_cover': {}, 'set_cover_position': {},
        }})
        for external in ('cover.simulated', 'cover.second') if duplicate else ('cover.simulated',):
            unique = f'xiaomi_home.fixture_{IDENTITY}_shade'
            if variant != 'ambiguous':
                unique += '_s_2_curtain' if external == 'cover.simulated' else '_s_2_duplicate'
            core.adapter._registry[external].update(platform='xiaomi_home', unique_id=unique)
            core.adapter._devices[f'device-{external}']['identifiers'] = [['xiaomi_home', IDENTITY]]
        core.adapter._areas['room']['name'] = '客厅'

    core.adapter._inventory = registry
    service = SimpleNamespace(discovery_report=dict(
        state='ready', models={IDENTITY: {'urn': URN, 'model': MODEL}},
        grant=grant, household=account_household(saved, 'synthetic-home'),
        family=family_binding(grant, 'synthetic-home'), inputs=home_input(tmp_path)[2],
    ), discovery_checked_at=time.monotonic())
    gateway = CoverGateway(tmp_path, service)
    power = MijiaPower(service, spec=AsyncMock(side_effect=AssertionError('无需重新取规格')),
                       request=AsyncMock(side_effect=AssertionError('不允许用云端写')), lan=gateway)
    power.specs[URN] = specification(status=status, variant=variant)
    core.actions.mijia = power
    power.home = MijiaPowerHome(core, power)
    baseline = await core.snapshot()
    power.home.migrate()
    return core, baseline, gateway, ha_calls


async def cutover(core, tmp_path):
    power = core.actions.mijia
    assert await power.home.qualify_source()
    await close(core)
    core.store.connection.close()
    local = HomeCore(LocalCameraHome(mijia_selected=True), TraceStore(tmp_path/'mijia.sqlite'))
    local.actions = ActionController(local, tmp_path/'action-authority.json')
    local.actions.mijia = power
    power.home = MijiaPowerHome(local, power)
    await power.home.sample_once()
    return local


@pytest.mark.asyncio
async def test_registered_cover_uses_same_device_room_voice_gesture_and_clock_binding_without_ha(tmp_path, monkeypatch):
    core, baseline, gateway, ha_calls = await setup(tmp_path)
    target, = read_cover_catalog(tmp_path).targets
    entity = next(e for e in baseline.entities if e.kind == 'cover')
    device = next(d for d in baseline.devices if entity.id in d.function_ids)
    assert (target.entity.id, target.device.id, target.entity.room) == (entity.id, device.id, entity.room)
    assert catalog_available(tmp_path)
    core = await cutover(core, tmp_path)
    lock = asyncio.Lock()
    app = FastAPI()
    app.include_router(create_router(core, lock, SettingsService(tmp_path/'web.json'), Embodied(), None, Runtime()))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            report = (await client.get('/api/home/overview')).json()
            cover, = [d for d in report['devices'] if d['kind'] == 'cover']
            control, = cover['controls']
            assert report['source'] == 'mijia-lan' and cover['id'] == device.id
            assert control['id'] == entity.id and control['attributes']['current_position'] == 0
            assert 'current_tilt_position' not in control['attributes']
            req = dict(target=entity.id, operation='cover.position', desired={'position': 65},
                       event_id='synthetic-position', expected_state_version=control['state_version'])
            result = await client.post('/api/home/control', json=req)
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'reported_match' and result.json()['reported']['position'] == 65
            assert gateway.manager.set_prop_async.await_count == 1
            repeated = await client.post('/api/home/control', json=req)
            assert repeated.json() == result.json() and gateway.manager.set_prop_async.await_count == 1

        commands = DeviceCommands(core, lock)
        spoken = basic_command('Friday，打开客厅窗帘')
        result = await commands.command(spoken['device'], spoken['action'], 'synthetic-voice', room='客厅')
        assert result['status'] == 'reported_match' and result['outcomes'][0]['target'] == entity.id
        result = await commands.command(None, 'position:25', 'synthetic-gesture', target=entity.id,
                                        dispatch_guard=lambda: True, dispatch_deadline=time.monotonic()+1)
        assert result['status'] == 'reported_match' and gateway.fields[DID, 2, 1] == 25
        assert not ha_calls and not core.actions.mijia.request.await_count
        assert [(x.args[2], x.args[3]) for x in gateway.manager.set_prop_async.await_args_list] == [
            (2, 65), (3, 30), (2, 25),
        ]
        monkeypatch.setattr('homeplane.embodied.interaction.load', lambda: SimpleNamespace(
            enabled=True, bindings=[SimpleNamespace(target=entity.id, enabled=True, action='stop')]))
        home = core.actions.mijia.home
        assert not home.unqualified_references(home.connected_targets()[1])
        rule = RuleIR(
            id='synthetic-cover-clock', revision=1, intent='合成窗帘定时关闭',
            trigger={'type': 'schedule', 'at': '11:30', 'weekdays': [1], 'timezone': 'Asia/Shanghai'},
            actions=[{'type': 'device.set', 'id': 'close', 'operation': 'cover.state', 'target': entity.id,
                      'desired': {'state': 'closed'}}],
            execution={'preferred_backend': 'home_core'},
        )
        authority, _ = core.actions.authority()
        bound = home.schedule_bindings(rule, authority)
        assert bound and home.schedule_fresh(bound) and home.schedule_routable(bound)
    finally:
        await close(core)
        core.store.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['angle', 'ambiguous', 'no_actual', 'window', 'protected', 'duplicate'])
async def test_unsafe_or_ambiguous_curtain_specs_never_migrate(tmp_path, variant):
    core, _, gateway, _ = await setup(tmp_path, variant=variant, protected=variant=='protected',
                                      duplicate=variant=='duplicate')
    try:
        assert not catalog_available(tmp_path) and not core.actions.mijia.home.connected_targets()[1]
        assert not gateway.calls
    finally:
        await close(core)
        core.store.connection.close()


@pytest.mark.asyncio
async def test_registered_cover_confirmed_stop_and_read_only_ambiguous_write_reconcile(tmp_path):
    origin, _, gateway, ha_calls = await setup(tmp_path)
    core = await cutover(origin, tmp_path)
    try:
        stop = await plan(core, 'cover.state', {'state': 'stopped'})
        gateway.fields[DID, 2, 1], gateway.fields[DID, 2, 4] = 36, 8
        result = await core.actions.execute(stop.action_id)
        assert result.status == 'reported_match' and gateway.fields[DID, 2, 1] == 36
        gateway.lose_response = True
        pending = await plan(core, 'cover.position', {'position': 60})
        result = await core.actions.execute(pending.action_id)
        assert result.status == 'unknown_outcome' and gateway.manager.set_prop_async.await_count == 2
        assert (await core.actions.execute(pending.action_id)).status == 'unknown_outcome'
        confirmed = await core.actions.reconcile(pending.action_id)
        assert confirmed.status == 'reported_match' and gateway.manager.set_prop_async.await_count == 2
        assert not ha_calls
    finally:
        await close(core)
        core.store.connection.close()


@pytest.mark.asyncio
async def test_goal_only_curtain_takes_commands_and_never_reports_position_from_requested_value(tmp_path):
    origin, baseline, gateway, ha_calls = await setup(tmp_path, variant='target_only')
    target = next(e.id for e in baseline.entities if e.kind == 'cover')
    core = await cutover(origin, tmp_path)
    lock = asyncio.Lock()
    app = FastAPI()
    app.include_router(create_router(core, lock, SettingsService(tmp_path/'web.json'), Embodied(), None, Runtime()))
    try:
        action = await plan(core, 'cover.position', {'position': 65})
        result = await core.actions.execute(action.action_id)
        assert result.status == 'accepted' and result.code == 'mijia_cover_acknowledged'
        assert result.reported['position'] is None and result.reported['target_position'] == 65
        assert not result.physically_confirmed
        assert not core.store.unresolved_light_actions()
        assert (await core.actions.execute(action.action_id)).status == 'accepted'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            overview = (await client.get('/api/home/overview')).json()
            device, = [d for d in overview['devices'] if d['kind'] == 'cover']
            control, = device['controls']
            assert control['id'] == target and control['available'] and control['value'] == 'stopped'
            assert control['attributes'] == {'target_position': 65}
        assert not any(call.args[2] == 1 for call in gateway.manager.get_prop_async.await_args_list)
        assert gateway.manager.set_prop_async.await_count == 1 and not ha_calls
    finally:
        await close(core)
        core.store.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['fault', 'authority', 'rebound', 'late_claim'])
async def test_registered_cover_requires_current_scope_before_motor_write(tmp_path, blocked):
    origin, _, gateway, ha_calls = await setup(tmp_path)
    core = await cutover(origin, tmp_path)
    try:
        pending = await plan(core, 'cover.position', {'position': 65})
        if blocked == 'fault':
            gateway.fields[DID, 2, 5] = 1
        elif blocked == 'authority':
            core.actions.pause()
        elif blocked == 'rebound':
            gateway.service.discovery_report['models'][IDENTITY] = {'urn': URN, 'model': 'fixture.curtain.changed'}
        else:
            gateway.before_dispatch = core.actions.pause
        with pytest.raises(ValueError, match='mijia_permission_changed'):
            catalog = read_cover_catalog(tmp_path)
            target, = catalog.targets
            from homeplane.mijia_registered_cover import cover_digest
            await gateway.request({'registered_cover': cover_digest(target), 'did': ALTERNATIVE,
                                   'grant': gateway.service.discovery_report['grant']}, family=catalog.family)
        result = await core.actions.execute(pending.action_id)
        assert result.status in {'rejected', 'unknown_outcome'}
        assert not gateway.manager.set_prop_async.await_count and not ha_calls
    finally:
        await close(core)
        core.store.connection.close()
