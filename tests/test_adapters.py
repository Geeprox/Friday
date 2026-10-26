import httpx
import pytest

from friday.adapters import HomeAssistantAdapter, MilocoAdapter
from friday.config import Device, Settings


async def test_ha_brightness_and_service_contract(monkeypatch):
    monkeypatch.setenv("FRIDAY_HA_TOKEN", "test-token")
    calls = []

    def handle(r):
        calls.append(r)
        assert r.headers["authorization"] == "Bearer test-token"
        if r.method == "GET":
            return httpx.Response(200, json={"state": "on", "attributes": {"brightness": 128}})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        a = HomeAssistantAdapter(Settings(), client)
        d = Device(id="lamp", name="灯", room="客厅", entity_id="light.lamp")
        assert (await a.read(d))["brightness"] == 50
        await a.write(d, "brightness", 30)
        assert calls[-1].url.path == "/api/services/light/turn_on"
        assert b'"brightness_pct":30' in calls[-1].content


async def test_ha_unavailable_not_off(monkeypatch):
    monkeypatch.setenv("FRIDAY_HA_TOKEN", "x")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"state": "unavailable"}))
    ) as client:
        a = HomeAssistantAdapter(Settings(), client)
        with pytest.raises(ValueError):
            await a.read(Device(id="a", name="a", room="a", entity_id="light.a"))


async def test_miloco_contract_and_failure(monkeypatch):
    monkeypatch.setenv("FRIDAY_MILOCO_TOKEN", "x")

    def handle(r):
        if r.method == "GET":
            assert r.url.params["iid"] == "prop.2.1,prop.2.2"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "properties": [
                            {"iid": "prop.2.1", "value": True, "code": 0},
                            {"iid": "prop.2.2", "value": 70, "code": 0},
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"results": [{"code": -1}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        a = MilocoAdapter(Settings(), client)
        d = Device(
            id="lamp",
            name="灯",
            room="客厅",
            did="123",
            power_siid=2,
            power_piid=1,
            brightness_siid=2,
            brightness_piid=2,
        )
        assert await a.read(d) == {"on": True, "brightness": 70}
        with pytest.raises(ValueError):
            await a.write(d, "on", False)


def test_miloco_property_errors_are_not_values():
    assert MilocoAdapter.values({"properties": [{"iid": "prop.2.1", "value": False, "code": -1}]}) == {}
