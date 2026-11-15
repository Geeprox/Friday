"""The real desktop wrapper reaches only the fixed KTV adapter; no lifespan starts."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from homeplane.ktv_game_api import WRITES, create_router

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8740"
BACKGROUND = "a" * 64
JOB = "00000000-0000-4000-8000-000000000001"
WRITER_OPERATIONS = WRITES | {
    f"manage/backgrounds/{BACKGROUND}/regenerate", f"manage/jobs/{JOB}/retry", f"manage/jobs/{JOB}/remove",
}


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path))
    monkeypatch.setenv("FRIDAY_EMBODIED_DATA", str(tmp_path / "embodied"))
    monkeypatch.setenv("FRIDAY_SETTINGS", str(tmp_path / "settings.json"))
    candidate = os.environ.get("FRIDAY_DESKTOP_KTV_TEST_APP")
    if candidate:
        app = Path(candidate).resolve(strict=True)
        assert app.name == "Friday.app"
        backend_path = app / "Contents/Resources/backend/desktop_backend.py"
    else:
        backend_path = ROOT / "native/FridayDesktop/backend/desktop_backend.py"
    spec = importlib.util.spec_from_file_location("ktv_desktop_wrapper", backend_path)
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    if candidate:
        from homeplane import ktv_game_api

        assert Path(backend.__file__).resolve() == backend_path
        assert Path(ktv_game_api.__file__).resolve().is_relative_to(backend.PRODUCT / "app")
        assert backend.accepts_owner_operation is ktv_game_api.accepts_owner_operation

    def compose(read_only=False, *, has_credential=True, with_home=False):
        config = tmp_path / "owner.json"
        if has_credential:
            config.write_text(json.dumps({"token": "SYNTHETIC_" + "a" * 32}))
            config.chmod(0o600)
        seen = []

        def owner(request):
            seen.append(request)
            return httpx.Response(200, json={"accepted": True})

        monkeypatch.setattr(backend, "ktv_owner_router", lambda: create_router(
            config, transport=httpx.MockTransport(owner), read_only=read_only,
        ))
        wrapper = backend.DesktopService()
        if with_home:
            inner = FastAPI()
            inner.state.core = SimpleNamespace(actions=SimpleNamespace(policy_report=lambda: {}))
            inner.add_api_route("/api/actions/execute", lambda: {"unexpected_write": True}, methods=["POST"])
            wrapper.inner = inner
        return wrapper, seen
    return compose


@pytest.mark.asyncio
@pytest.mark.parametrize("with_home", [False, True])
async def test_normal_window_can_use_all_fixed_owner_operations(composition, with_home):
    wrapper, seen = composition(with_home=with_home)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for operation in sorted(WRITER_OPERATIONS):
            response = await client.post("/api/games/ktv/" + operation, json={}, headers={"Origin": ORIGIN})
            assert response.status_code == 200, operation
        for operation in ("jobs", "manage/backgrounds", f"manage/backgrounds/{BACKGROUND}/preview", f"jobs/{JOB}/journal"):
            assert (await client.get("/api/games/ktv/" + operation)).status_code == 200
        count = len(seen)
        for path in ("/api/games/ktv/jobs", "/api/games/ktv/unreviewed", "/api/games/ktv/manage/delete/extra",
                     "/api/games/ktv/manage/backgrounds/invalid/regenerate", f"/api/games/ktv/manage/jobs/{JOB}/unknown",
                     "/v1/manage/delete", "/api/actions/execute"):
            response = await client.post(path, json={}, headers={"Origin": ORIGIN})
            assert response.status_code == 409 and response.json()["code"] == "desktop_read_only", path
        assert len(seen) == count == len(WRITER_OPERATIONS) + 4
    assert all(request.url.host == "127.0.0.1" and request.url.port == 8769 for request in seen)
    assert all(request.headers["X-KTV-Key"] == "SYNTHETIC_" + "a" * 32 for request in seen)


@pytest.mark.asyncio
async def test_actual_readonly_composition_remains_non_mutating(composition):
    wrapper, seen = composition(read_only=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for operation in ("manage/delete", f"manage/backgrounds/{BACKGROUND}/regenerate", f"manage/jobs/{JOB}/retry"):
            response = await client.post("/api/games/ktv/" + operation, json={}, headers={"Origin": ORIGIN})
            assert response.status_code == 403 and response.json()["detail"] == "ktv_read_only"
    assert not seen


@pytest.mark.asyncio
async def test_remote_client_wrong_host_and_wrong_origin_are_refused(composition):
    wrapper, seen = composition()
    path = f"/api/games/ktv/manage/backgrounds/{BACKGROUND}/regenerate"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        for headers in ({}, {"Origin": "https://example.invalid"}, {"Origin": ORIGIN, "Host": "example.invalid:8740"},
                        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}):
            assert (await client.post(path, json={}, headers=headers)).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper, client=("192.0.2.11", 1234)), base_url=ORIGIN) as client:
        assert (await client.post(path, json={}, headers={"Origin": ORIGIN})).status_code == 403
    assert not seen


@pytest.mark.asyncio
async def test_local_owner_operations_still_require_worker_credentials(composition):
    wrapper, seen = composition(has_credential=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        response = await client.post("/api/games/ktv/scheduler", json={}, headers={"Origin": ORIGIN})
        assert response.status_code == 503 and response.json()["detail"] == "ktv_not_configured"
    assert not seen


@pytest.mark.asyncio
async def test_desktop_page_keeps_ha_readonly_and_marks_ktv_owner(composition, tmp_path, monkeypatch):
    from homeplane.api import create_app

    wrapper, seen = composition()
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    wrapper.inner = create_app(
        db_path=data / "trace.sqlite", settings_path=data / "settings.json",
        intelligence_directory=data / "intelligence", console_read_only=True, ktv_owner_writes=True,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=ORIGIN) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert 'data-console-read-only="true"' in page.text
        assert 'data-ktv-owner-writes="true"' in page.text
        operation = await client.post("/api/games/ktv/scheduler", json={}, headers={"Origin": ORIGIN})
        assert operation.status_code == 200 and len(seen) == 1
        general = await client.post("/api/actions/execute", json={}, headers={"Origin": ORIGIN})
        assert general.status_code == 409 and general.json()["code"] == "desktop_read_only"
