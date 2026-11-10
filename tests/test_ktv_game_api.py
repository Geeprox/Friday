"""Game-library adapter contracts. All upstream requests are intercepted in-process."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from homeplane.ktv_game_api import MAX_RESPONSE, OWNER_ORIGIN, create_router

TOKEN = "SYNTHETIC_OWNER_" + "a" * 32
ORIGIN = "http://127.0.0.1:8740"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"token": TOKEN, "root": "/private/SYNTHETIC"}))
    path.chmod(0o600)
    return path


def client_for(config, handler, *, read_only=False, client_host="127.0.0.1"):
    app = FastAPI()
    app.include_router(create_router(config, transport=httpx.MockTransport(handler), read_only=read_only))
    return TestClient(app, base_url=ORIGIN, client=(client_host, 9999))


def test_fixed_loopback_owner_header_and_no_browser_credentials(config):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"active": False, "token": TOKEN, "nested": {
            "agent_key": "SYNTHETIC_PRIVATE", "path": "/private/hidden", "title": "测试曲",
            "headers": {"X-KTV-Key": TOKEN}, "message": "prefix " + TOKEN}})

    with client_for(config, upstream) as client:
        response = client.get("/api/games/ktv/manage/room", headers={"cookie": "browser=SYNTHETIC"})
    assert response.json() == {"active": False, "nested": {"title": "测试曲", "message": ""}}
    assert response.headers["cache-control"] == "no-store"
    assert str(seen[0].url) == OWNER_ORIGIN + "/v1/manage/room"
    assert seen[0].headers["X-KTV-Key"] == TOKEN
    assert "origin" not in seen[0].headers and "cookie" not in seen[0].headers


@pytest.mark.parametrize("path", ["status", "party", "jobs/anything/media/original", "manage/delete", "http://example.invalid/x", "../v1/status"])
def test_read_allowlist_never_forwards_arbitrary_paths(config, path):
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
        assert client.get("/api/games/ktv/" + path).status_code == 404
    assert not seen


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://example.invalid"}, {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}])
def test_same_origin_write_is_required_before_owner_access(config, headers):
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
        response = client.post("/api/games/ktv/manage/room/controls", json={"value": {"mic_muted": 1}}, headers=headers)
    assert response.status_code == 403
    assert not seen


@pytest.mark.parametrize("method", ["get", "post"])
def test_rebound_hostname_cannot_borrow_owner_credentials(config, method):
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
        response = client.request(method, "/api/games/ktv/manage/room" + ("/controls" if method == "post" else ""),
                                  json={} if method == "post" else None,
                                  headers={"Host": "rebound.example.invalid:8740", "Origin": "http://rebound.example.invalid:8740"})
    assert response.status_code == 403 and not seen


def test_explicit_write_passes_revision_payload_only_to_fixed_owner(config):
    seen = []
    payload = {"room_id": "SYNTHETIC-room", "value": {"ids": ["SYNTHETIC-song"], "revision": 7}}
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={"accepted": True, "revision": 8})) as client:
        response = client.post("/api/games/ktv/manage/room/queue-remove", json=payload, headers={"Origin": ORIGIN})
        assert response.json()["revision"] == 8
        assert client.post("/api/games/ktv/jobs", json={}, headers={"Origin": ORIGIN}).status_code == 404
    assert len(seen) == 1 and json.loads(seen[0].content) == payload
    assert seen[0].url.host == "127.0.0.1" and seen[0].url.port == 8769


@pytest.mark.parametrize("query", ["q=x&url=http://example.invalid", "q=x&q=y", "q=x&offset=-1", "q=x&source=other", "q=" + "a" * 121])
def test_search_query_budget_and_no_destination_override(config, query):
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
        assert client.get("/api/games/ktv/manage/search?" + query).status_code == 400
    assert not seen


def test_search_and_fixed_journal_are_readable(config):
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={"songs": []})) as client:
        assert client.get("/api/games/ktv/manage/search", params={"q": "测试", "source": "mv", "offset": 30}).status_code == 200
        assert client.get("/api/games/ktv/manage/search?source=cloud").status_code == 200
        assert client.get("/api/games/ktv/jobs/aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa/journal").status_code == 200
    assert len(seen) == 3


@pytest.mark.parametrize("kind", ["missing", "unsafe_mode", "symlink", "corrupt", "bad_token"])
def test_private_config_fails_closed_and_never_leaks_path(config, kind):
    if kind == "missing":
        config.unlink()
    elif kind == "unsafe_mode":
        config.chmod(0o644)
    elif kind == "symlink":
        target = config.with_name("target.json")
        config.rename(target)
        config.symlink_to(target)
    else:
        config.write_text("not-json" if kind == "corrupt" else '{"token":"too-short"}')
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
        response = client.get("/api/games/ktv/manage/room")
    assert response.status_code == 503 and str(config) not in response.text
    assert not seen


def test_lan_client_and_readonly_writes_cannot_gain_owner_access(config):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"active": False})
    with client_for(config, handler, client_host="192.0.2.12") as client:
        assert client.get("/api/games/ktv/manage/room").status_code == 403
    with client_for(config, handler, read_only=True) as client:
        assert client.post("/api/games/ktv/manage/preview", json={}, headers={"Origin": ORIGIN}).status_code == 403
        assert client.get("/api/games/ktv/manage/room").status_code == 200
    assert len(seen) == 1


def test_response_bounds_timeout_redirect_and_safe_error_codes(config):
    responses = iter([
        httpx.Response(409, json={"detail": "queue_changed"}),
        httpx.Response(500, json={"detail": "/private/secret " + TOKEN}),
        httpx.Response(302, headers={"Location": "https://example.invalid/"}, json={}),
        httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1)),
    ])
    with client_for(config, lambda r: next(responses)) as client:
        assert client.get("/api/games/ktv/manage/room").json() == {"detail": "queue_changed"}
        assert client.get("/api/games/ktv/manage/room").json() == {"detail": "ktv_unavailable"}
        assert client.get("/api/games/ktv/manage/room").status_code == 502
        assert client.get("/api/games/ktv/manage/room").status_code == 502
    def timeout(request):
        raise httpx.ConnectError("PRIVATE " + TOKEN, request=request)
    with client_for(config, timeout) as client:
        response = client.get("/api/games/ktv/manage/room")
        assert response.status_code == 503 and response.json() == {"detail": "ktv_offline"}


def test_party_audio_shared_policy_is_local_and_does_not_contact_owner(tmp_path):
    seen = []
    with client_for(tmp_path / "absent.json", lambda r: seen.append(r)) as client:
        response = client.get("/api/games/ktv/party-audio.js")
        assert response.status_code == 200 and "commandObserved" in response.text
    assert not seen


def test_invalid_write_bodies_do_not_contact_owner(config):
    seen = []
    with client_for(config, lambda r: seen.append(r) or httpx.Response(200, json={})) as client:
        url = "/api/games/ktv/manage/preview"
        headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
        assert client.post(url, content="[1]", headers=headers).status_code == 400
        assert client.post(url, content="broken", headers=headers).status_code == 400
        assert client.post(url, content="x" * 32769, headers=headers).status_code == 413
        assert client.post(url, content="{}", headers={"Origin": ORIGIN}).status_code == 415
        assert client.delete(url, headers=headers).status_code == 405
    assert not seen


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for KTV UI contract")
def test_game_library_ui_contracts():
    root = Path(__file__).parents[1]
    for fixture in ("games_library_fixture.cjs", "ktv_game_library_fixture.cjs"):
        result = subprocess.run(["node", str(root / "tests" / fixture)], cwd=root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_read_and_write_have_separate_openapi_operations(config):
    seen = []
    with client_for(config, lambda request: seen.append(request) or httpx.Response(200, json={})) as client:
        operations = client.get('/openapi.json').json()['paths']['/api/games/ktv/{operation}']
    assert set(operations) == {'get', 'post'}
    assert operations['get']['operationId'] != operations['post']['operationId']
    assert not seen
