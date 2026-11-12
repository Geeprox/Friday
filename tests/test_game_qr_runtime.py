"""Default game QR dependency and the original invitation endpoint, without LAN."""
import ast
import builtins
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from fastapi import FastAPI
import httpx
import pytest

from homeplane.werewolf_api import create_router


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


def invitation():
    owner = SimpleNamespace(session='SYNTHETIC-GAME', origin='http://example.test:18750',
                            join_code='SYNTHETIC-INVITATION')
    app = FastAPI()
    app.include_router(create_router(None, None, None, Path('.'), owner=owner))
    return owner, app


@pytest.mark.asyncio
async def test_original_invitation_qr_has_the_exact_current_phone_link(monkeypatch):
    import qrcode
    owner, app = invitation()
    make = qrcode.make
    inputs = []
    def actual(value, **kwargs):
        inputs.append(value)
        return make(value, **kwargs)
    monkeypatch.setattr(qrcode, 'make', actual)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/games/werewolf/join-qr', params={'session': owner.session})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'].startswith('image/svg+xml')
    svg = ElementTree.fromstring(response.content)
    assert svg.tag == '{http://www.w3.org/2000/svg}svg'
    assert svg.find('{http://www.w3.org/2000/svg}path') is not None
    assert inputs == [owner.origin + '/phone#code=' + owner.join_code]


@pytest.mark.asyncio
async def test_missing_qr_keeps_the_original_manual_join_fallback(monkeypatch):
    owner, app = invitation()
    refuse_qr(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/games/werewolf/join-qr', params={'session': owner.session})
    assert response.status_code == 409 and response.json()['detail'] == 'qr_unavailable'


@pytest.mark.asyncio
async def test_retired_invitation_is_rejected_before_dependency_access(monkeypatch):
    owner, app = invitation()
    previous = owner.session
    owner.session = 'SYNTHETIC-REPLACEMENT'
    refuse_qr(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/games/werewolf/join-qr', params={'session': previous})
    assert response.status_code == 404
