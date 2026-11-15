"""在无 HA 的控制面验证狼人杀场景从建房到恢复的米家调光。"""

import asyncio
from functools import partial

import httpx
import pytest
from fastapi import FastAPI

from homeplane.actions import ActionController
from homeplane.core import HomeCore
from homeplane.local_camera_home import LocalCameraHome
from homeplane.mijia_power_home import MijiaPowerHome
from homeplane.store import TraceStore
from homeplane.werewolf import WerewolfGame
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects, SceneLights
from homeplane.werewolf_service import WerewolfService
from tests.test_homeplane_actions import close
from tests.test_mijia_numeric_home import LIGHT, setup_home, warm
from tests.test_werewolf import Clock, FixedRoles, confirm_deal
from tests.test_werewolf_service import NoNetwork


async def cold_home(tmp_path):
    core, baseline, gateway, ha_calls = await setup_home(tmp_path)
    power = core.actions.mijia
    await close(core)
    core = HomeCore(LocalCameraHome(mijia_selected=True), TraceStore(tmp_path / 'mijia-scene.sqlite'))
    core.actions = ActionController(core, tmp_path / 'action-authority.json')
    core.actions.mijia = power
    power.home = MijiaPowerHome(core, power)
    await warm(power.home)
    target = next(entity.id for entity in baseline.entities if entity.kind == 'light')
    return core, gateway, ha_calls, target


@pytest.mark.asyncio
async def test_host_uses_the_same_mijia_lamp_at_night_by_day_and_on_close(tmp_path):
    core, gateway, ha_calls, target = await cold_home(tmp_path)
    journal = tmp_path / 'scene.json'
    lock = asyncio.Lock()
    lights = SceneLights(core, lock, journal)
    owner = WerewolfService(HostEffects(lights), gateway_factory=NoNetwork,
                            addresses=lambda: ['192.168.50.20'], clock=Clock(),
                            game_factory=partial(WerewolfGame, rng=FixedRoles()))

    async def brightness_read(did, siid, piid):
        if (did, siid, piid) == (LIGHT, 2, 2):
            # 现场调光可以等待正常的局域网读回，独立于手势的短探测时限。
            await asyncio.sleep(.25)
        return gateway.read(did, siid, piid)

    gateway.manager.get_prop_async.side_effect = brightness_read
    app = FastAPI()
    app.include_router(create_router(core, lock, None, tmp_path, owner=owner))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://host.invalid',
                                     headers={'Origin': 'http://host.invalid'}) as host:
            result = await host.get('/api/games/werewolf/context')
            assert [lamp['id'] for lamp in result.json()['lights']] == [target]
            created = await host.post('/api/games/werewolf/create', json={
                'players': 6, 'address': '192.168.50.20', 'lights': [target],
            })
            assert created.status_code == 200, created.text
            assert lights.status()['selected'] == 1 and journal.exists()
            gateway.manager.set_prop_async.assert_not_called()
            owner.pump.cancel()
            await asyncio.gather(owner.pump, return_exceptions=True)
            owner.pump = None
            for seat in range(1, 7):
                owner.game.join(seat, f'合成玩家{seat}')
                owner.game.ready(seat, True)

            async def command(action):
                result = await host.post('/api/games/werewolf/control', json={
                    'action': action, 'session': owner.session, 'revision': owner.game.revision,
                    'event_id': f'synthetic-{action}-{owner.game.revision}',
                })
                assert result.status_code == 200, result.text
                if owner.effect_task is not None:
                    await owner.effect_task

            await command('start')
            confirm_deal(owner.game)
            await command('advance')
            assert gateway.fields[LIGHT, 2, 2] == 20
            for _ in range(6):
                if owner.game.phase == 'discussion':
                    break
                await command('advance')
            assert owner.game.phase == 'discussion' and gateway.fields[LIGHT, 2, 2] == 70
            await command('close')

        assert gateway.fields[LIGHT, 2, 2] == 40 and gateway.fields[LIGHT, 2, 1] is True
        assert [tuple(call.args) for call in gateway.manager.set_prop_async.await_args_list] == [
            (LIGHT, 2, 2, 20), (LIGHT, 2, 2, 70), (LIGHT, 2, 2, 40),
        ]
        assert not lights.status()['recovery_pending'] and not journal.exists()
        assert not ha_calls
        core.actions.mijia.request.assert_not_called()
    finally:
        await owner.close()
        await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_field', [(2, 85), (1, False)])
async def test_manual_dimming_or_poweroff_is_preserved_on_mijia(tmp_path, manual_field):
    core, gateway, ha_calls, target = await cold_home(tmp_path)
    try:
        lights = SceneLights(core, asyncio.Lock(), tmp_path / 'scene.json')
        await lights.select('SYNTHETIC', [target])
        await lights.scene(True, 1, lambda: True)
        piid, changed = manual_field
        gateway.fields[LIGHT, 2, piid] = changed
        outcome = await lights.restore()
        assert outcome['receipts'][-1]['status'] == 'left_unchanged'
        assert gateway.fields[LIGHT, 2, piid] == changed
        gateway.manager.set_prop_async.assert_awaited_once_with(LIGHT, 2, 2, 20)
        assert not ha_calls
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_unknown_mijia_dimming_is_not_replayed_and_can_restore_after_readback(tmp_path):
    core, gateway, ha_calls, target = await cold_home(tmp_path)
    journal = tmp_path / 'scene.json'
    try:
        lights = SceneLights(core, asyncio.Lock(), journal)
        await lights.select('SYNTHETIC', [target])
        gateway.lose_response = True
        await lights.scene(True, 1, lambda: True)
        assert lights.status()['recovery_pending'] and lights.status()['receipts'][-1]['status'] == 'unknown_outcome'
        await lights.scene(True, 1, lambda: True)
        gateway.manager.set_prop_async.assert_awaited_once_with(LIGHT, 2, 2, 20)

        gateway.lose_response = False
        reopened = SceneLights(core, asyncio.Lock(), journal)
        assert reopened.status()['recovery_pending'] and gateway.fields[LIGHT, 2, 2] == 20
        assert not (await reopened.restore())['recovery_pending']
        assert not (await reopened.restore())['recovery_pending']
        assert [tuple(call.args) for call in gateway.manager.set_prop_async.await_args_list] == [
            (LIGHT, 2, 2, 20), (LIGHT, 2, 2, 40),
        ]
        assert not ha_calls and not journal.exists()
    finally:
        await close(core)
