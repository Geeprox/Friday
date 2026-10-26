"""API proposal verification using private temporary state and a synthetic transport."""

import base64
import time

from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.nas_connection import NASConnectionManager, NASCredentials
from homeplane.settings import SettingsService

REF = "SYNTHETIC-source"
CFG = {"source_ref": REF, "transport": "smb", "host": "synthetic.invalid", "share": "SYNTHETIC-share",
       "credential_ref": "SYNTHETIC-ref", "enabled": False}


class Endpoint:
    def __init__(self):
        self.closed = False

    async def connect(self, config, credentials):
        return {"reachable": True, "authenticated": True, "readable": True}

    async def request(self, operation, payload):
        if operation == "read":
            return {"data": base64.b64encode(b"SYNTHETIC-bytes").decode()}
        return {"reachable": True, "authenticated": True, "readable": True}

    async def close(self):
        self.closed = True


def app_at(tmp_path, **kwargs):
    return create_app(db_path=tmp_path / "db.sqlite", settings_path=tmp_path / "settings.json",
                      authority_path=tmp_path / "authority.json", **kwargs)


def test_default_off_cached_status_no_credential_or_state_side_effect(tmp_path):
    calls = []

    async def resolver(reference):
        calls.append(reference)
        return None

    with TestClient(app_at(tmp_path, nas_credential_resolver=resolver)) as client:
        result = client.get("/api/settings/nas/connection").json()
        assert result["state"] == "not_configured" and result["reachable"] is None
        assert client.get("/api/snapshot").status_code == 200
    assert not calls and not (tmp_path / "nas-connection.json.owner").exists()


def test_api_explicit_config_generation_origin_enable_disable_and_teardown(tmp_path):
    calls, transports = [], []

    async def resolve(reference):
        calls.append(reference)
        return NASCredentials("SYNTHETIC-user", "SYNTHETIC-secret")

    def factory():
        endpoint = Endpoint()
        transports.append(endpoint)
        return endpoint

    manager = NASConnectionManager(tmp_path / "nas-connection.json", credential_resolver=resolve,
                                   transport_factory=factory)
    headers = {"Origin": "http://testserver"}
    with TestClient(app_at(tmp_path, nas_manager=manager)) as client:
        body = {"expected_generation": 0, "config": CFG}
        assert client.put("/api/settings/nas/connection", json=body).status_code == 403
        disabled = client.put("/api/settings/nas/connection", json=body, headers=headers)
        assert disabled.status_code == 200 and disabled.json()["state"] == "disabled" and not calls
        assert client.post("/api/settings/nas/connection/resume", json={"expected_generation": 0},
                           headers=headers).status_code == 409
        assert client.post("/api/settings/nas/connection/resume", json={"expected_generation": 1},
                           headers=headers).status_code == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = client.get("/api/settings/nas/connection").json()
            if current["state"] == "online":
                break
            time.sleep(0.005)
        assert current["reachable"] and current["authenticated"] and current["readable"]
        assert calls == ["SYNTHETIC-ref"]
        assert "SYNTHETIC-secret" not in str(current) and "synthetic.invalid" not in str(current)
        result = client.post("/api/settings/nas/connection/disable", json={"expected_generation": 2},
                             headers=headers)
        assert result.json()["state"] == "disabled" and transports[0].closed
        assert client.get("/api/snapshot").status_code == 200
    assert manager.store.fd is None
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/settings/nas/connection").json()["state"] == "disabled"


def test_corrupt_nas_state_is_visible_without_blocking_core(tmp_path):
    (tmp_path / "nas-connection.json").write_text("SYNTHETIC-corrupt")
    with TestClient(app_at(tmp_path)) as client:
        status = client.get("/api/settings/nas/connection")
        assert status.status_code == 200 and status.json()["failure"] == "nas_invalid_request"
        assert client.get("/api/snapshot").status_code == 200


def test_missing_private_resolver_pauses_without_ambient_credentials(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        body = {"expected_generation": 0, "config": {**CFG, "enabled": True}}
        assert client.put("/api/settings/nas/connection", json=body,
                          headers={"Origin": "http://testserver"}).status_code == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = client.get("/api/settings/nas/connection").json()
            if status["state"] == "paused":
                break
            time.sleep(0.005)
        assert status["state"] == "paused" and status["failure"] == "nas_auth_failed"
        assert status["reachable"] is None and not status["readable"]
    assert SettingsService(tmp_path / "settings.json").nas_connection_state_path == tmp_path / "nas-connection.json"
