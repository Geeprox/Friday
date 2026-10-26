"""Owner-local first-run contract; private synthetic state and fake endpoints only."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeplane.nas_connection import (
    NASConnectionError,
    NASConnectionManager,
    NASConnectionSettings,
    NASCredentials,
)
from tests.test_homeplane_nas_api import CFG, Endpoint, app_at

HEADERS = {"Origin": "http://testserver"}
VIEW = "/api/settings/nas/connection/config"
CONNECTION = "/api/settings/nas/connection"


def wait_state(client, expected):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        view = client.get(VIEW).json()
        if view["status"]["state"] == expected:
            return view
        time.sleep(0.005)
    raise AssertionError(view)


def test_first_run_and_saved_reference_readback_never_resolve_or_connect(tmp_path):
    calls = []

    async def resolver(ref):
        calls.append(ref)
        raise AssertionError("readback must not inspect credentials")

    def factory():
        raise AssertionError("saving disabled settings must not open an endpoint")

    manager = NASConnectionManager(tmp_path / "nas-connection.json", credential_resolver=resolver,
                                   transport_factory=factory)
    with TestClient(app_at(tmp_path, nas_manager=manager)) as client:
        response = client.get(VIEW)
        assert response.headers["cache-control"] == "no-store"
        initial = response.json()
        assert initial["config"] is None and not initial["credential_reference_saved"]
        assert initial["credential_availability"] == "not_checked"
        assert initial["status"]["state"] == "not_configured"
        assert not (tmp_path / "nas-connection.json.owner").exists()
        assert client.put(CONNECTION, json={"expected_generation": 0, "config": CFG},
                          headers=HEADERS).status_code == 200
        for _ in range(3):
            saved = client.get(VIEW).json()
            assert saved["config"]["credential_ref"] == CFG["credential_ref"]
            assert saved["credential_reference_saved"]
            assert saved["credential_availability"] == "not_checked"
            assert saved["status"]["state"] == "disabled"
            assert all(saved["status"][key] is None for key in ("reachable", "authenticated", "readable"))
        assert not calls
    with TestClient(app_at(tmp_path)) as client:
        assert client.get(VIEW).json()["config"] == saved["config"]
        assert client.get(VIEW).json()["status"]["state"] == "disabled"


@pytest.mark.parametrize("invalid", [
    {"host": "smb://SYNTHETIC-user:SYNTHETIC-secret@example.invalid"},
    {"root": "../outside"}, {"share": "a/b"}, {"password": "SYNTHETIC-secret"},
    {"transport": "sftp", "share": None, "root": "/", "host_key_sha256": None,
     "sftp_root_confined": True},
    {"transport": "sftp", "share": None, "root": "/", "host_key_sha256": "SHA256:" + "a" * 43,
     "sftp_root_confined": False},
    {"transport": "sftp", "share": None, "root": "/photos", "host_key_sha256": "SHA256:" + "a" * 43,
     "sftp_root_confined": True},
])
def test_protocol_inputs_rejected_without_echo_or_generation_change(tmp_path, invalid):
    with TestClient(app_at(tmp_path)) as client:
        result = client.put(CONNECTION, json={"expected_generation": 0, "config": {**CFG, **invalid}},
                            headers=HEADERS)
        assert result.status_code == 422 and result.json() == {"code": "invalid_request"}
        assert "SYNTHETIC-secret" not in result.text
        assert client.get(VIEW).json()["status"]["config_generation"] == 0


def test_sftp_saved_pin_and_confined_root_are_not_verified_by_readback(tmp_path):
    config = {**CFG, "transport": "sftp", "share": None, "root": "/", "port": 2222,
              "host_key_sha256": "SHA256:" + "a" * 43, "sftp_root_confined": True}
    with TestClient(app_at(tmp_path)) as client:
        assert client.put(CONNECTION, json={"expected_generation": 0, "config": config},
                          headers=HEADERS).status_code == 200
        view = client.get(VIEW).json()
        assert view["config"]["root"] == "/" and view["config"]["share"] is None
        assert view["config"]["host_key_sha256"] == config["host_key_sha256"]
        assert view["status"]["authenticated"] is None and view["status"]["reachable"] is None


def test_missing_reference_provider_pauses_before_endpoint_contact(tmp_path):
    calls = []

    async def missing(ref):
        calls.append(ref)
        return None

    class ForbiddenEndpoint(Endpoint):
        async def connect(self, config, credential):
            raise AssertionError("no endpoint contact without a resolved credential")

    manager = NASConnectionManager(tmp_path / "nas-connection.json", credential_resolver=missing,
                                   transport_factory=ForbiddenEndpoint)
    with TestClient(app_at(tmp_path, nas_manager=manager)) as client:
        client.put(CONNECTION, json={"expected_generation": 0, "config": CFG}, headers=HEADERS)
        client.post(CONNECTION + "/resume", json={"expected_generation": 1}, headers=HEADERS)
        view = wait_state(client, "paused")
        assert calls == [CFG["credential_ref"]]
        assert view["credential_reference_saved"] and view["credential_availability"] == "not_checked"
        assert view["status"]["failure"] == "nas_auth_failed"
        assert view["status"]["reachable"] is None
        assert view["status"]["authenticated"] is False
        assert view["status"]["readable"] is False


def test_backoff_cached_readback_no_replay_then_explicit_disconnect(tmp_path):
    endpoints, credentials = [], []

    async def resolve(ref):
        credentials.append(ref)
        return NASCredentials("SYNTHETIC-user", "SYNTHETIC-secret")

    class Flaky(Endpoint):
        async def connect(self, config, credential):
            if len(endpoints) == 1:
                raise NASConnectionError("nas_timeout")
            return await super().connect(config, credential)

        async def request(self, operation, payload):
            assert operation == "health", "onboarding must not enumerate or read media"
            return await super().request(operation, payload)

    def factory():
        endpoint = Flaky()
        endpoints.append(endpoint)
        return endpoint

    manager = NASConnectionManager(tmp_path / "nas-connection.json", credential_resolver=resolve,
                                   transport_factory=factory)
    config = {**CFG, "backoff_initial_seconds": 0.2, "backoff_max_seconds": 0.2}
    with TestClient(app_at(tmp_path, nas_manager=manager)) as client:
        assert client.put(CONNECTION, json={"expected_generation": 0, "config": config},
                          headers=HEADERS).status_code == 200
        assert client.post(CONNECTION + "/resume", json={"expected_generation": 1},
                           headers=HEADERS).status_code == 200
        backoff = wait_state(client, "backoff")["status"]
        assert 0 <= backoff["retry_in_seconds"] <= 0.2
        assert backoff["failure"] == "nas_timeout" and backoff["readable"] is False
        assert client.post(CONNECTION + "/resume", json={"expected_generation": 1},
                           headers=HEADERS).status_code == 409
        online = wait_state(client, "online")
        assert online["status"]["config_generation"] == 2
        assert all(online["status"][key] for key in ("reachable", "authenticated", "readable"))
        assert "SYNTHETIC-secret" not in json.dumps(online)
        assert len(endpoints) == 2 and credentials == [CFG["credential_ref"]] * 2
        disabled = client.post(CONNECTION + "/disable", json={"expected_generation": 2}, headers=HEADERS)
        assert disabled.status_code == 200 and disabled.json()["state"] == "disabled"
        assert all(endpoint.closed for endpoint in endpoints)
        assert client.get(VIEW).json()["config"]["enabled"] is False
        assert client.get(VIEW).json()["status"]["connection_epoch"] is None
    with TestClient(app_at(tmp_path)) as client:
        assert client.get(VIEW).json()["status"]["state"] == "disabled"


def test_readback_retains_localhost_and_cross_site_guards(tmp_path):
    with TestClient(app_at(tmp_path)) as client:
        assert client.get(VIEW, headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert client.get(VIEW, headers={"host": "untrusted.invalid"}).status_code == 400
        assert client.put(CONNECTION, json={"expected_generation": 0, "config": CFG}).status_code == 403
        assert client.get(VIEW, headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_settings_schema_matches_closed_runtime_contract():
    path = Path(__file__).parents[1] / "schemas/NASConnectionSettings.json"
    schema = json.loads(path.read_text())
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema == NASConnectionSettings.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["credential_availability"]["const"] == "not_checked"
