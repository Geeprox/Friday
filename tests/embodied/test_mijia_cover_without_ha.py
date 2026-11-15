"""从原手势 HTTP 入口到米家窗帘发令，HA 断开后使用同一目标。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from homeplane.embodied import api, interaction, miot_spec
from homeplane.embodied.cover_worker import run
from homeplane.embodied.mijia_inventory import account_household
from homeplane.embodied.mijia_lan import cover_operation, home_input
from homeplane.embodied.mijia_power import family_binding, grant_binding
from homeplane.embodied.ptz_control import atomic
from homeplane.embodied.smart_home import Curtain
from homeplane.local_camera_home import LocalCameraHome
from homeplane.mijia_control import MijiaPower
from tests.embodied.test_home_interaction import FakeAPI
from tests.test_homeplane_device_controls import rig


def cover_config():
    def prop(iid, name, access, **details):
        return {'iid': iid, 'type': f'urn:miot-spec-v2:property:{name}:00000000',
                'access': access, **details}

    properties = [
        prop(1, 'fault', ['read']),
        prop(2, 'motor-control', ['write'], **{'value-list': [
            {'value': index, 'description': action}
            for index, action in enumerate(('stop', 'close', 'open'))]}),
        prop(6, 'current-position', ['read'], **{'value-range': [0, 100, 1]}),
        prop(7, 'target-position', ['read', 'write'], **{'value-range': [0, 100, 1]}),
    ]
    return dict(version=1, cameras=[{'device': {'did': '200000001'}}], miot_covers={
        'selected': dict(did='100000001', name='测试窗帘', room='测试房间', feedback_quality='unreliable',
                         spec=dict(services=[dict(iid=2, properties=properties)])),
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['accepted', 'lan_accepted', 'lan_lost_response', 'lan_read_failure',
                                  'expired_in_worker', 'paused_in_worker', 'paused_and_resumed',
                                  'voice', 'voice_after_ha_gap', 'voice_other_room', 'manual',
                                  'lost_response', 'moved_home', 'other_account', 'restricted_grant'])
async def test_saved_curtain_gesture_uses_mijia_without_ha(tmp_path, monkeypatch, case):
    core, _, ha_calls, _, _, authority = rig(tmp_path)
    ha = core.adapter
    directory = tmp_path
    core.actions.authority_path = directory / 'action-authority.json'
    if case == 'restricted_grant':
        authority = authority.model_copy(update={'external_entity_ids': ['cover.simulated']})
    atomic(core.actions.authority_path, authority.model_dump(mode='json'))
    approved_bytes = core.actions.authority_path.read_bytes()
    config = cover_config()
    atomic(directory / 'config.json', config)
    saved = {'cloud_server': 'cn', 'user_info': {'uid': 'synthetic-account'}, 'oauth_uuid': 'synthetic-grant'}
    atomic(directory / 'miot-session.json', saved)
    atomic(directory / 'home-room-membership.json', {'home_id': 'synthetic-home'})
    monkeypatch.setattr(miot_spec, 'configuration', lambda: config)
    monkeypatch.setattr(api, 'PRIVATE', directory)
    monkeypatch.setattr(interaction, 'PRIVATE', directory)
    grant = grant_binding(saved)
    discovery = dict(state='ready', models={'cn_100000001': {'urn': 'synthetic-cover'}},
                     grant=grant, household=account_household(saved, 'synthetic-home'),
                     family=family_binding(grant, 'synthetic-home'), inputs=home_input(directory)[2])
    delegated = FakeAPI()
    if case == 'lost_response':
        delegated.set_prop_async = AsyncMock(side_effect=TimeoutError)

    did = '100000001'
    def local_value(_, siid, piid):
        return {'code': -1} if piid == 7 else {'code': 0, 'value': 0 if piid == 1 else 50}

    lan_get = AsyncMock(side_effect=local_value)
    lan_set = AsyncMock(return_value={'code': 0, 'did': did, 'siid': 2, 'piid': 2})
    if case == 'lan_lost_response':
        lan_set.side_effect = TimeoutError()
    if case == 'lan_read_failure':
        lan_get.return_value = None
        lan_get.side_effect = lambda *_: {'code': -1}
    lan = SimpleNamespace(can_control=lambda value: value == did, in_local_cooldown=lambda _: False,
                          local_device=lambda _: {'group_id': 'synthetic-group'},
                          get_prop_async=lan_get, set_prop_async=lan_set, note_local_failure=lambda _: None)

    async def local(payload):
        return await cover_operation(lan, {**payload, 'did': did, 'grant': grant,
                                     'binding': miot_spec.cover_revision(miot_spec.cover_binding(config))},
                                     grant, 'synthetic-group', {did}, lambda: True, directory)

    class CurtainFixture(Curtain):
        async def connect(self, *, binding, session, expected_grant, preflight):
            assert not preflight and expected_grant == grant
            self.api = delegated
            self.binding = binding
            self.get_parameter = self.set_parameter = SimpleNamespace
            from homeplane.embodied.smart_home import CURTAIN_DID
            self.did = CURTAIN_DID

        async def close(self):
            pass

        async def read(self):
            result = await super().read()
            if case == 'expired_in_worker':
                offset[0] += 2
            if case == 'paused_in_worker':
                core.actions.pause()
            if case == 'paused_and_resumed':
                core.actions.pause()
                core.actions.resume(core.actions.policy_report()['revision'])
            return result

    async def miot_cover(action, event_id, expected_revision, *, lan=None, **context):
        request = dict(action=action, event_id=event_id, expected_revision=expected_revision, **context)
        result = await run(request, directory=directory, curtain_factory=CurtainFixture,
                           local=local if case.startswith('lan_') else None)
        if result.get('status') == 'local_preflight_unavailable':
            return await run(request, directory=directory, curtain_factory=CurtainFixture)
        return result

    service = SimpleNamespace(discovery_report=discovery, discovery_checked_at=time.monotonic(),
                              miot_cover=AsyncMock(side_effect=miot_cover), practice_deadline=0)
    core.actions.mijia = MijiaPower(service)
    offset = [0.0]
    from homeplane.embodied import cover_worker
    monkeypatch.setattr(cover_worker, 'monotonic', lambda: time.monotonic() + offset[0])
    worker_key = 'synthetic-worker-key'
    (directory / 'control-key').write_text(worker_key)
    binding_id = 'b' * 32
    target = 'friday:configured-cover'
    atomic(directory / 'interactions.json', dict(revision=3, enabled=True, bindings=[
        dict(id=binding_id, gesture='body.arms_open', target=target, action='open', enabled=True)]))
    try:
        await core.snapshot()
        core.actions.migrate_cover_authority()
        assert (directory / 'mijia-cover-authority.json').exists() is (case != 'restricted_grant')
        assert core.actions.authority_path.read_bytes() == approved_bytes
        if case == 'voice_after_ha_gap':
            ha._gap = True
        else:
            core.adapter = LocalCameraHome()
        ha_calls.clear()
        if case == 'moved_home':
            atomic(directory / 'home-room-membership.json', {'home_id': 'other-home'})
        if case == 'other_account':
            atomic(directory / 'miot-session.json', {**saved, 'user_info': {'uid': 'other-account'}})
        # 精确目标的交互不读取 HA 假数据，即使 HA 响应会长时间挂起。
        core.snapshot = AsyncMock(side_effect=AssertionError('unexpected household snapshot'))
        app = FastAPI()
        pending_ha = asyncio.Lock()
        await pending_ha.acquire()
        app.include_router(api.create_router(service, core, pending_ha))
        if case == 'manual':
            from pathlib import Path
            from unittest.mock import Mock
            from homeplane.home_experience import create_router

            runtime = SimpleNamespace(hold_for_manual=AsyncMock())
            settings = SimpleNamespace(path=Path(directory) / 'settings.json')
            service.safety_output = Mock()
            app.include_router(create_router(core, pending_ha, settings, service, None, runtime))
        event = {'target': target, 'action': 'open', 'event_id': 'synthetic-gesture',
                 'camera': 'cam_1111111111', 'gesture': {
                     'binding': binding_id, 'revision': 3, 'observed_mono': time.monotonic()}}
        endpoint = '/api/embodied/command'
        if case in {'voice', 'voice_after_ha_gap', 'voice_other_room'}:
            event = {'text': '打开窗帘', 'event_id': 'synthetic-voice', 'camera': 'cam_1111111111',
                     'room': '另一间房' if case == 'voice_other_room' else '测试房间'}
        if case == 'manual':
            endpoint = '/api/home/control'
            event = dict(target=target, operation='cover.state', desired={'state': 'open'},
                         event_id='synthetic-button',
                         expected_state_version=miot_spec.cover_revision(miot_spec.cover_binding(config)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                     headers={'X-Friday-Worker': worker_key}) as client:
            response = await asyncio.wait_for(client.post(endpoint, json=event), 4)
            assert response.status_code == 200
            result = response.json()
            if case == 'manual':
                result = result['result']
            if case in {'accepted', 'lost_response', 'lan_accepted', 'lan_lost_response',
                        'lan_read_failure', 'voice', 'voice_after_ha_gap', 'manual'}:
                assert result['write_attempted'] and not result['reached_target'], result
                assert result['status'] == ('unknown_outcome' if 'lost_response' in case else 'accepted')
                retry = await client.post(endpoint, json=event)
                assert retry.json()['status'] == result['status']
                if case in {'accepted', 'voice', 'voice_after_ha_gap', 'manual'}:
                    assert len(delegated.writes) == 1
                    core.actions.pause()
                    assert not core.actions.cover_permission()
                    core.actions.resume(core.actions.policy_report()['revision'])
                    assert core.actions.cover_permission()
                elif case == 'lost_response':
                    delegated.set_prop_async.assert_awaited_once()
                elif case == 'lan_read_failure':
                    lan_set.assert_not_awaited()
                    assert len(delegated.writes) == 1
                else:
                    lan_set.assert_awaited_once_with(did, 2, 2, 2)
                    assert not delegated.writes
            else:
                assert not result['write_attempted'] and not delegated.writes
                assert not (directory / 'cover-ledger.json').exists()
        core.snapshot.assert_not_awaited()
        assert not ha_calls
    finally:
        core.actions.close()
        await ha.close()
        core.store.close()
