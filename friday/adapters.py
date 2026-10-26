import os
from urllib.parse import quote

import httpx


class DemoAdapter:
    def __init__(self, devices):
        self.states = {d.id: {"on": False, "brightness": 70} for d in devices}

    async def read(self, device):
        return dict(self.states[device.id])

    async def write(self, device, prop, value):
        self.states[device.id][prop] = value


class HomeAssistantAdapter:
    def __init__(self, settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def request(self, method, path, **kwargs):
        token = os.environ.get(self.settings.ha_token_env)
        if not token:
            raise ValueError("Home Assistant token missing")
        r = await self.client.request(
            method,
            self.settings.ha_url.rstrip("/") + "/api" + path,
            headers={"Authorization": f"Bearer {token}"},
            timeout=4,
            **kwargs,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def entity(device):
        if not device.entity_id or not device.entity_id.startswith("light."):
            raise ValueError("Only explicitly mapped light entities can be controlled")
        return device.entity_id

    async def read(self, device):
        data = await self.request("GET", "/states/" + quote(self.entity(device), safe=""))
        if data["state"] not in ("on", "off"):
            raise ValueError("Device unavailable")
        brightness = data.get("attributes", {}).get("brightness")
        return {
            "on": data["state"] == "on",
            "brightness": round(brightness / 255 * 100) if isinstance(brightness, (int, float)) else None,
        }

    async def write(self, device, prop, value):
        payload = {"entity_id": self.entity(device)}
        if prop == "brightness":
            payload["brightness_pct"] = value
        service = "turn_off" if prop == "on" and not value else "turn_on"
        await self.request("POST", "/services/light/" + service, json=payload)


class MilocoAdapter:
    def __init__(self, settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    def auth(self):
        token = os.environ.get(self.settings.miloco_token_env)
        if not token:
            raise ValueError("Miloco token missing")
        return {"Authorization": f"Bearer {token}"}

    async def request(self, method, path, **kwargs):
        r = await self.client.request(
            method,
            self.settings.miloco_url.rstrip("/") + "/api/miot" + path,
            headers=self.auth(),
            timeout=4,
            **kwargs,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise ValueError("Miloco rejected request")
        return body["data"]

    @staticmethod
    def iid(device, prop):
        siid, piid = (
            (device.power_siid, device.power_piid)
            if prop == "on"
            else (device.brightness_siid, device.brightness_piid)
        )
        if not device.did or not siid or not piid:
            raise ValueError("Explicit MIoT property mapping required")
        return f"prop.{siid}.{piid}"

    async def read(self, device):
        power = self.iid(device, "on")
        iids = [power]
        brightness = None
        if device.brightness_siid and device.brightness_piid:
            brightness = self.iid(device, "brightness")
            iids.append(brightness)
        data = await self.request(
            "GET", f"/devices/{quote(device.did, safe='')}/status", params={"iid": ",".join(iids)}
        )
        values = self.values(data)
        if not isinstance(values.get(power), bool):
            raise ValueError("Power state unavailable or not boolean")
        level = values.get(brightness)
        lo, hi = device.brightness_range
        if hi <= lo:
            raise ValueError("Invalid brightness range")
        return {
            "on": values[power],
            "brightness": round((level - lo) / (hi - lo) * 99 + 1)
            if isinstance(level, (int, float))
            else None,
        }

    @staticmethod
    def values(data):
        # Concrete upstream status schema is checked in tests and docs/integration.md.
        return {
            item["iid"]: item["value"]
            for item in data["properties"]
            if item.get("code", 0) == 0 and "value" in item
        }

    async def write(self, device, prop, value):
        iid = self.iid(device, prop)
        if prop == "brightness":
            lo, hi = device.brightness_range
            value = round(lo + (value - 1) / 99 * (hi - lo))
        data = await self.request(
            "POST",
            f"/devices/{quote(device.did, safe='')}/control",
            json={"type": "set_property", "iid": iid, "value": value},
        )
        if not data.get("results") or any(r.get("code") != 0 for r in data["results"]):
            raise ValueError("Property write failed")
