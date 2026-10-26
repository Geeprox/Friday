"""Simulated native Miloco contract; no vendor calls, real keys or household media."""
import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.miloco_models import CATALOG, PREFIX
from homeplane.settings import SettingsService

ORIGIN = "http://127.0.0.1:8740"
KEY = "SIMULATED-PRIVATE-model-key"


@pytest.fixture
def setup(tmp_path):
    credential = tmp_path / "miloco.json"
    credential.write_text(json.dumps({"server": {"token": "SIMULATED-service-token"}}))
    credential.chmod(0o600)
    active = {"label": "SIMULATED-original", "base_url": "http://127.0.0.1:8811/v1",
              "model": "local-vision", "has_key": False}
    state = {"active": active, "profiles": []}
    requests = []
    failure = {}

    def handler(request):
        requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 1810
        assert request.headers["Authorization"] == "Bearer SIMULATED-service-token"
        if failure.get("oversize"):
            return httpx.Response(200, content=b"x" * 65537)
        if failure.get("error"):
            return httpx.Response(500, json={"secret": KEY})
        if request.method == "PUT":
            body = json.loads(request.content)
            assert body["activate"] is False
            assert "original_label" not in body
            assert body["label"] != state["active"]["label"]
            state["profiles"].append({**body, "has_key": bool(body["api_key"]), "api_key_masked": KEY[:3] + "…" + KEY[-4:]})
            if failure.get("unknown"):
                # Apply then lose response: client must not repeat the mutation.
                raise httpx.ReadTimeout("SIMULATED lost response " + KEY)
            if failure.get("active_changed"):
                state["active"] = {**active, "model": "SIMULATED-external-change"}
        if request.url.path.endswith("/test"):
            assert json.loads(request.content).get("api_key") is None
            return httpx.Response(200, json={"code": 0, "data": {"ok": True, "code": "ok", "message": KEY}})
        return httpx.Response(200, json={"code": 0, "data": state})

    service = SettingsService(tmp_path / "settings.json", token_path=credential, transport=httpx.MockTransport(handler))
    service.update({"camera_service_url": "http://127.0.0.1:1810"})
    app = create_app(db_path=tmp_path / "db.sqlite", settings_service=service)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, state, requests, failure, credential


def save(client, preset="mimo-flash", **extra):
    return client.post("/api/settings/miloco/models/save", json={"preset": preset, **extra}, headers={"origin": ORIGIN})


def test_candidate_save_preserves_active_and_secret_not_returned(setup):
    client, state, requests, _, credential = setup
    before = state["active"].copy()
    response = save(client, api_key=KEY)
    assert response.status_code == 200 and response.json()["status"] == "saved_inactive"
    assert state["active"] == before
    assert KEY not in response.text and "api_key_masked" not in response.text
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert sum(r.method == "PUT" for r in requests) == 1
    report = client.get("/api/settings/miloco/models").json()
    item = next(p for p in report["catalog"] if p["preset"] == "mimo-flash")
    assert item["key_configured"] and item["saved"] and not item["active"]
    assert not report["activation_supported"] and report["family_agent_runtime"] == "ARL"
    assert not report["vision_verified"]
    # Blank save is idempotent, preserving the key instead of replacing it empty.
    assert save(client).json()["status"] == "already_saved"
    assert sum(r.method == "PUT" for r in requests) == 1


def test_key_change_creates_inactive_candidate_even_if_previous_is_active(setup):
    client, state, requests, _, _ = setup
    save(client, api_key=KEY)
    state["active"] = state["profiles"][0].copy()
    original = state["active"].copy()
    save(client, api_key="SIMULATED-new-key")
    assert state["active"] == original
    labels = [json.loads(r.content)["label"] for r in requests if r.method == "PUT"]
    assert len(set(labels)) == 2
    assert all(label.startswith(PREFIX) for label in labels)


def test_no_missing_key_probe_and_third_party_not_claimed_compatible(setup):
    client, _, requests, _, _ = setup
    save(client)
    path = "/api/settings/miloco/models/probe"
    assert client.post(path, json={"preset": "mimo-flash"}, headers={"origin": ORIGIN}).json()["status"] == "key_required"
    for preset in ("glm-flash", "glm-free", "deepseek-flash"):
        save(client, preset, api_key=KEY)
        assert client.post(path, json={"preset": preset}, headers={"origin": ORIGIN}).json()["status"] == "media_adapter_required"
    assert not any(r.url.path.endswith("/test") for r in requests)


def test_explicit_native_text_probe_never_activates_or_claims_vision(setup):
    client, state, requests, _, _ = setup
    save(client, api_key=KEY)
    before = state["active"].copy()
    response = client.post("/api/settings/miloco/models/probe", json={"preset": "mimo-flash"}, headers={"origin": ORIGIN})
    assert response.json() == {"status": "text_probe_pass", "verification": "TEXT_CONNECTIVITY_ONLY",
                              "vision_verified": False, "household_media_sent": False, "activated": False}
    assert KEY not in response.text and state["active"] == before
    assert [r.url.path for r in requests if r.method == "POST"] == ["/api/admin/omni-config/test"]


def test_unknown_save_no_retry_and_safe_error(setup):
    client, state, requests, failure, _ = setup
    failure["unknown"] = True
    response = save(client, api_key=KEY)
    assert response.status_code == 503 and response.json()["verification"] == "unknown_outcome"
    assert KEY not in response.text and len(state["profiles"]) == 1
    assert sum(r.method == "PUT" for r in requests) == 1
    failure.clear()
    failure["active_changed"] = True
    assert save(client, api_key=KEY).json()["status"] == "active_configuration_changed"


def test_boundary_rejects_arbitrary_endpoint_activation_and_cross_origin(setup):
    client, _, requests, _, _ = setup
    for extra in ({"base_url": "https://attacker.invalid"}, {"activate": True}, {"api_key": KEY + "\n"}):
        response = save(client, **extra)
        assert response.status_code in (400, 422) and KEY not in response.text
    assert save(client, "deepseek-v4-pro").status_code == 400
    assert client.post("/api/settings/miloco/models/save", json={"preset": "mimo-flash", "api_key": KEY}).status_code == 403
    assert not any(r.method == "PUT" for r in requests)


@pytest.mark.parametrize("failure_name", ["error", "oversize"])
def test_unavailable_or_oversized_response_sanitized(setup, failure_name):
    client, _, _, failure, _ = setup
    failure[failure_name] = True
    response = client.get("/api/settings/miloco/models")
    assert response.json()["status"] == "service_unavailable" and KEY not in response.text
    assert not any(p["key_configured"] for p in response.json()["catalog"])


def test_profile_capacity_does_not_delete_or_replace(setup):
    client, state, requests, _, _ = setup
    state["profiles"] = [{"label": "SIMULATED-preserved-" + str(i)} for i in range(20)]
    assert save(client, api_key=KEY).json()["status"] == "profile_capacity_reached"
    assert len(state["profiles"]) == 20
    assert not any(r.method in {"PUT", "DELETE"} for r in requests)
    assert "deepseek-v4-pro" not in {v["model"] for v in CATALOG.values()}


def test_shared_readable_secret_reference_does_not_send_credentials(setup):
    client, _, requests, _, credential = setup
    credential.chmod(0o644)
    report = client.get("/api/settings/miloco/models").json()
    assert report["status"] == "service_unavailable"
    assert requests == []
