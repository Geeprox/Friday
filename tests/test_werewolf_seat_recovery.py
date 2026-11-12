"""通过原主持和手机接口验证单座交接，所有输出使用合成数据。"""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from homeplane.werewolf import WerewolfGame
from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import SEAT_RECOVERY_SECONDS, WerewolfService, phone_app
from tests.test_werewolf import Clock, FixedRoles
from tests.test_werewolf_join_recovery import attempt
from tests.test_werewolf_service import NoNetwork, SilentLights


@asynccontextmanager
async def room(tmp_path):
    clock = Clock()
    service = WerewolfService(
        HostEffects(SilentLights()), gateway_factory=NoNetwork,
        addresses=lambda: ['192.168.50.20'], clock=clock,
        game_factory=lambda count, **kwargs: WerewolfGame(count, rng=FixedRoles(), **kwargs),
    )
    app = FastAPI()
    app.include_router(create_router(None, None, None, tmp_path, owner=service))
    host_origin = 'http://127.0.0.1:18743'
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=host_origin, headers={'Origin': host_origin}
        ) as host:
            assert (await host.post('/api/games/werewolf/create', json=dict(players=6, address='192.168.50.20'))).status_code == 200
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=phone_app(service, service.origin)), base_url=service.origin
            ) as phone:
                yield service, clock, host, phone
    finally:
        await service.close()


def proof(service, event, **kwargs):
    return dict(session=service.session, revision=service.game.revision, event_id='synthetic-'+event, **kwargs)


def bearer(token):
    return {'Authorization': 'Bearer '+token}


async def join_all(service, phone):
    phones = {}
    for seat in range(1, 7):
        joined = await phone.post('/api/join', json=attempt(service, seat=seat, marker=str(seat)))
        assert joined.status_code == 200
        phones[seat] = bearer(joined.json()['token'])
        assert (await phone.post('/api/ready', json={'ready': True}, headers=phones[seat])).status_code == 200
    return phones


async def recovery(host, service, event, seat=1):
    response = await host.post('/api/games/werewolf/seat-recovery', json=proof(service, event, seat=seat))
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    result = response.json()
    url = urlsplit(result['url'])
    assert result['seat'] == seat and url.path == '/phone/recover' and not url.query
    assert result['seconds_left'] == SEAT_RECOVERY_SECONDS and result['qr']
    assert parse_qs(url.fragment)['seat'] == [str(seat)]
    return dict(seat=seat, ticket=parse_qs(url.fragment)['ticket'][0], attempt_id='a'*64)


@pytest.mark.asyncio
async def test_recover_one_seat_and_continue_the_same_night(tmp_path):
    async with room(tmp_path) as (service, clock, host, phone):
        phones = await join_all(service, phone)
        control = '/api/games/werewolf/control'
        dealt = await host.post(control, json=proof(service, 'deal', action='start'))
        revision = dealt.json()['game']['revision']
        for seat in range(2, 7):
            assert (await phone.post('/api/identity', json={'revision': revision}, headers=phones[seat])).status_code == 200
        assert (await host.post(control, json=proof(service, 'waiting', action='advance'))).json()['detail'] == 'identities_pending'
        television = bearer(service.public_token)
        before = (await phone.get('/api/public', headers=television)).json()
        claim = await recovery(host, service, 'first-seat')
        assert (await phone.get('/api/player', headers=phones[1])).status_code == 200
        assert (await phone.post('/api/games/werewolf/seat-recovery', json=proof(service, 'from-tv', seat=1),
                                 headers=television)).status_code == 404
        assert (await phone.post('/api/recover-seat', json={**claim, 'seat': 2})).status_code == 409
        resumed = await phone.post('/api/recover-seat', json=claim)
        assert resumed.status_code == 200
        assert resumed.headers['Cache-Control'] == 'no-store'
        player = resumed.json()['player']
        assert player['seat'] == 1 and player['role'] == 'wolf' and not player['identity_confirmed']
        assert (await phone.get('/api/player', headers=phones[1])).status_code == 403
        assert (await phone.get('/api/player', headers=television)).status_code == 403
        assert (await phone.get('/api/public', headers=television)).json() == before
        new_phone = bearer(resumed.json()['token'])
        assert (await phone.post('/api/identity', json={'revision': revision}, headers=new_phone)).status_code == 200
        night = await host.post(control, json=proof(service, 'start-night', action='advance'))
        assert night.json()['game']['phase'] == 'wolves'
        choice = dict(action='attack', target=3, revision=service.game.revision,
                      event_id='synthetic-choice', expected_choice_version=0)
        selected = await phone.post('/api/choice', json=choice, headers=new_phone)
        assert selected.status_code == 200 and selected.json()['submitted'] == ['attack', 3]
        snapshot, deadline = service.game.public(), service.game.deadline
        second = await recovery(host, service, 'same-night')
        accepted = await phone.post('/api/recover-seat', json=second)
        assert accepted.status_code == 200 and accepted.json()['player']['choice_version'] == 1
        assert accepted.json()['player']['submitted'] == ['attack', 3]
        assert accepted.json()['player']['public'] == snapshot and service.game.deadline == deadline
        restored = bearer(accepted.json()['token'])
        status = await phone.get('/api/player', headers={**restored, 'X-Friday-Choice': choice['event_id']})
        assert status.json()['choice_receipt']['recorded']
        assert (await phone.post('/api/choice', json=choice, headers=new_phone)).status_code == 403
        assert (await phone.get('/api/player', headers=phones[2])).status_code == 200
        for view in [(await host.get('/api/games/werewolf/host')).text, (await phone.get('/api/public', headers=television)).text]:
            assert claim['ticket'] not in view and second['ticket'] not in view and accepted.json()['token'] not in view


@pytest.mark.asyncio
async def test_only_one_phone_can_claim_and_its_retry_reads_back_the_same_seat(tmp_path):
    async with room(tmp_path) as (service, clock, host, phone):
        phones = await join_all(service, phone)
        wrong_origin = await host.post('/api/games/werewolf/seat-recovery',
                                       json=proof(service, 'wrong-origin', seat=1), headers={'Origin': 'http://outside.invalid'})
        assert wrong_origin.status_code == 403
        claim = await recovery(host, service, 'competing-phones')
        retry_of_host_request = await recovery(host, service, 'competing-phones')
        assert retry_of_host_request == claim
        claims = [claim, {**claim, 'attempt_id': 'b'*64}]
        results = await asyncio.gather(*(phone.post('/api/recover-seat', json=payload) for payload in claims))
        assert sorted(response.status_code for response in results) == [200, 409]
        winner = next(i for i, response in enumerate(results) if response.status_code == 200)
        reply = await phone.post('/api/recover-seat', json=claims[winner])
        assert reply.json()['token'] == results[winner].json()['token']
        assert (await phone.get('/api/player', headers=phones[1])).status_code == 403
        assert len(service.credentials) == 6
        clock.now += SEAT_RECOVERY_SECONDS
        assert (await phone.post('/api/recover-seat', json=claims[winner])).status_code == 409
        assert (await phone.get('/api/player', headers=bearer(reply.json()['token']))).status_code == 200


@pytest.mark.asyncio
async def test_unused_tickets_expire_and_retire_with_the_original_seat_or_deal(tmp_path):
    async with room(tmp_path) as (service, clock, host, phone):
        phones = await join_all(service, phone)
        path = '/api/recover-seat'
        first = await recovery(host, service, 'older-link')
        clock.now += SEAT_RECOVERY_SECONDS
        assert (await phone.post(path, json=first)).status_code == 409
        assert (await phone.get('/api/player', headers=phones[1])).status_code == 200
        replaced = await recovery(host, service, 'replaced-link')
        removed = await recovery(host, service, 'removed-seat')
        assert (await phone.post(path, json=replaced)).status_code == 409
        await service.control('remove', seat=1)
        joined = await phone.post('/api/join', json=attempt(service, marker='c'))
        assert joined.status_code == 200 and (await phone.post(path, json=removed)).status_code == 409
        await phone.post('/api/ready', json={'ready': True}, headers=bearer(joined.json()['token']))
        await service.control('start')
        first_deal = await recovery(host, service, 'first-deal')
        await service.control('reseat')
        assert (await phone.post(path, json=first_deal)).status_code == 409
        await service.control('end')
        finished = await recovery(host, service, 'finished-link')
        await service.control('rematch')
        assert (await phone.post(path, json=finished)).status_code == 409
        old_room = await recovery(host, service, 'closing-room')
        await service.close_game()
        await service.create(6, address='192.168.50.20')
        assert (await phone.post(path, json=old_room)).status_code == 409
