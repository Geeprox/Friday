"""使用现有控制入口验证米家中枢接管。不连接家庭或硬件。"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from homeplane.actions import ActionController
from homeplane.core import HomeCore
from homeplane.embodied.household import DeviceCommands, basic_command
from homeplane.embodied.mijia_inventory import account_household
from homeplane.embodied.mijia_lan import (
    PropertyNotifications,
    home_input,
    lan_replies,
    migrated_power_operation,
    migrated_property_read,
    numeric_operation,
    property_operation,
)
from homeplane.embodied.mijia_power import family_binding, grant_binding
from homeplane.embodied.ptz_control import atomic
from homeplane.home_experience import create_router
from homeplane.local_camera_home import LocalCameraHome
from homeplane.mijia_climate_mode import climate_mode_operation
from homeplane.mijia_control import MijiaPower
from homeplane.mijia_lan_client import MijiaLanClient
from homeplane.mijia_enrollment import native_power_operation
from homeplane.mijia_power_home import MijiaPowerHome, catalog_available
from homeplane.settings import SettingsService
from homeplane.store import TraceStore
from tests.test_home_experience import Embodied, Runtime
from tests.test_homeplane_actions import close
from tests.test_homeplane_device_controls import plan, rig

DID = '200000001'
IDENTITY = f'cn_{DID}'
URN = 'urn:miot-spec-v2:device:switch:00000001:fixture-channel:1'
MODEL = 'fixture.switch.channel'


@pytest.mark.asyncio
async def test_lan_notifications_discard_old_callbacks_and_return_property_keys_only():
    callbacks = {}
    denied = {'200000000'}
    visible = {'200000000', DID}
    groups = {}

    async def subscribe(did, callback):
        if did in denied:
            raise ValueError('synthetic_rejection')
        callbacks[did] = callback
        groups[did] = {'synthetic-group'}

    manager = SimpleNamespace(can_control=lambda did: did in visible, can_push=lambda _did: True,
                              in_local_cooldown=lambda _did: False,
                              local_device=lambda _did: {'group_id': 'synthetic-group'},
                              props_sub_groups=lambda did: groups.get(did, set()),
                              sub_device_props_async=subscribe, unsub_device_props=lambda did: groups.pop(did, None))
    watch = [[did, 2, 1] for did in sorted(visible)]
    notifications = PropertyNotifications(manager, 'synthetic-group', set(visible))
    await notifications.fetch(watch)  # 第一个设备被拒绝。
    await notifications.fetch(watch)
    callback = callbacks[DID]
    event = SimpleNamespace(did=DID, changes=[SimpleNamespace(siid=2, piid=1, value=True),
                                              SimpleNamespace(siid=2, piid=3, value=False)])
    callback(event)
    callback(event)
    await asyncio.sleep(0)
    assert (await notifications.fetch(watch))['changes'] == [(DID, 2, 1)]
    assert (await notifications.fetch(watch))['changes'] == []
    visible.remove(DID)
    await notifications.fetch(watch)
    visible.add(DID)
    await notifications.fetch(watch)
    callback(event)
    await asyncio.sleep(0)
    assert (await notifications.fetch(watch))['changes'] == []
    only_device = [[DID, 2, 1]]
    assert (await notifications.fetch(only_device))['changes'] == []
    old_transport = callbacks[DID]
    assert old_transport is not callback
    old_transport(event)
    await asyncio.sleep(0)
    notifications.transport_reset()
    assert notifications.generation == 1
    assert not notifications.dirty
    assert (await notifications.fetch(only_device))['changes'] == []
    old_transport(event)
    await asyncio.sleep(0)
    assert (await notifications.fetch(only_device))['changes'] == []
    callbacks[DID](event)
    await asyncio.sleep(0)
    assert (await notifications.fetch(only_device))['changes'] == [(DID, 2, 1)]
    with pytest.raises(ValueError, match='mijia_push_invalid'):
        await notifications.fetch([['300000000', 2, 1]])


class Gateway:
    def __init__(self, path, service, fields=None):
        self.path, self.service = path, service
        self.io = asyncio.Lock()
        self.value = False
        self.calls = []
        self.online = True
        self.route_tokens = {}
        self.lose_response = False
        self.before_dispatch = lambda: None
        self.fields = fields if fields is not None else {}
        self.manager = SimpleNamespace(
            can_control=lambda did: self.online and did in self.dids,
            in_local_cooldown=lambda _: False,
            local_device=lambda _: {'group_id': 'synthetic-group'},
            get_prop_async=AsyncMock(side_effect=self.read),
            set_prop_async=AsyncMock(side_effect=self.write),
        )

    def ready(self, family, did=None):
        return self.online and family == self.service.discovery_report['family'] and (did is None or did in self.dids)

    def route_token(self, family, did):
        return self.route_tokens.setdefault(did, object()) if self.ready(family, did) else None

    @property
    def dids(self):
        return {DID, *(did for did, _, _ in self.fields)}

    def read(self, did, siid, piid):
        value = self.value if not self.fields else self.fields.get((did, siid, piid))
        return {'code': 0, 'value': value}

    def write(self, did, siid, piid, value):
        if self.fields:
            self.fields[did, siid, piid] = value
        else:
            self.value = value
        if self.lose_response:
            raise httpx.ReadTimeout('synthetic')
        return {'code': 0, 'did': did, 'siid': siid, 'piid': piid}

    async def request(self, payload, *, family, guard=None):
        async with self.io:
            self.calls.append(dict(payload))
            args = self.manager, payload, self.service.discovery_report['grant']
            if guard is not None:
                assert guard()
                # 模拟父进程完成派发、子进程消费命令前撤销授权。
                self.before_dispatch()
            if payload.get('numeric') is True:
                return await numeric_operation(
                    *args, family, 'cn', 'synthetic-group', self.dids, lambda: True, self.path,
                )
            if 'registered_climate_mode' in payload:
                return await climate_mode_operation(
                    *args, family, 'cn', 'synthetic-group', self.dids, lambda: True, self.path,
                )
            if 'native_power' in payload:
                return await native_power_operation(
                    *args, family, 'cn', 'synthetic-group', self.dids, lambda: True, self.path,
                )
            if guard is not None:
                return await migrated_power_operation(
                    *args, family, 'cn', 'synthetic-group', self.dids, lambda: True, self.path,
                )
            if payload.get('registered_power') is True:
                return await migrated_property_read(
                    *args, family, 'cn', 'synthetic-group', self.dids, lambda: True, self.path,
                )
            return await property_operation(*args, 'synthetic-group', self.dids, lambda: True)


async def provision(tmp_path, *, configurable=False, allowed=True, duplicate=False):
    core, rows, ha_calls, _, path, authority = rig(tmp_path)
    core.actions.authority_path = path.with_name('action-authority.json')
    restricted = authority.model_copy(update={
        'external_entity_ids': ['switch.simulated'] if allowed else ['light.simulated'],
    })
    atomic(core.actions.authority_path, restricted.model_dump(mode='json'))
    saved = {'cloud_server': 'cn', 'user_info': {'uid': 'synthetic-account'}, 'oauth_uuid': 'synthetic-grant'}
    atomic(tmp_path / 'miot-session.json', saved)
    atomic(tmp_path / 'home-room-membership.json', {'home_id': 'synthetic-home'})
    grant = grant_binding(saved)
    row = next(row for row in rows if row['entity_id'] == 'switch.simulated')
    row['attributes']['friendly_name'] = '客厅插座'
    if duplicate:
        rows.append({**row, 'entity_id': 'switch.second'})
    service = SimpleNamespace(discovery_report=dict(
        state='ready', models={IDENTITY: {'urn': URN, 'model': MODEL}}, grant=grant,
        household=account_household(saved, 'synthetic-home'), family=family_binding(grant, 'synthetic-home'),
        inputs=home_input(tmp_path)[2],
    ), discovery_checked_at=time.monotonic())
    gateway = Gateway(tmp_path, service)
    cloud = AsyncMock(side_effect=AssertionError('不得访问米家云端'))
    inventory = core.adapter._inventory

    async def registry():
        await inventory()
        names = ('switch.simulated', 'switch.second') if duplicate else ('switch.simulated',)
        for external in names:
            core.adapter._registry[external].update(
                platform='xiaomi_home', unique_id=f'xiaomi_home.fixture_{IDENTITY}_channel_on_p_2_1')
            core.adapter._devices[f'device-{external}']['identifiers'] = [['xiaomi_home', IDENTITY]]

    spec = {'type': URN, 'services': [{'iid': 2, 'type': 'urn:miot-spec-v2:service:switch:00000000',
        'properties': [{'iid': 1, 'type': 'urn:miot-spec-v2:property:on:00000000',
                        'format': 'bool', 'access': ['read', 'write']}]}]}
    if configurable:
        spec['services'][0]['properties'].append(
            {'iid': 2, 'type': 'urn:miot-spec-v2:property:mode:00000000', 'access': ['read']})
    core.adapter._inventory = registry
    core.actions.mijia = MijiaPower(service, spec=AsyncMock(return_value=spec), request=cloud, lan=gateway)
    core.actions.mijia.specs[URN] = spec
    core.actions.mijia.home = MijiaPowerHome(core, core.actions.mijia)
    snapshot = await core.snapshot()
    core.actions.mijia.home.migrate()
    return core, snapshot, gateway, ha_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('outage', ['live', 'cold'])
async def test_manual_voice_and_target_binding_survive_ha_outage(tmp_path, outage):
    core, baseline, gateway, ha_calls = await provision(tmp_path)
    prev = next(e for e in baseline.entities if e.kind == 'switch')
    previous_device = next(d for d in baseline.devices if prev.id in d.function_ids)
    assert catalog_available(tmp_path)
    ha = core.adapter
    try:
        if outage == 'cold':
            power = core.actions.mijia
            await close(core)
            core = HomeCore(LocalCameraHome(), TraceStore(tmp_path / 'SIMULATED-cold.sqlite'))
            core.actions = ActionController(core, tmp_path / 'action-authority.json')
            core.actions.mijia = power
            power.home = MijiaPowerHome(core, power)
        else:
            await ha._stream_gap('synthetic_disconnect')
            ha.snapshot = AsyncMock(side_effect=AssertionError('不应为米家状态等待 HA'))
        home = core.actions.mijia_power_home
        before = await core.snapshot()
        assert before.mode == 'mijia-lan' and not before.observations and not before.action_targets
        await home.sample_once()
        after = await core.snapshot()
        assert [d.id for d in after.devices] == [previous_device.id]
        assert [(e.id, e.name, e.room) for e in after.entities] == [(prev.id, prev.name, prev.room)]
        assert after.states[0].value == 'off' and after.observations[0].ingest_source == 'mijia-lan'
        assert after.action_targets == [prev.id] and not after.rules
        assert not any(c.access == 'event' for e in after.entities for c in e.capabilities)

        lock = asyncio.Lock()
        app = FastAPI()
        app.include_router(create_router(
            core, lock, SettingsService(tmp_path / 'settings.json'), Embodied(), None, Runtime(),
        ))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            view = (await client.get('/api/home/overview')).json()
            device = next(d for d in view['devices'] if d['id'] == previous_device.id)
            assert view['connected'] and device['controls'][0]['value'] == 'off'
            req = dict(target=prev.id, operation='switch.set', desired={'power': 'on'},
                       expected_state_version=device['controls'][0]['state_version'], event_id='synthetic-control')
            result = (await client.post('/api/home/control', json=req)).json()
            assert result['route'] == 'mijia-scoped-power' and result['status'] == 'reported_match'
            assert (await client.post('/api/home/control', json=req)).json() == result
            view = (await client.get('/api/home/overview')).json()
            assert next(d for d in view['devices'] if d['id'] == device['id'])['controls'][0]['value'] == 'on'

        command = basic_command('Friday，关掉客厅插座')
        assert command and command['route'] == 'home'
        router = DeviceCommands(core, lock)
        voice = await router.command(command['device'], command['action'], 'synthetic-voice')
        assert voice['status'] == 'reported_match'
        gesture = await router.command(None, 'on', 'synthetic-gesture', target=prev.id,
                                       dispatch_guard=lambda: True, dispatch_deadline=time.monotonic() + 1.5)
        assert gesture['status'] == 'reported_match'
        assert gateway.manager.set_prop_async.await_count == 3
        assert not ha_calls
        core.actions.mijia.request.assert_not_called()
    finally:
        await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['stale_input', 'lost_reply', 'revoke_while_queued', 'family_changed', 'plan_from_ha'])
async def test_migrated_plan_cannot_change_source_or_retry_send(tmp_path, case):
    core, _, gateway, ha_calls = await provision(tmp_path)
    try:
        if case == 'plan_from_ha':
            intent = await plan(core, 'switch.set', {'power': 'on'})
        await core.adapter._stream_gap('synthetic_disconnect')
        await core.actions.mijia.home.sample_once()
        if case != 'plan_from_ha':
            intent = await plan(core, 'switch.set', {'power': 'on'})
        if case == 'stale_input':
            gateway.value = True
        if case == 'lost_reply':
            gateway.lose_response = True
        if case == 'revoke_while_queued':
            gateway.before_dispatch = core.actions.pause
        if case == 'family_changed':
            atomic(tmp_path / 'home-room-membership.json', {'home_id': 'other-synthetic-home'})
        result = await core.actions.execute(intent.action_id)
        assert result == await core.actions.execute(intent.action_id)
        if case == 'lost_reply':
            assert result.status == 'unknown_outcome'
            # 旧桌面版能读入同一发送记录，回滚时因已领取而不会重新发送。
            stored = json.loads(core.store.connection.execute(
                'SELECT body FROM light_actions WHERE id = ?', (intent.action_id,),
            ).fetchone()[0])
            assert stored['route'] == 'ha-scoped-device' and stored['real_write_attempted']
            assert core.store.light_action(intent.action_id).route == 'mijia-scoped-power'
            result = await core.actions.reconcile(intent.action_id)
            assert result.status == 'reported_match' and result.reported['evidence'] == 'mijia_lan_readback'
            assert gateway.manager.set_prop_async.await_count == 1
        else:
            gateway.manager.set_prop_async.assert_not_called()
            if case == 'revoke_while_queued':
                assert result.status == 'unknown_outcome'
                report = core.actions.policy_report()
                assert report['enabled'] is False
                core.actions.resume(report['revision'])
                assert (await core.snapshot()).action_targets
            else:
                assert result.status == 'rejected'
        assert not ha_calls
        core.actions.mijia.request.assert_not_called()
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_mijia_direct_requires_fresh_state_and_a_new_plan_after_reconnect(tmp_path):
    core, _, gateway, ha_calls = await provision(tmp_path)
    original_adapter = core.adapter
    core.adapter = LocalCameraHome(mijia_selected=True)
    try:
        home = core.actions.mijia.home
        await home.sample_once()
        context, targets = home.active()
        target = next(iter(targets.values()))
        assert target.entity.kind == 'switch'
        before = home.sample_for(context, target)
        assert before is not None and before.value == 'off'
        intent = await plan(core, 'switch.set', {'power': 'on'})
        assert intent.status == 'planned'
        old_writer = home.writer(target.entity.id)

        gateway.route_tokens.pop(DID)
        assert home.sample_for(context, target) is None
        assert not (await core.snapshot()).action_targets
        with pytest.raises(ValueError):
            await old_writer.readback()
        await home.sample_once()
        after = home.sample_for(context, target)
        assert after is not None and after.value == before.value and after.version != before.version
        rejected = await core.actions.execute(intent.action_id)
        assert rejected.status == 'rejected'
        gateway.manager.set_prop_async.assert_not_called()

        fresh = await plan(core, 'switch.set', {'power': 'on'})
        executed = await core.actions.execute(fresh.action_id)
        assert executed.status == 'reported_match'
        gateway.manager.set_prop_async.assert_awaited_once()
        assert not ha_calls
    finally:
        await original_adapter.close()
        await close(core)


@asynccontextmanager
async def local_transport(gateway):
    """用内存管道连接实际父进程协议与中枢命令处理器。"""
    family = gateway.service.discovery_report['family']
    grant = gateway.service.discovery_report['grant']
    incoming, outgoing = asyncio.StreamReader(), asyncio.StreamReader()
    lan = MijiaLanClient(lambda: family)
    lan.family = family
    lan.process = SimpleNamespace(returncode=None, stdout=outgoing,
                                  stdin=SimpleNamespace(write=incoming.feed_data, drain=AsyncMock()))
    gateway.manager.is_ready = True
    gateway.manager.can_push = lambda _: False
    notifications = PropertyNotifications(gateway.manager, 'synthetic-group', gateway.dids)

    async def run():
        async for reply in lan_replies(incoming, notifications, family=family, region='cn',
                                       grant=grant, directory=gateway.path, current=lambda: True):
            outgoing.feed_data(json.dumps(reply).encode() + b'\n')
        outgoing.feed_eof()

    worker = asyncio.create_task(run())
    try:
        await lan._refresh_routes(family)
        yield lan, notifications
    finally:
        incoming.feed_eof()
        await asyncio.wait_for(worker, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('reset', ['health_readback', 'before_preflight', 'after_preflight', 'during_write'])
async def test_sdk_replacement_with_same_devices_revokes_readings_and_never_replays_a_write(tmp_path, reset):
    core, _, gateway, ha_calls = await provision(tmp_path)
    original_adapter = core.adapter
    core.adapter = LocalCameraHome(mijia_selected=True)
    try:
        async with local_transport(gateway) as (lan, notifications):
            core.actions.mijia.lan = lan
            home = core.actions.mijia.home
            await home.sample_once()
            context, targets = home.active()
            target = next(iter(targets.values()))
            family = context[0]
            route = lan.route_token(family, DID)
            await lan._refresh_routes(family)
            assert lan.route_token(family, DID) is route
            reading = home.sample_for(context, target)
            assert reading is not None and reading.value == 'off'
            intent = await plan(core, 'switch.set', {'power': 'on'})
            assert intent.status == 'planned'
            if reset == 'during_write':
                def send_with_reset(did, siid, piid, value):
                    response = gateway.write(did, siid, piid, value)
                    notifications.transport_reset()
                    return response
                gateway.manager.set_prop_async.side_effect = send_with_reset
            elif reset == 'after_preflight':
                request = lan.request

                async def request_with_reset(payload, **kwargs):
                    if 'power' in payload and notifications.generation == 0:
                        notifications.transport_reset()
                    return await request(payload, **kwargs)

                lan.request = request_with_reset
            else:
                notifications.transport_reset()
            if reset == 'health_readback':
                await lan._refresh_routes(family)
            else:
                assert lan.route_token(family, DID) is route
            result = await core.actions.execute(intent.action_id)
            assert result.status == ('unknown_outcome' if reset in ('after_preflight', 'during_write') else 'rejected')
            assert result == await core.actions.execute(intent.action_id)
            assert gateway.manager.set_prop_async.await_count == int(reset == 'during_write')
            assert home.sample_for(context, target) is None

            await lan._refresh_routes(family)
            assert lan.routes == frozenset({DID})
            assert lan.route_token(family, DID) is not route
            await home.sample_once()
            refreshed = home.sample_for(context, target)
            assert refreshed is not None and refreshed.version != reading.version
            if reset == 'during_write':
                assert refreshed.value == 'on'
                confirmed = await core.actions.reconcile(intent.action_id)
                assert confirmed.status == 'reported_match'
            else:
                assert refreshed.value == 'off'
                fresh = await plan(core, 'switch.set', {'power': 'on'})
                assert (await core.actions.execute(fresh.action_id)).status == 'reported_match'
            gateway.manager.set_prop_async.assert_awaited_once_with(DID, 2, 1, True)
            core.actions.mijia.request.assert_not_called()
            assert not ha_calls
    finally:
        await original_adapter.close()
        await close(core)


@pytest.mark.asyncio
async def test_lan_route_tokens_are_local_to_each_device_and_fence_an_io_waiter(monkeypatch):
    from homeplane import mijia_lan_client
    from unittest.mock import Mock

    family, peer = 'a' * 64, '222222222'
    now = [20.]
    monkeypatch.setattr(mijia_lan_client, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    lan = MijiaLanClient(lambda: family)
    lan.family = family
    lan.process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock()))
    lan._update_routes([DID, peer], True, 2)
    first, other = lan.route_token(family, DID), lan.route_token(family, peer)
    assert first is not None and other is not None
    async with lan.io:
        job = asyncio.create_task(lan.request({'did': DID, 'power': 'on'}, family=family))
        await asyncio.sleep(0)
        lan._update_routes([peer], True, 1)
        assert lan.route_token(family, DID) is None
        lan._update_routes([DID, peer], True, 2)
    with pytest.raises(ValueError, match='mijia_permission_changed'):
        await job
    lan.process.stdin.write.assert_not_called()
    assert lan.route_token(family, DID) is not first
    assert lan.route_token(family, peer) is other
    now[0] += 13
    assert lan.route_token(family, peer) is None
    lan._update_routes([DID, peer], True, 2)
    assert lan.route_token(family, peer) is not other


@pytest.mark.asyncio
@pytest.mark.parametrize('excluded', ['mode', 'grant', 'duplicate'])
async def test_unqualified_power_targets_are_not_persisted(tmp_path, excluded):
    core, _, gateway, _ = await provision(tmp_path, configurable=excluded == 'mode', allowed=excluded != 'grant',
                                         duplicate=excluded == 'duplicate')
    try:
        assert not catalog_available(tmp_path)
        assert not gateway.calls
    finally:
        await close(core)
