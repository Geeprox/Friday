"""Synthetic OAuth refresh + read ingress tests; no actual HA or hardware."""
import json
import stat
import time

import httpx
import pytest

from homeplane import ha_configured


@pytest.fixture
def configured(tmp_path, monkeypatch):
    url = "http://simulated-ha.invalid"
    config_path = tmp_path / "explicit.json"
    config_path.write_text(json.dumps({"ha_url": url, "ha_token_env": "SIMULATED_HA_ENV_TOKEN"}))
    monkeypatch.delenv("SIMULATED_HA_ENV_TOKEN", raising=False)
    credential_path = tmp_path / "home-assistant.json"
    monkeypatch.setattr(ha_configured, "CREDENTIAL_PATH", credential_path)
    credential = {"ha_url": url, "access_token": "SIMULATED-OLD", "refresh_token": "SIMULATED-REFRESH",
                  "client_id": "http://127.0.0.1:8741", "expires_at": time.time() + 3600, "expires_in": 1800}
    credential_path.write_text(json.dumps(credential))
    calls = []
    fail = {"refresh": False}

    def handler(request):
        calls.append(request)
        if request.url.path == "/auth/token":
            if fail["refresh"]:
                return httpx.Response(401, text="SIMULATED-PRIVATE-ERROR")
            return httpx.Response(200, json={"access_token": "SIMULATED-NEW", "expires_in": 1800})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": "light.simulated_study", "state": "off", "attributes": {}}])
        return httpx.Response(200, json=[])

    def no_ws(*_args, **_kwargs):
        raise OSError("synthetic WS unavailable")

    return config_path, credential_path, credential, calls, fail, httpx.MockTransport(handler), no_ws


@pytest.mark.asyncio
async def test_explicit_factory_refreshes_before_snapshot_without_changing_identity(configured):
    config, path, credential, calls, _, transport, ws = configured
    adapter = await ha_configured.build(config, transport=transport, ws_connect=ws)
    first = await adapter.snapshot()
    credential["expires_at"] = 0
    path.write_text(json.dumps(credential))
    second = await adapter.snapshot()
    assert first.version == second.version
    assert first.entities[0].id == second.entities[0].id
    refresh = [r for r in calls if r.method == "POST"]
    assert len(refresh) == 1 and refresh[0].url.path == "/auth/token"
    assert calls[-2].headers["authorization"] == "Bearer SIMULATED-NEW"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert adapter.health()["authentication_available"]
    assert "SIMULATED-NEW" not in second.model_dump_json()
    await adapter.close()


@pytest.mark.asyncio
async def test_refresh_failure_retains_stale_evidence_without_using_expired_token(configured):
    config, path, credential, calls, fail, transport, ws = configured
    adapter = await ha_configured.build(config, transport=transport, ws_connect=ws)
    await adapter.snapshot()
    credential["expires_at"] = 0
    path.write_text(json.dumps(credential))
    fail["refresh"] = True
    before = len(calls)
    snapshot = await adapter.snapshot()
    assert len(calls) == before + 1 and calls[-1].url.path == "/auth/token"
    assert snapshot.states[0].value is None and snapshot.states[0].quality == "stale"
    assert snapshot.events[-1].payload["reason"] == "authentication_unavailable"
    assert not adapter.health()["authentication_available"]
    assert "SIMULATED-PRIVATE-ERROR" not in snapshot.model_dump_json()
    await adapter.close()


@pytest.mark.asyncio
async def test_missing_auth_error_contains_no_private_endpoint(configured):
    config, path, _, _, _, transport, ws = configured
    path.unlink()
    with pytest.raises(ValueError, match="^ha_authentication_unavailable$"):
        await ha_configured.build(config, transport=transport, ws_connect=ws)


@pytest.mark.asyncio
async def test_config_is_explicit_and_no_default_endpoint(tmp_path):
    config = tmp_path / "invalid.json"
    config.write_text("{}")
    with pytest.raises(ValueError, match="^explicit_ha_config_invalid$"):
        await ha_configured.build(config)
