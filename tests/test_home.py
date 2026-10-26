import json
import stat

import httpx
import pytest

from friday.config import Settings
from friday.home import Credentials, HAHome


async def test_oauth_refresh_is_private_and_never_in_public_entities(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 0}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        creds = Credentials(Settings(ha_url="http://ha.local:8123"), client)
        await creds.exchange("fixture-code", "http://127.0.0.1:8730")
        assert stat.S_IMODE(creds.path.stat().st_mode) == 0o600
        assert await creds.token() == "fixture-access"
        assert len(requests) == 2
        assert b"grant_type=refresh_token" in requests[1].content
        creds.settings.ha_url = "http://another-ha.local"
        with pytest.raises(ValueError):
            await creds.token()


async def test_live_catalog_joins_real_area_and_removes_sensitive_attributes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    states = [
        {
            "entity_id": "light.living",
            "state": "on",
            "attributes": {
                "friendly_name": "客厅灯",
                "supported_color_modes": ["brightness"],
                "brightness": 128,
                "access_token": "must-not-leak",
                "entity_picture": "private-url",
            },
        },
        {"entity_id": "light.hidden", "state": "on", "attributes": {}},
        {"entity_id": "switch.indicator", "state": "off", "attributes": {"friendly_name": "指示灯"}},
    ]

    class Token:
        async def token(self):
            return "fixture"

    def respond(request):
        if request.url.path.endswith("/states"):
            return httpx.Response(200, json=states)
        if request.url.path.endswith("/services"):
            return httpx.Response(
                200,
                json=[
                    {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
                    {"domain": "switch", "services": {"turn_on": {}}},
                ],
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        home = HAHome(Settings(ha_url="http://ha.local:8123"), client, Token())

        async def registries():
            return {
                "area": [{"area_id": "living", "name": "客厅"}],
                "device": [{"id": "d1", "area_id": "living"}],
                "entity": [
                    {"entity_id": "light.living", "device_id": "d1"},
                    {"entity_id": "light.hidden", "hidden_by": "user"},
                    {"entity_id": "switch.indicator", "device_id": "d1", "entity_category": "config"},
                ],
            }

        home.registries = registries
        entities = await home.discover()
        assert len(entities) == 2
        assert home.entities["light.living"].room == "客厅"
        assert "brightness_pct" in home.entities["light.living"].capabilities["turn_on"]["parameters"]
        assert home.entities["switch.indicator"].category == "config"
        serialized = json.dumps([e.model_dump() for e in entities])
        assert "must-not-leak" not in serialized and "private-url" not in serialized
