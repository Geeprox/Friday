"""Simulated OAuth exchanges only; no live login and no device writes."""
import json
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from homeplane import ha_setup


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / "explicit.json"
    config.write_text(json.dumps({"ha_url": "http://simulated-ha.invalid:8123"}))
    credential = tmp_path / "data/credentials/home-assistant.json"
    monkeypatch.setattr(ha_setup, "CREDENTIAL_PATH", credential)
    requests = []
    clock = {"now": 100.0}

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"access_token": "SIMULATED-ACCESS", "refresh_token": "SIMULATED-REFRESH",
                                         "expires_in": 1800, "token_type": "Bearer"})

    app = ha_setup.create_app(config, transport=httpx.MockTransport(handler), monotonic=lambda: clock["now"])
    with TestClient(app, base_url=ha_setup.CLIENT_ID, follow_redirects=False) as client:
        yield client, credential, requests, clock


def start(client):
    response = client.post("/auth/ha/start", headers={"origin": ha_setup.CLIENT_ID})
    assert response.status_code == 303
    url = urlsplit(response.headers["location"])
    assert url.hostname == "simulated-ha.invalid" and url.path == "/auth/authorize"
    values = parse_qs(url.query)
    assert values["client_id"] == [ha_setup.CLIENT_ID]
    assert values["redirect_uri"] == [ha_setup.CLIENT_ID + ha_setup.CALLBACK]
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    return values["state"][0]


def test_exchange_writes_private_credential_and_never_services(setup):
    client, credential, requests, _ = setup
    nonce = start(client)
    response = client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce},
                          headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 303 and response.headers["location"] == "/complete"
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert json.loads(credential.read_text())["access_token"] == "SIMULATED-ACCESS"
    assert len(requests) == 1
    assert requests[0].method == "POST" and requests[0].url.path == "/auth/token"
    assert parse_qs(requests[0].content.decode())["client_id"] == [ha_setup.CLIENT_ID]
    assert client.get("/status").json()["credential_saved"] is True
    assert "SIMULATED-ACCESS" not in client.get("/status").text
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce}).status_code == 400
    assert len(requests) == 1


def test_nonce_expiry_is_monotonic_and_single_use(setup):
    client, credential, requests, clock = setup
    nonce = start(client)
    clock["now"] += 601
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce}).status_code == 400
    clock["now"] = 101
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce}).status_code == 400
    assert not requests and not credential.exists()


def test_nonce_requires_same_browser_cookie(setup):
    client, credential, requests, _ = setup
    nonce = start(client)
    cookie = client.cookies.get(ha_setup.COOKIE)
    client.cookies.clear()
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce}).status_code == 400
    assert not requests and not credential.exists()
    client.cookies.set(ha_setup.COOKIE, cookie)
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce}).status_code == 303


def test_restart_login_invalidates_previous_nonce(setup):
    client, _, requests, _ = setup
    old, new = start(client), start(client)
    assert old != new
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": old}).status_code == 400
    assert not requests


@pytest.mark.parametrize("headers", [{}, {"origin": "http://evil.invalid"},
                                     {"origin": ha_setup.CLIENT_ID, "host": "evil.invalid"},
                                     {"origin": ha_setup.CLIENT_ID, "sec-fetch-site": "cross-site"},
                                     {"origin": ha_setup.CLIENT_ID, "host": "127.0.0.1:9999"}])
def test_invalid_origin_host_or_cross_site_rejected(setup, headers):
    client, _, requests, _ = setup
    assert client.post("/auth/ha/start", headers=headers).status_code in {400, 403}
    assert not requests


def test_request_size_and_query_budget(setup):
    client, _, requests, _ = setup
    assert client.post("/auth/ha/start", headers={"origin": ha_setup.CLIENT_ID}, content=b"x" * 1025).status_code == 413
    assert client.get(ha_setup.CALLBACK + "?state=" + "a" * 9000).status_code == 413
    assert not requests


def test_credential_symlink_cannot_redirect_write(setup, tmp_path):
    client, credential, requests, _ = setup
    outside = tmp_path / "unrelated"
    outside.write_text("keep")
    credential.parent.mkdir(parents=True)
    credential.symlink_to(outside)
    nonce = start(client)
    response = client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": nonce})
    assert response.headers["location"] == "/failed" and not requests
    assert outside.read_text() == "keep"


def test_no_implicit_configuration(tmp_path):
    config = tmp_path / "missing-ha.json"
    config.write_text("{}")
    with pytest.raises(ValueError):
        ha_setup.create_app(config)


def test_get_start_fallback_from_same_origin_anchor(setup):
    client, _, requests, _ = setup
    assert 'href="/auth/ha/start"' in client.get("/").text
    response = client.get("/auth/ha/start", headers={"sec-fetch-site": "same-origin"})
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/auth/authorize"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert not requests  # login initiation does not exchange or save credentials


@pytest.mark.parametrize("headers", [{}, {"sec-fetch-site": "cross-site"},
                                     {"sec-fetch-site": "none"},
                                     {"origin": "http://evil.invalid", "sec-fetch-site": "same-origin"}])
def test_get_start_crosssite_or_unbound_navigation_denied(setup, headers):
    client, credential, requests, _ = setup
    response = client.get("/auth/ha/start", headers=headers)
    assert response.status_code == 403
    assert not response.headers.get("set-cookie")
    assert not requests and not credential.exists()


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    from fastapi import FastAPI

    config = tmp_path / "settings.json"
    credential = tmp_path / "credentials/home-assistant.json"
    monkeypatch.setattr(ha_setup, "CREDENTIAL_PATH", credential)
    calls = []
    clock = {"now": 100.0}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "SIMULATED-ACCESS", "refresh_token": "SIMULATED-REFRESH",
                                         "expires_in": 1800, "token_type": "Bearer"})

    app = FastAPI()
    ha_setup.install_oauth(app, config, transport=httpx.MockTransport(handler), monotonic=lambda: clock["now"])
    with TestClient(app, base_url="http://127.0.0.1:8740", follow_redirects=False) as client:
        yield client, config, credential, calls, clock


def integrated_start(client, config):
    config.write_text(json.dumps({"ha_url": "http://simulated-ha.invalid:8123"}))
    response = client.post("/api/settings/ha/connect", headers={"origin": "http://127.0.0.1:8740"})
    assert response.status_code == 200
    values = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert values["client_id"] == ["http://127.0.0.1:8740"]
    assert values["redirect_uri"] == ["http://127.0.0.1:8740/auth/ha/callback"]
    return values["state"][0]


def test_integrated_configuration_is_lazy_and_setup_needs_no_restart(integrated):
    client, config, credential, calls, _ = integrated
    assert client.get("/api/settings/ha/status").json()["configured"] is False
    assert client.post("/api/settings/ha/connect", headers={"origin": "http://127.0.0.1:8740"}).status_code == 400
    state = integrated_start(client, config)
    assert not calls and not credential.exists()
    response = client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": state},
                          headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 303 and response.headers["location"] == "/?ha=connected#settings"
    assert client.get("/api/settings/ha/status").json()["credential_available"] is True
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert len(calls) == 1 and calls[0].url.path == "/auth/token"
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": state}).status_code == 400
    assert "SIMULATED-ACCESS" not in client.get("/api/settings/ha/status").text


def test_integrated_origin_and_cookie_binding(integrated):
    client, config, _, calls, _ = integrated
    assert client.post("/api/settings/ha/connect", headers={"origin": "http://evil.invalid"}).status_code == 403
    state = integrated_start(client, config)
    client.cookies.clear()
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": state}).status_code == 400
    assert not calls


def test_integrated_monotonic_expiry(integrated):
    client, config, _, calls, clock = integrated
    state = integrated_start(client, config)
    clock["now"] += 601
    response = client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": state})
    assert response.status_code == 400
    assert '返回连接设置' in response.text and 'simulated-code' not in response.text
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert not calls


def test_pending_callback_keeps_original_endpoint_when_settings_change(integrated):
    client, config, _, calls, _ = integrated
    state = integrated_start(client, config)
    config.write_text(json.dumps({"ha_url": "http://different-simulated.invalid:8123"}))
    assert client.get(ha_setup.CALLBACK, params={"code": "simulated-code", "state": state}).status_code == 303
    assert calls[0].url.host == "simulated-ha.invalid"
    assert client.get("/api/settings/ha/status").json()["credential_available"] is False
