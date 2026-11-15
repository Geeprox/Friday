"""从桌面设备页登记米家设备并读回控制状态，无需启动真实设备或 HA。"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from homeplane.embodied.household import DeviceCommands
from homeplane.core_rules import CoreRuleProposal
from homeplane.embodied.mijia_inventory import account_household, reference
from homeplane.embodied.mijia_lan import home_input
from homeplane.embodied.mijia_power import family_binding, grant_binding
from homeplane.embodied.ptz_control import atomic
from homeplane.mijia_control import MijiaPower
from homeplane.mijia_lan_client import MijiaLanClient
from homeplane.mijia_power_home import MijiaPowerHome, NATIVE_CATALOG, NATIVE_SENSORS
from homeplane.rule_contracts import RuleIR
from tests.test_connection_setup import ORIGIN
from tests.test_connection_setup import fresh as fresh
from tests.test_homeplane_core_rules import enable
from tests.test_mijia_power_home import Gateway


NEW_DID = '900000001'
IDENTITY = f'cn_{NEW_DID}'
URN = 'urn:miot-spec-v2:device:outlet:00000001:fixture-dual:1'
MODEL = 'fixture.outlet.dual'
SENSOR_URN = 'urn:miot-spec-v2:device:temperature-humidity-sensor:0000A00A:fixture-sensor:1'
HOME = 'synthetic-home'
CANDIDATE = reference('cn', HOME, NEW_DID)
HEADERS = {'Origin': ORIGIN}


def spec(first, second):
    def outlet(siid, piid):
        return {'iid': siid, 'type': 'urn:miot-spec-v2:service:outlet:00000001', 'properties': [
            {'iid': piid, 'type': 'urn:miot-spec-v2:property:on:00000001', 'access': ['read', 'write'], 'format': 'bool'},
        ]}
    return {'type': URN, 'services': [outlet(5, first), outlet(9, second)]}


def sensor_spec():
    return {'type': SENSOR_URN, 'services': [{
        'iid': 2, 'type': 'urn:miot-spec-v2:service:environment:0000780A:fixture:1',
        'properties': [
            {'iid': 1, 'type': 'urn:miot-spec-v2:property:temperature:00000020',
             'access': ['read', 'notify'], 'format': 'float', 'unit': 'celsius'},
            {'iid': 2, 'type': 'urn:miot-spec-v2:property:relative-humidity:0000000C',
             'access': ['read'], 'format': 'uint8', 'unit': 'percentage'},
            {'iid': 3, 'type': 'urn:miot-spec-v2:property:illumination:00000065',
             'access': ['read', 'write'], 'format': 'uint32', 'unit': 'lux'},
        ],
    }]}


def fixture_home(module, tmp_path, monkeypatch, *, sensor=False):
    directory = tmp_path / 'embodied' / 'private'
    directory.mkdir(parents=True, mode=0o700)
    saved = {'cloud_server': 'cn', 'user_info': {'uid': 'synthetic-account'}, 'oauth_uuid': 'synthetic-mi-grant'}
    atomic(directory / 'miot-session.json', saved)
    atomic(directory / 'home-room-membership.json', {'home_id': HOME})
    grant = grant_binding(saved)
    specification = sensor_spec() if sensor else spec(3, 7)
    model = 'fixture.temperature.humidity' if sensor else MODEL
    report = {
        'state': 'ready', 'region': 'cn', 'models': {IDENTITY: {'urn': specification['type'], 'model': model}},
        'grant': grant, 'family': family_binding(grant, HOME), 'household': account_household(saved, HOME),
        'inputs': home_input(directory)[2], 'devices': [
            {'id': CANDIDATE, 'identity': IDENTITY, 'name': '示例温湿度计' if sensor else '示例双路插座', 'room': '书房',
             'kind': 'device', 'online': True},
        ],
    }
    gateways = []
    fields = ({(NEW_DID, 2, 1): 22.4, (NEW_DID, 2, 2): 0, (NEW_DID, 2, 3): 5} if sensor
              else {(NEW_DID, 5, 3): False, (NEW_DID, 9, 7): True})

    def discovery(service):
        service.discovery_report = report
        service.discovery_checked_at = time.monotonic()

    def prepare(power, core):
        gateway = Gateway(directory, power.service, fields)
        power.lan = gateway
        gateway.close = AsyncMock()
        power.spec = AsyncMock(return_value=specification)
        power.request = AsyncMock(side_effect=AssertionError('不应经米家云写入'))
        power.home = MijiaPowerHome(core, power)
        gateways.append(gateway)

    monkeypatch.setattr('homeplane.embodied.service.mijia_runtime_configured', lambda path: path == directory)
    monkeypatch.setattr('homeplane.embodied.service.EmbodiedService.start_mijia_discovery', discovery)
    monkeypatch.setattr(MijiaPower, 'start', prepare)
    monkeypatch.setattr(module, 'real_adapter', AsyncMock(side_effect=AssertionError('不应连接 HA')))
    return directory, report, gateways


def client_for(backend, address='127.0.0.1'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(backend, client=(address, 25000)), base_url=ORIGIN)


def control_request(entity, event_id, power):
    return dict(target=entity['id'], operation='switch.set', desired={'power': power}, event_id=event_id,
                expected_state_version=entity['state_version'])


@pytest.mark.asyncio
@pytest.mark.parametrize('sensor', [False, True])
async def test_enrollment_rejects_readings_across_a_gateway_reconnect(fresh, tmp_path, monkeypatch, sensor):
    backend, module = fresh
    backend.persistent = True
    directory, _, gateways = fixture_home(module, tmp_path, monkeypatch, sensor=sensor)
    try:
        await backend._connect()
        gateway, = gateways
        request_property = gateway.request

        async def reconnect_between_reads(payload, *, family):
            result = await request_property(payload, family=family)
            if len(gateway.calls) == 1:
                gateway.route_tokens.pop(NEW_DID)
            return result

        monkeypatch.setattr(gateway, 'request', reconnect_between_reads)
        async with client_for(backend) as client:
            status = (await client.get('/api/home/overview')).json()
            candidate, = status['discovery']['devices']
            permitted = {'allow_readings': True} if sensor else {'allow_power': True}
            response = await client.post('/api/embodied/discovery/enroll-device', headers=HEADERS,
                                         json=dict(candidate=CANDIDATE, **permitted,
                                                   expected_revision=candidate['enrollment_revision']))
            assert response.status_code == 409 and response.json()['detail'] == 'mijia_enrollment_changed'
            assert not (directory / NATIVE_CATALOG).exists()
            assert not (directory / NATIVE_SENSORS).exists()
            gateway.manager.set_prop_async.assert_not_awaited()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sensor_enrollment_restores_readings_without_ha_or_control_authority(fresh, tmp_path, monkeypatch):
    backend, module = fresh
    backend.persistent = True
    directory, discovery, gateways = fixture_home(module, tmp_path, monkeypatch, sensor=True)
    route = '/api/embodied/discovery/enroll-device'
    try:
        await backend._connect()
        gateway, = gateways
        async with client_for(backend) as client:
            result = (await client.get('/api/home/overview')).json()
            candidate, = result['discovery']['devices']
            assert candidate['enrollment_access'] == 'readings' and not result['devices']
            command = {'candidate': CANDIDATE, 'expected_revision': candidate['enrollment_revision']}
            response = await client.post(route, headers=HEADERS, json=command | {'allow_power': True})
            assert response.status_code == 422 and not gateway.calls
            response = await client.post(route, headers=HEADERS, json=command | {'allow_readings': True})
            assert response.status_code == 200, response.text
            connected = response.json()['device']
            result = (await client.get('/api/home/overview')).json()
            assert result['source'] == 'mijia-lan' and not result['discovery']['devices']
            device, = result['devices']
            assert device['id'] == connected and device['room'] == '书房' and not device['controls']
            assert {(r['device_class'], r['value'], r['unit']) for r in device['readings']} == {
                ('temperature', 22.4, '°C'), ('humidity', 0, '%'),
            }
            assert all(r['measured_at'] is None for r in device['readings'])
            identifiers = {r['id'] for r in device['readings']}
            assert (directory / NATIVE_SENSORS).is_file() and not (directory / NATIVE_CATALOG).exists()
            assert not (directory / 'action-authority.json').exists()
            assert [row['piid'] for row in gateway.calls] == [1, 2]
            with pytest.raises(ValueError, match='mijia_permission_changed'):
                await gateway.request({'did': NEW_DID, 'siid': 2, 'piid': 3, 'numeric': True,
                                       'sensor_read': 'registered', 'grant': discovery['grant']},
                                      family=discovery['family'])
            assert gateway.manager.get_prop_async.await_count == 2
            gateway.manager.set_prop_async.assert_not_awaited()
    finally:
        await backend.close()

    discovery['devices'][0].update(name='示例传感器新名称', room='客厅')
    reopened = module.DesktopService()
    reopened.persistent = True
    try:
        await reopened._connect()
        core = reopened.inner.state.core
        home = core.actions.mijia_power_home
        gateway = gateways[-1]
        for _ in range(3):
            await home.sample_once()
        async with client_for(reopened) as client:
            status = (await client.get('/api/home/overview')).json()
            device, = status['devices']
            assert device['id'] == connected and device['room'] == '客厅' and device['name'] == '示例传感器新名称'
            assert {r['id'] for r in device['readings']} == identifiers
            assert all(r['available'] for r in device['readings'])
            assert all(row['sensor_read'] == 'registered' for row in gateway.calls)
            gateway.route_tokens.pop(NEW_DID)
            assert not any(r['available'] for r in (await client.get('/api/home/overview')).json()['devices'][0]['readings'])
            for _ in range(3):
                await home.sample_once()
            assert all(r['available'] for r in (await client.get('/api/home/overview')).json()['devices'][0]['readings'])
        snapshot = await core.snapshot()
        assert not snapshot.events and all(state.quality == 'unknown' for state in snapshot.states)
        assert not core.actions.authority()[0]
        gateway.manager.set_prop_async.assert_not_awaited()
    finally:
        await reopened.close()
    module.real_adapter.assert_not_awaited()


@pytest.mark.asyncio
async def test_mijia_devices_enroll_and_control_without_any_ha_setup(fresh, tmp_path, monkeypatch):
    backend, module = fresh
    backend.persistent = True
    directory, _, gateways = fixture_home(module, tmp_path, monkeypatch)
    today = datetime.now(UTC)
    try:
        await backend._connect()
        assert len(gateways) == 1 and backend.adapter.mijia_selected
        assert backend.inner.state.core.actions.authority_path == directory / 'action-authority.json'
        gateway = gateways[0]
        async with client_for(backend) as client:
            report = (await client.get('/api/home/overview')).json()
            item, = report['discovery']['devices']
            assert item['id'] == CANDIDATE and report['discovery']['direct_enrollment']
            assert 'identity' not in item and not gateway.calls and not report['devices']
            request = dict(candidate=CANDIDATE, expected_revision=item['enrollment_revision'], allow_power=True)
            endpoint = '/api/embodied/discovery/enroll-device'
            assert (await client.post(endpoint, json=request)).status_code == 403
            assert (await client.post(endpoint, json=request | {'allow_power': False}, headers=HEADERS)).status_code == 422
            async with client_for(backend, '192.0.2.25') as remote:
                assert (await remote.post(endpoint, json=request, headers=HEADERS)).status_code == 403
            assert not gateway.calls
            response = await client.post(endpoint, json=request, headers=HEADERS)
            assert response.status_code == 200, response.text
            assert len(gateway.calls) == 2
            gateway.manager.set_prop_async.assert_not_awaited()
            assert all('power' not in call for call in gateway.calls)
            delivered = response.json()['device']

            async def device_view():
                report = (await client.get('/api/home/overview')).json()
                assert report['source'] == 'mijia-lan' and not report['discovery']['devices']
                device, = report['devices']
                assert device['id'] == delivered and device['room'] == '书房'
                return device['controls']

            controls = await device_view()
            assert set(c['value'] for c in controls) == {'on', 'off'}
            off = next(c for c in controls if c['value'] == 'off')
            command = control_request(off, 'synthetic-first-activation', 'on')
            response = await client.post('/api/home/control', json=command, headers=HEADERS)
            assert response.json()['status'] == 'reported_match' and not response.json()['physically_confirmed']
            assert (await client.post('/api/home/control', json=command, headers=HEADERS)).json() == response.json()
            gateway.manager.set_prop_async.assert_awaited_once_with(NEW_DID, 5, 3, True)
            assert all(c['value'] == 'on' for c in await device_view())

            action_status = (await client.get('/api/settings/actions/status')).json()
            assert action_status['enabled'] and action_status['scope'] == 'mijia_selected_devices'
            assert (await client.post('/api/settings/actions/pause', json={}, headers=HEADERS)).status_code == 200
            assert (await client.get('/api/settings/actions/status')).json()['enabled'] is False
            assert not await device_view()
            paused = (await client.get('/api/settings/actions/status')).json()
            resume = await client.post('/api/settings/actions/resume', json={'expected_revision': paused['revision']},
                                       headers=HEADERS)
            assert resume.status_code == 200, resume.text
            assert {c['id'] for c in await device_view()} == {c['id'] for c in controls}
            core = backend.inner.state.core
            rule = RuleIR(id='synthetic-native-clock', revision=1, intent='合成插座定时关机',
                          trigger={'type':'once', 'at':(today+timedelta(minutes=5)).isoformat(timespec='seconds'), 'timezone':'UTC'},
                          actions=[{'type':'device.set', 'id':'off', 'operation':'switch.set',
                                    'target':off['id'], 'desired':{'power':'off'}}],
                          execution={'preferred_backend':'home_core'})
            manager = backend.inner.state.core_rules
            plan = await enable(manager, CoreRuleProposal(
                rule=rule, expected_context=manager.context()['fingerprint'], idempotency_key='synthetic-native-clock',
                expires_at=datetime.now(UTC)+timedelta(seconds=15),
            ))
            assert plan.new.enabled and manager.matches_scope(plan.new, await core.snapshot())
            commands = DeviceCommands(core, asyncio.Lock())
            for device in await commands.inventory():
                if device['id'] == off['id']:
                    assert device['actions'] == ['on', 'off']
                    break
            else:
                pytest.fail('已登记通道未进入互动目录')
            other = next(c for c in controls if c['id'] != off['id'])
            result = await commands.command(None, 'off', 'synthetic-native-gesture', target=other['id'],
                                            dispatch_guard=lambda: True, dispatch_deadline=time.monotonic()+2)
            assert result['status'] == 'reported_match'
            gateway.manager.set_prop_async.assert_any_await(NEW_DID, 9, 7, False)
        assert (directory / NATIVE_CATALOG).is_file() and not (directory / 'action-authority.json').exists()
    finally:
        await backend.close()

    reopened = module.DesktopService()
    reopened.persistent = True
    try:
        await reopened._connect()
        home = reopened.inner.state.core.actions.mijia_power_home
        for _ in range(3):
            await home.sample_once()
        async with client_for(reopened) as client:
            view = (await client.get('/api/home/overview')).json()
            control = next(c for c in view['devices'][0]['controls'] if c['id'] == off['id'])
            assert control['value'] == 'on' and not view['discovery']['devices']
            response = await client.post('/api/home/control', json=control_request(control, 'synthetic-after-restart','off'),
                                         headers=HEADERS)
            assert response.json()['status'] == 'reported_match'
            gateway = gateways[-1]
            gateway.lose_response = True
            view = (await client.get('/api/home/overview')).json()
            control = next(c for c in view['devices'][0]['controls'] if c['id'] == off['id'])
            request = control_request(control, 'synthetic-unknown-send', 'on')
            response = (await client.post('/api/home/control', json=request, headers=HEADERS)).json()
            assert response['status'] == 'unknown_outcome'
            assert (await client.post('/api/home/control', json=request, headers=HEADERS)).json() == response
            reconciled = await reopened.inner.state.core.actions.reconcile(response['action_id'])
            assert reconciled.status == 'reported_match' and not reconciled.physically_confirmed
        assert [call.args for call in gateways[-1].manager.set_prop_async.await_args_list] == [
            (NEW_DID, 5, 3, False), (NEW_DID, 5, 3, True),
        ]
        assert not module.real_adapter.await_count
        assert reopened.inner.state.core_rules.read(plan.new.id) == plan.new
        assert reopened.inner.state.core_rules.matches_scope(
            plan.new, await reopened.inner.state.core.snapshot(),
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['moved_after_review', 'offline', 'wireless_switch'])
async def test_native_enrollment_fails_closed_when_confirmation_or_physical_binding_changes(
        fresh, tmp_path, monkeypatch, scenario):
    backend, module = fresh
    backend.persistent = True
    directory, report, gateways = fixture_home(module, tmp_path, monkeypatch)
    try:
        await backend._connect()
        home = backend.inner.state.core.actions.mijia_power_home
        async with client_for(backend) as client:
            item, = (await client.get('/api/home/overview')).json()['discovery']['devices']
            if scenario == 'moved_after_review':
                report['devices'][0]['room'] = '卧室'
            elif scenario == 'offline':
                gateways[0].online = False
            else:
                wire = spec(3, 7)
                wire['services'][0]['properties'] += [
                    {'iid': 20, 'type': 'urn:miot-spec-v2:property:switch-mode:00000001',
                     'access':['read', 'write'], 'format':'uint8'},
                ]
                home.power.spec = AsyncMock(return_value=wire)
            response = await client.post('/api/embodied/discovery/enroll-device',
                json=dict(candidate=item['id'], expected_revision=item['enrollment_revision'], allow_power=True),
                headers=HEADERS)
            assert response.status_code == 409
        assert not (directory / NATIVE_CATALOG).exists()
        assert not gateways[0].calls
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_new_device_roster_reopens_lan_worker_when_current_request_is_idle(monkeypatch):
    inventory = ['old-membership']
    family = 'a'*64
    lan = MijiaLanClient(lambda: family, current_inventory=lambda: inventory[0])
    started = asyncio.Event()
    opened = []

    async def connect(scope, membership):
        lan.family, lan.inventory = scope, membership
        lan.process = SimpleNamespace(returncode=None)
        opened.append(membership)
        started.set()

    async def close():
        lan.process = None

    async def routes(*_args, **_kwargs):
        return {'routes': [NEW_DID] if lan.inventory == 'new-membership' else [], 'push': 0, 'connected': True}

    monkeypatch.setattr(lan, '_open', connect)
    monkeypatch.setattr(lan, '_close_process', close)
    monkeypatch.setattr(lan, '_call', routes)
    async with lan.io:
        lan.start()
        await asyncio.sleep(0)
        assert not opened
        inventory[0] = 'new-membership'
        assert not lan.ready(family, NEW_DID)
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert opened == ['new-membership'] and lan.ready(family, NEW_DID)
        inventory[0] = 'second-revision'
        assert not lan.ready(family, NEW_DID)
    finally:
        await lan.close()
