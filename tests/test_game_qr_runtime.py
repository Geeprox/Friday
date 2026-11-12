"""游戏二维码依赖及主持、电视、手机邀请流程的隔离验证。"""
import ast
import base64
import builtins
from pathlib import Path
from xml.etree import ElementTree

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from homeplane.werewolf_api import create_router
from homeplane.werewolf_effects import HostEffects
from homeplane.werewolf_service import WerewolfService, phone_app
from tests.test_werewolf_service import NoNetwork, SilentLights


HOST = '/api/games/werewolf'
ADDRESS = '192.168.50.20'


def packaged_probe():
    source = Path('native/FridayDesktop/tests/check_packaged_import.py')
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'qr_runtime_check')
    scope = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
    return scope['qr_runtime_check']


def refuse_qr(monkeypatch):
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'qrcode' or name.startswith('qrcode.'):
            raise ImportError('synthetic_missing_qr_dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)


def test_packaged_runtime_rejects_the_missing_game_dependency(monkeypatch):
    probe = packaged_probe()
    refuse_qr(monkeypatch)
    with pytest.raises(ImportError, match='synthetic_missing_qr_dependency'):
        probe()


def test_packaged_runtime_generates_svg_with_the_actual_dependency():
    result = packaged_probe()()
    assert result['svg_generated'] and result['bytes'] > 100


@pytest_asyncio.fixture
async def invitation(tmp_path):
    owner = WerewolfService(HostEffects(SilentLights()), gateway_factory=NoNetwork, addresses=lambda: [ADDRESS])
    app = FastAPI()
    app.include_router(create_router(None, None, None, tmp_path, owner=owner))
    origin = 'http://testserver'
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                     headers={'Origin': origin}) as host:
            created = await host.post(HOST + '/create', json={'players': 6, 'address': ADDRESS})
            assert created.status_code == 200
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=phone_app(owner, owner.origin)),
                                         base_url=owner.origin) as phone:
                yield owner, host, phone
    finally:
        await owner.close()


async def control(host, action, **kwargs):
    current = (await host.get(HOST + '/host')).json()
    result = await host.post(HOST + '/control', json=dict(
        action=action, session=current['session'], revision=current['game']['revision'],
        event_id=f'synthetic-{action}-command', **kwargs,
    ))
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.asyncio
async def test_original_invitation_qr_has_the_exact_current_phone_link(invitation, monkeypatch):
    import qrcode
    owner, host, phone = invitation
    make = qrcode.make
    inputs = []
    def actual(value, **kwargs):
        inputs.append(value)
        return make(value, **kwargs)
    monkeypatch.setattr(qrcode, 'make', actual)
    response = await host.get(HOST + '/join-qr', params={'session': owner.session})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'].startswith('image/svg+xml')
    svg = ElementTree.fromstring(response.content)
    assert svg.tag == '{http://www.w3.org/2000/svg}svg'
    assert svg.find('{http://www.w3.org/2000/svg}path') is not None
    tv = await phone.get('/api/invitation', headers={'Authorization': 'Bearer ' + owner.public_token})
    assert tv.status_code == 200 and tv.headers['cache-control'] == 'no-store'
    assert base64.b64decode(tv.json()['qr_svg'], validate=True) == response.content
    assert inputs == [owner.origin + '/phone#code=' + owner.join_code] * 2


@pytest.mark.asyncio
async def test_missing_qr_keeps_the_original_manual_join_fallback(invitation, monkeypatch):
    owner, host, phone = invitation
    refuse_qr(monkeypatch)
    response = await host.get(HOST + '/join-qr', params={'session': owner.session})
    assert response.status_code == 409 and response.json()['detail'] == 'qr_unavailable'
    assert response.headers['cache-control'] == 'no-store'
    tv = await phone.get('/api/invitation', headers={'Authorization': 'Bearer ' + owner.public_token})
    assert tv.status_code == 200 and tv.json()['qr_svg'] is None
    assert (await phone.get('/phone')).status_code == 200
    joined = await phone.post('/api/join', json={
        'code': tv.json()['room_code'], 'session': tv.json()['session'], 'seat': 1, 'name': '合成玩家',
    })
    assert joined.status_code == 200 and joined.json()['player']['seat'] == 1


@pytest.mark.asyncio
async def test_retired_invitation_is_rejected_before_dependency_access(invitation, monkeypatch):
    owner, host, _ = invitation
    previous = owner.session
    await control(host, 'close')
    replaced = await host.post(HOST + '/create', json={'players': 6, 'address': ADDRESS})
    assert replaced.status_code == 200 and owner.session != previous
    refuse_qr(monkeypatch)
    response = await host.get(HOST + '/join-qr', params={'session': previous})
    assert response.status_code == 404 and response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_invitation_is_withdrawn_when_full_and_returns_when_a_seat_opens(invitation):
    owner, host, phone = invitation
    params = {'session': owner.session}
    display = {'Authorization': 'Bearer ' + owner.public_token}
    original = await host.get(HOST + '/join-qr', params=params)
    assert original.status_code == 200

    async def join_ready(seat):
        joined = await phone.post('/api/join', json={
            'code': owner.join_code, 'session': owner.session, 'seat': seat, 'name': f'合成玩家{seat}',
        })
        assert joined.status_code == 200
        token = {'Authorization': 'Bearer ' + joined.json()['token']}
        ready = await phone.post('/api/ready', headers=token, json={
            'ready': True, 'lobby_revision': joined.json()['player']['public']['lobby_revision'],
        })
        assert ready.status_code == 200

    for seat in range(1, 7):
        await join_ready(seat)
    host_full = await host.get(HOST + '/join-qr', params=params)
    tv_full = await phone.get('/api/invitation', headers=display)
    assert host_full.status_code == tv_full.status_code == 404
    assert host_full.headers['cache-control'] == tv_full.headers['cache-control'] == 'no-store'

    await control(host, 'remove', seat=6)
    reopened = await host.get(HOST + '/join-qr', params=params)
    tv = await phone.get('/api/invitation', headers=display)
    assert reopened.status_code == tv.status_code == 200
    assert reopened.content == base64.b64decode(tv.json()['qr_svg'], validate=True) == original.content

    await join_ready(6)
    await control(host, 'start')
    assert (await host.get(HOST + '/join-qr', params=params)).status_code == 404
    assert (await phone.get('/api/invitation', headers=display)).status_code == 404
