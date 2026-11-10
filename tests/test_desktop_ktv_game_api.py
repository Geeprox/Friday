"""The real desktop wrapper reaches only the fixed KTV adapter; no lifespan starts."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from homeplane.ktv_game_api import WRITES, create_router

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8740"


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path))
    monkeypatch.setenv("FRIDAY_EMBODIED_DATA", str(tmp_path / "embodied"))
    monkeypatch.setenv("FRIDAY_SETTINGS", str(tmp_path / "settings.json"))
    spec = importlib.util.spec_from_file_location("ktv_desktop_wrapper", ROOT / "native/FridayDesktop/backend/desktop_backend.py")
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    config = tmp_path / "owner.json"
    config.write_text(json.dumps({"token": "SYNTHETIC_" + "a" * 32}))
    config.chmod(0o600)

    def compose(read_only=False):
        seen = []
        def owner(request):
            seen.append(request)
            return httpx.Response(200, json={"accepted": True})
        inner = FastAPI()
        inner.include_router(create_router(config, transport=httpx.MockTransport(owner), read_only=read_only))
        inner.state.core = SimpleNamespace(actions=SimpleNamespace(policy_report=lambda: {"enabled": False, "configured": False}))
        wrapper = backend.DesktopService()
        wrapper.inner = inner
        return wrapper, seen
    return compose


@pytest.mark.asyncio
async def test_normal_window_can_use_all_fixed_owner_operations(composition):
    wrapper, seen = composition()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for operation in sorted(WRITES):
            response = await client.post("/api/games/ktv/" + operation, json={}, headers={"Origin": ORIGIN})
            assert response.status_code == 200, operation
        count = len(seen)
        for path in ("/api/games/ktv/jobs", "/api/games/ktv/unreviewed", "/api/games/ktv/manage/delete/extra", "/v1/manage/delete"):
            assert (await client.post(path, json={}, headers={"Origin": ORIGIN})).status_code == 409
        assert len(seen) == count == len(WRITES)
        wrapper.inner = None
        assert (await client.post("/api/games/ktv/manage/preview", json={}, headers={"Origin": ORIGIN})).status_code == 503


@pytest.mark.asyncio
async def test_actual_readonly_composition_remains_non_mutating(composition):
    wrapper, seen = composition(read_only=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        response = await client.post("/api/games/ktv/manage/delete", json={}, headers={"Origin": ORIGIN})
        assert response.status_code == 403 and response.json()["detail"] == "ktv_read_only"
    assert not seen


@pytest.mark.asyncio
async def test_remote_client_wrong_host_and_wrong_origin_are_refused(composition):
    wrapper, seen = composition()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for headers in ({}, {"Origin": "https://example.invalid"}, {"Origin": ORIGIN, "Host": "example.invalid:8740"}):
            assert (await client.post("/api/games/ktv/manage/preview", json={}, headers=headers)).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper, client=("192.0.2.11", 1234)), base_url=ORIGIN) as client:
        assert (await client.post("/api/games/ktv/manage/preview", json={}, headers={"Origin": ORIGIN})).status_code == 403
    assert not seen
