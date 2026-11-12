"""用原主持、手机和公屏接口验证换地址后的同局恢复。"""

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from homeplane.werewolf import WerewolfGame
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import NETWORK_CHECK_SECONDS, WerewolfService, phone_app
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_seat_recovery import bearer, join_all, proof, recovery
from tests.test_werewolf_service import SilentLights


OLD_ADDRESS = '192.0.2.10'
NEW_ADDRESS = '192.0.2.11'
HOST = '/api/games/werewolf'
TV = bearer('c' * 64)


class Gateway:
    def __init__(self, network):
        self.network = network
        self.running = False

    async def open(self, address, owner):
        if self.network.bind_failure:
            raise OSError('synthetic_bind_failure')
        self.running = True
        self.network.opened.append(self)
        return f'http://{address}:{8000 + len(self.network.opened)}'

    async def close(self):
        self.running = False
        self.network.closed.append(self)


def party_client(service):
    origin = service.origin
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=phone_app(service, origin)), base_url=origin)


@asynccontextmanager
async def match(tmp_path):
    network = SimpleNamespace(addresses=[OLD_ADDRESS], opened=[], closed=[], bind_failure=False)
    clock = Clock()
    service = WerewolfService(
        HostEffects(SilentLights()), gateway_factory=partial(Gateway, network),
        addresses=lambda: network.addresses, clock=clock,
        game_factory=partial(WerewolfGame, rng=FixedRoles()),
    )
    app = FastAPI()
    app.include_router(create_router(None, None, None, tmp_path, owner=service))
    origin = 'http://host.invalid'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                    headers={'Origin': origin}) as host:
            created = await host.post(HOST + '/create', json=dict(players=6, address=OLD_ADDRESS))
            assert created.status_code == 200
            service.pump.cancel()
            await asyncio.gather(service.pump, return_exceptions=True)
            service.pump = None
            async with party_client(service) as party:
                yield service, clock, network, host, party
    finally:
        await service.close()


async def phase(host, service, action, event=None):
    return await host.post(HOST + '/control', json=proof(service, event or action, action=action))


async def first_night(host, service, party):
    players = await join_all(service, party)
    assert (await phase(host, service, 'start')).status_code == 200
    for headers in players.values():
        seen = await party.post('/api/identity', json={'revision': service.game.revision}, headers=headers)
        assert seen.status_code == 200
    assert (await phase(host, service, 'advance')).json()['game']['phase'] == 'wolves'
    return players


async def pair_tv(host, service, party):
    pairing = await party.post('/api/display-pairing', headers=TV)
    assert pairing.status_code == 200
    code = pairing.json()['code']
    assert (await host.post(HOST + '/display-pairing', json=dict(session=service.session, code=code))).status_code == 200
    return code


@pytest.mark.asyncio
async def test_new_address_keeps_seat_identity_choice_clock_and_public_screen(tmp_path):
    async with match(tmp_path) as (service, clock, network, host, old_party):
        players = await first_night(host, service, old_party)
        await pair_tv(host, service, old_party)
        old_public_token = bearer(service.public_token)
        expired_ticket = await recovery(host, service, 'old-address')
        choice = dict(action='attack', target=3, revision=service.game.revision,
                      event_id='synthetic-attack', expected_choice_version=0)
        saved = await old_party.post('/api/choice', json=choice, headers=players[1])
        assert saved.json()['submitted'] == ['attack', 3] and saved.json()['choice_version'] == 1
        deadline, session, game, old_gateway = service.game.deadline, service.session, service.game, service.gateway
        old_board = (await old_party.get('/api/public', headers=TV)).json()['game']
        network.addresses = []
        clock.now += NETWORK_CHECK_SECONDS
        await service.step()
        interrupted = (await host.get(HOST + '/host')).json()
        assert interrupted['network'] == dict(state='interrupted', revision=1, addresses=[])
        assert interrupted['game']['paused'] and interrupted['game']['phase'] == 'wolves'
        remaining = interrupted['game']['seconds_left']
        assert remaining == deadline - clock.now
        assert interrupted['phone_url'] is interrupted['display_entry'] is None
        assert (await phase(host, service, 'resume')).json()['detail'] == 'party_link_unavailable'
        assert (await old_party.get('/api/player', headers=players[1])).status_code == 409

        network.addresses = [NEW_ADDRESS]
        clock.now = deadline + NETWORK_CHECK_SECONDS
        await service.step()
        frozen = (await host.get(HOST + '/host')).json()
        assert frozen['game']['phase'] == 'wolves' and frozen['game']['seconds_left'] == remaining
        assert frozen['network']['addresses'] == [NEW_ADDRESS]
        command = proof(service, 'new-address', address=NEW_ADDRESS, network_revision=frozen['network']['revision'])
        rebound = await host.post(HOST + '/reconnect', json=command)
        assert rebound.status_code == 200 and rebound.headers['cache-control'] == 'no-store'
        assert rebound.json()['session'] == session and service.game is game
        assert rebound.json()['network'] == dict(state='ready', revision=2, addresses=[])
        assert rebound.json()['game'] == frozen['game']
        assert len(network.opened) == 2 and network.closed == [old_gateway]
        assert (await host.post(HOST + '/reconnect', json=command)).json() == rebound.json()
        assert len(network.opened) == 2
        assert (await old_party.get('/api/player', headers=players[1])).status_code == 409
        assert (await old_party.get('/api/public', headers=TV)).status_code == 403
        async with party_client(service) as party:
            assert (await party.post('/api/recover-seat', json=expired_ticket)).status_code == 409
            assert (await party.get('/api/public', headers=TV)).status_code == 403
            assert (await party.get('/api/public', headers=old_public_token)).status_code == 403
            code = await pair_tv(host, service, party)
            resumed_board = (await party.get('/api/public', headers=TV)).json()['game']
            assert resumed_board['paused'] and resumed_board['players'] == old_board['players']
            assert all('role' not in player for player in resumed_board['players'])
            assert 'network' not in (await party.get('/api/public', headers=TV)).json()
            assert (await host.get(HOST + '/host')).json()['connected_display_codes'] == [code]
            claim = await recovery(host, service, 'new-phone')
            recovered = await party.post('/api/recover-seat', json=claim)
            assert recovered.status_code == 200 and recovered.json()['session'] == session
            private = recovered.json()['player']
            assert private['role'] == 'wolf' and service.game.identities_confirmed == set(players)
            assert private['submitted'] == ['attack', 3] and private['choice_version'] == 1
            assert (await party.get('/api/player', headers=players[1])).status_code == 403
            headers = {**bearer(recovered.json()['token']), 'X-Friday-Choice': choice['event_id']}
            assert (await party.get('/api/player', headers=headers)).json()['choice_receipt']['recorded']
            assert (await party.get('/api/player', headers=TV)).status_code == 403
            resumed = (await phase(host, service, 'resume', 'resume-rebound')).json()['game']
            assert not resumed['paused'] and resumed['phase'] == 'wolves' and resumed['seconds_left'] == remaining
            assert (await party.get('/api/public', headers=TV)).json()['game'] == resumed


@pytest.mark.asyncio
async def test_address_return_uses_original_connection_and_retains_host_pause(tmp_path):
    async with match(tmp_path) as (service, clock, network, host, party):
        players = await first_night(host, service, party)
        code = await pair_tv(host, service, party)
        origin = service.origin
        network.addresses = None
        clock.now += NETWORK_CHECK_SECONDS
        await service.step()
        assert service.host()['network']['state'] == 'ready'
        assert not service.game.paused

        network.addresses = []
        # 即使定时枚举尚未到期，环节到点前仍要重新核对原地址。
        clock.now = service.game.deadline
        service.network_check_at = clock.now + NETWORK_CHECK_SECONDS
        await service.step()
        assert service.game.paused and service.game.phase == 'wolves'
        assert (await party.post('/api/display-pairing', headers=TV)).status_code == 409

        network.addresses = [OLD_ADDRESS]
        clock.now += NETWORK_CHECK_SECONDS
        await service.step()
        assert service.host()['network']['state'] == 'ready' and service.game.paused
        assert service.origin == origin and len(network.opened) == 1
        assert (await party.get('/api/player', headers=players[1])).json()['role'] == 'wolf'
        assert (await party.get('/api/public', headers=TV)).status_code == 200
        assert service.host()['connected_display_codes'] == [code]


@pytest.mark.asyncio
async def test_failed_rebind_or_outdated_network_snapshot_keeps_the_existing_room(tmp_path):
    async with match(tmp_path) as (service, clock, network, host, party):
        await first_night(host, service, party)
        original, session = service.gateway, service.session
        original.running = False
        await service.step()
        snapshot = service.host()
        assert snapshot['network']['state'] == 'interrupted' and snapshot['game']['paused']
        command = proof(service, 'replace-listener', address=OLD_ADDRESS,
                        network_revision=snapshot['network']['revision'])
        stale = await host.post(HOST + '/reconnect', json={**command, 'network_revision': 0})
        assert stale.json()['detail'] == 'phone_network_changed'
        unavailable = await host.post(HOST + '/reconnect', json={**command, 'address': NEW_ADDRESS})
        assert unavailable.json()['detail'] == 'phone_address_unavailable'
        network.bind_failure = True
        failed = await host.post(HOST + '/reconnect', json=command)
        assert failed.status_code == 409 and failed.json()['detail'] == 'game_unavailable'
        assert service.host() == snapshot and service.gateway is original and network.closed == []
        network.bind_failure = False
        recovered = await host.post(HOST + '/reconnect', json=command)
        assert recovered.json()['session'] == session and recovered.json()['game']['paused']
        assert service.gateway is not original and network.closed == [original]
