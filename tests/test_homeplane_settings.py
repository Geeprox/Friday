"""Simulated endpoint and filesystem fixtures only; never household credentials."""
import json
import stat
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from homeplane import ha_configured, ha_setup
from homeplane.api import create_app
from homeplane.settings import SettingsService, resolve_smb_path

ORIGIN = "http://127.0.0.1:8740"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(ha_setup, "CREDENTIAL_PATH", tmp_path / "credentials/ha.json")
    service = SettingsService(tmp_path / "settings/settings.json", token_path=tmp_path / "camera.json")
    exchanges = []
    def handler(request):
        exchanges.append(request)
        return httpx.Response(200, json={"access_token": "SIMULATED-access", "refresh_token": "SIMULATED-refresh",
                                        "expires_in": 3600, "token_type": "Bearer"})
    app = create_app(db_path=tmp_path / "traces.sqlite", settings_service=service,
                     oauth_transport=httpx.MockTransport(handler))
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client, service, app, exchanges


def test_saved_settings_private_partial_and_masked(setup):
    client, service, _, _ = setup
    response = client.put("/api/settings", json={"nas_url": "smb://simulated.local/share/Monitor/",
                                               "camera_id": "SIMULATED-private-did"}, headers={"origin": ORIGIN})
    assert response.status_code == 200
    assert "SIMULATED-private-did" not in response.text
    assert response.json()["config"]["camera_id_configured"] is True
    assert stat.S_IMODE(service.path.stat().st_mode) == 0o600
    client.put("/api/settings", json={"ha_url": "http://simulated-ha.invalid:8123"}, headers={"origin": ORIGIN})
    assert service.config.camera_id == "SIMULATED-private-did"
    assert service.config.nas_url
    assert SettingsService(service.path).config.camera_id == "SIMULATED-private-did"
    assert not SettingsService(service.path).checks


@pytest.mark.parametrize("patch", [
    {"ha_url": "http://user:SIMULATED-secret@host/"}, {"nas_url": "smb://server/share/../private"},
    {"nas_path": "relative"}, {"camera_service_url": "http://192.168.50.1:1810"},
    {"nas_path": "/tmp/fixtures", "nas_url": "smb://server/share"}, {"ha_token": "SIMULATED-secret"},
])
def test_invalid_or_secret_settings_not_echoed(setup, patch):
    client, service, _, _ = setup
    response = client.put("/api/settings", json=patch, headers={"origin": ORIGIN})
    assert response.status_code in {400, 422}
    assert "SIMULATED-secret" not in response.text
    assert not service.path.exists()


def test_settings_mutations_require_origin_and_no_crosssite_reads(setup):
    client, service, _, _ = setup
    assert client.put("/api/settings", json={}).status_code == 403
    assert client.put("/api/settings", json={}, headers={"origin": "http://evil.invalid"}).status_code == 403
    assert client.get("/api/settings", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert not service.path.exists()


def test_console_localhost_alias_redirects_before_oauth_and_never_uses_forwarded_host(setup):
    client, _, _, _ = setup
    response = client.get("/", headers={"host": "localhost:8740", "x-forwarded-host": "evil.invalid"})
    assert response.status_code == 303
    assert response.headers["location"] == ORIGIN + "/"
    assert "no-store" in response.headers["cache-control"]
    assert client.post("/api/settings/ha/connect", headers={"host": "localhost:8740",
                       "origin": "http://localhost:8740"}).status_code == 403


def test_ha_discovery_endpoint_is_readonly_and_does_not_change_saved_url(setup):
    client, service, app, _ = setup
    async def scanner():
        return [{"instance_id": "simulated-instance", "name": "Simulated",
                 "url": "http://192.168.10.40:8123", "authenticated": False}]
    app.state.discovery.scanner = scanner
    assert client.get("/api/settings/ha/discover", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    result = client.get("/api/settings/ha/discover")
    assert result.status_code == 200 and result.json()["instances"][0]["authenticated"] is False
    assert service.config.ha_url is None and not service.path.exists()


def test_chunked_body_budget_and_password_validation_never_echo(setup):
    client, _, _, _ = setup
    response = client.post("/api/settings/geek/credential", content=iter([b"x" * 9000, b"y" * 9000]),
                           headers={"origin": ORIGIN})
    assert response.status_code == 413
    response = client.post("/api/settings/geek/credential", json={"passcode": "SIMULATED-secret" * 20},
                           headers={"origin": ORIGIN})
    assert response.status_code == 422 and "SIMULATED-secret" not in response.text


def test_geek_passcode_private_and_never_returned(setup):
    client, service, _, _ = setup
    response = client.post("/api/settings/geek/credential", json={"passcode": "SIMULATED-passcode"},
                           headers={"origin": ORIGIN})
    assert response.status_code == 200 and "SIMULATED-passcode" not in response.text
    secret = service.path.parent / "geek-passcode"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert client.get("/api/settings").json()["credentials"]["geek"]["credential_available"]
    assert client.post("/api/settings/geek/probe", headers={"origin": ORIGIN}).json()["status"] == "pairing_required"


def test_primary_settings_oauth_callback_only_exception_to_crosssite(setup):
    client, service, _, calls = setup
    client.put("/api/settings", json={"ha_url": "http://simulated-ha.invalid:8123"}, headers={"origin": ORIGIN})
    begin = client.post("/api/settings/ha/connect", headers={"origin": ORIGIN})
    nonce = parse_qs(urlsplit(begin.json()["authorization_url"]).query)["state"][0]
    result = client.get("/auth/ha/callback", params={"code": "SIMULATED-code", "state": nonce},
                        headers={"sec-fetch-site": "cross-site"})
    assert result.status_code == 303
    assert client.get("/api/settings/ha/status").json()["credential_available"] is True
    assert len(calls) == 1 and calls[0].url.path == "/auth/token"
    assert service.config.ha_url


def test_failed_activation_preserves_source_and_success_is_explicit(setup, monkeypatch):
    client, _, app, _ = setup
    from homeplane.simulator import Simulator
    class Candidate(Simulator):
        closed = False
        online = False
        def health(self):
            return {"transport": "online" if self.online else "offline"}
        async def close(self):
            self.closed = True
    candidate = Candidate()
    async def build(_):
        return candidate
    monkeypatch.setattr(ha_configured, "build", build)
    original = app.state.core.adapter
    failure = client.post("/api/settings/ha/activate", headers={"origin": ORIGIN})
    assert failure.status_code == 503 and failure.json()["source_unchanged"]
    assert app.state.core.adapter is original and candidate.closed
    candidate = Candidate()
    candidate.online = True
    assert client.post("/api/settings/ha/activate", headers={"origin": ORIGIN}).status_code == 200
    assert app.state.core.adapter is candidate
    assert client.post("/api/source/simulator", headers={"origin": ORIGIN}).status_code == 200
    assert app.state.core.adapter is not candidate and candidate.closed


def test_exact_smb_mount_resolution_not_assumed_volume_name():
    mounts = "//SIMULATED-user@example.invalid/homes on /Volumes/homes-2 (smbfs, nodev, nosuid, mounted by simulated)"
    assert str(resolve_smb_path("smb://example.invalid/homes/Monitor", mounts)) == "/Volumes/homes-2/Monitor"
    assert resolve_smb_path("smb://other.local/homes/Monitor", mounts) is None


def test_probe_evidence_expires_and_settings_change_discards_late_probe(setup):
    _, service, _, _ = setup
    revision = service.revision
    service.checks["nas"] = {"status": "readable", "readable": True,
                             "checked_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()}
    assert service.get_public()["checks"]["nas"]["status"] == "check_expired"
    assert service.get_public()["checks"]["nas"]["readable"] is False
    service.update({"nas_path": "/tmp/simulated"})
    assert service._record("nas", {"status": "readable"}, revision)["status"] == "discarded_configuration_changed"
    assert not service.checks


@pytest.mark.asyncio
async def test_camera_probe_no_media_and_redacted(tmp_path):
    token = tmp_path / "camera.json"
    token.write_text(json.dumps({"server": {"token": "SIMULATED-token"}}))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"is_bound": False}})
    service = SettingsService(tmp_path / "settings.json", token_path=token, transport=httpx.MockTransport(handler))
    service.update({"camera_service_url": "http://127.0.0.1:1810", "camera_id": "SIMULATED-private"})
    result = await service.probe_camera()
    assert result["status"] == "account_login_required" and result["stream_verified"] is False
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].url.path == "/api/miot/status"
    assert "SIMULATED-token" not in json.dumps(result)
