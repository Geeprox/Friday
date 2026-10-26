"""Home boundary: OAuth, discovery and typed capabilities; no model-generated URLs."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import websockets
from pydantic import BaseModel, ConfigDict, Field


def private_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".private-", delete=False) as file:
        temporary = Path(file.name)
        json.dump(value, file, ensure_ascii=False)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Credentials:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.path = Path("data/credentials/home-assistant.json")
        self.lock = asyncio.Lock()

    def connected(self):
        if os.environ.get(self.settings.ha_token_env):
            return True
        try:
            return json.loads(self.path.read_text()).get("ha_url") == self.settings.ha_url
        except (OSError, ValueError):
            return False

    async def exchange(self, code, client_id):
        response = await self.client.post(
            self.settings.ha_url.rstrip("/") + "/auth/token",
            data={"grant_type": "authorization_code", "code": code, "client_id": client_id},
            timeout=15,
        )
        response.raise_for_status()
        value = response.json()
        if not value.get("access_token") or not value.get("refresh_token"):
            raise ValueError("OAuth token response incomplete")
        private_json(
            self.path,
            {
                **value,
                "client_id": client_id,
                "ha_url": self.settings.ha_url,
                "expires_at": time.time() + value["expires_in"],
            },
        )

    async def token(self, *, allow_refresh=True):
        if token := os.environ.get(self.settings.ha_token_env):
            return token
        async with self.lock:
            value = json.loads(self.path.read_text())
            if value["ha_url"] != self.settings.ha_url:
                raise ValueError("Credentials belong to another Home Assistant")
            if allow_refresh and value["expires_at"] < time.time() + 60:
                response = await self.client.post(
                    self.settings.ha_url.rstrip("/") + "/auth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": value["refresh_token"],
                        "client_id": value["client_id"],
                    },
                    timeout=15,
                )
                response.raise_for_status()
                value.update(response.json())
                value["expires_at"] = time.time() + value["expires_in"]
                private_json(self.path, value)
            return value["access_token"]


class Entity(BaseModel):
    id: str
    name: str
    room: str = ""
    domain: str
    state: str
    category: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, dict] = Field(default_factory=dict)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    service: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class HomeGateway(Protocol):
    entities: dict[str, Entity]

    async def discover(self) -> list[Entity]: ...
    async def read(self, entity_id: str) -> dict: ...
    async def execute(self, action: Action) -> None: ...


def capabilities(domain, attrs, services):
    """HA features + registered services form the executable contract."""
    result = {}
    supported = int(attrs.get("supported_features", 0))

    def add(service, parameters=None, confirm=False):
        if service in services.get(domain, {}):
            result[service] = {"parameters": parameters or {}, "confirm": confirm}

    percent = {"type": "number", "min": 0, "max": 100}
    if domain == "light":
        modes = attrs.get("supported_color_modes", [])
        params = {}
        if any(m not in ("onoff", "unknown") for m in modes):
            params["brightness_pct"] = percent
        if "color_temp" in modes:
            params["color_temp_kelvin"] = {
                "type": "number",
                "min": attrs.get("min_color_temp_kelvin", 2000),
                "max": attrs.get("max_color_temp_kelvin", 6500),
            }
        add("turn_on", params)
        add("turn_off")
    elif domain == "switch":
        add("turn_on", confirm=True)
        add("turn_off", confirm=True)
    elif domain == "fan":
        add("turn_on")
        add("turn_off")
        if supported & 1:
            add("set_percentage", {"percentage": {**percent, "required": True}})
        if supported & 8 and attrs.get("preset_modes"):
            add("set_preset_mode", {"preset_mode": {"enum": attrs["preset_modes"], "required": True}})
    elif domain == "climate":
        add("set_hvac_mode", {"hvac_mode": {"enum": attrs.get("hvac_modes", []), "required": True}})
        if supported & 1:
            add(
                "set_temperature",
                {
                    "temperature": {
                        "type": "number",
                        "min": attrs.get("min_temp", 16),
                        "max": attrs.get("max_temp", 30),
                        "required": True,
                    }
                },
            )
        if supported & 8:
            add("set_fan_mode", {"fan_mode": {"enum": attrs.get("fan_modes", []), "required": True}})
    elif domain == "cover":
        risky = attrs.get("device_class") not in ("curtain", "blind", "shade", "shutter")
        for name, bit in (("open_cover", 1), ("close_cover", 2), ("stop_cover", 8)):
            if supported & bit:
                add(name, confirm=risky)
        if supported & 4:
            add("set_cover_position", {"position": {**percent, "required": True}}, confirm=risky)
    elif domain == "media_player":
        for name, bit in (("turn_on", 128), ("turn_off", 256), ("media_play", 16384), ("media_pause", 1)):
            if supported & bit:
                add(name)
        if supported & 4:
            add("volume_set", {"volume_level": {"type": "number", "min": 0, "max": 1, "required": True}})
    elif domain == "vacuum":
        for name, bit in (("start", 8192), ("pause", 4), ("stop", 8), ("return_to_base", 16)):
            if supported & bit:
                add(name)
    elif domain == "humidifier":
        add("turn_on")
        add("turn_off")
        add(
            "set_humidity",
            {
                "humidity": {
                    "type": "number",
                    "min": attrs.get("min_humidity", 30),
                    "max": attrs.get("max_humidity", 80),
                    "required": True,
                }
            },
        )
    elif domain == "lock":
        add("lock", confirm=True)
        add("unlock", confirm=True)
    elif domain in ("scene", "script"):
        add("turn_on", confirm=True)
    elif domain in ("button", "input_button"):
        add("press", confirm=True)
    elif domain in ("select", "input_select"):
        add("select_option", {"option": {"enum": attrs.get("options", []), "required": True}}, confirm=True)
    elif domain in ("number", "input_number"):
        add(
            "set_value",
            {
                "value": {
                    "type": "number",
                    "min": attrs.get("min", 0),
                    "max": attrs.get("max", 100),
                    "required": True,
                }
            },
            confirm=True,
        )
    elif domain == "water_heater":
        add(
            "set_operation_mode",
            {"operation_mode": {"enum": attrs.get("operation_list", []), "required": True}},
            confirm=True,
        )
    return result


# Only these state fields enter model context, never entity_picture URLs or tokens.
ATTRIBUTES = {
    "brightness",
    "color_temp_kelvin",
    "temperature",
    "current_temperature",
    "humidity",
    "current_humidity",
    "hvac_modes",
    "hvac_action",
    "min_temp",
    "max_temp",
    "fan_modes",
    "fan_mode",
    "supported_features",
    "supported_color_modes",
    "min_color_temp_kelvin",
    "max_color_temp_kelvin",
    "percentage",
    "preset_modes",
    "preset_mode",
    "device_class",
    "current_position",
    "volume_level",
    "source",
    "options",
    "min",
    "max",
    "step",
    "operation_list",
    "operation_mode",
    "unit_of_measurement",
    "min_humidity",
    "max_humidity",
}


class HAHome:
    def __init__(self, settings, client: httpx.AsyncClient, credentials: Credentials):
        self.settings, self.client, self.credentials = settings, client, credentials
        self.entities = {}
        self.services = {}
        self.last_sync = None
        self.error = None

    async def request(self, method, path, **kwargs):
        response = await self.client.request(
            method,
            self.settings.ha_url.rstrip("/") + "/api" + path,
            headers={"Authorization": "Bearer " + await self.credentials.token()},
            timeout=15,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def registries(self):
        url = (
            self.settings.ha_url.rstrip("/").replace("http:", "ws:", 1).replace("https:", "wss:", 1)
            + "/api/websocket"
        )
        result = {}
        async with websockets.connect(url, proxy=None, open_timeout=8, max_size=8_000_000) as ws:
            await asyncio.wait_for(ws.recv(), 8)
            await ws.send(json.dumps({"type": "auth", "access_token": await self.credentials.token()}))
            if json.loads(await asyncio.wait_for(ws.recv(), 8))["type"] != "auth_ok":
                raise ValueError("HA authentication rejected")
            for index, kind in enumerate(("area", "device", "entity"), 1):
                await ws.send(json.dumps({"id": index, "type": f"config/{kind}_registry/list"}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if not reply.get("success"):
                    raise ValueError("Registry access rejected")
                result[kind] = reply["result"]
        return result

    async def discover(self):
        states, service_list = await asyncio.gather(
            self.request("GET", "/states"), self.request("GET", "/services")
        )
        registries = await self.registries()
        self.services = {row["domain"]: row["services"] for row in service_list}
        areas = {row["area_id"]: row["name"] for row in registries["area"]}
        devices = {row["id"]: row for row in registries["device"]}
        entries = {row["entity_id"]: row for row in registries["entity"]}
        entities = {}
        for state in states:
            eid = state["entity_id"]
            entry = entries.get(eid, {})
            if entry.get("disabled_by") or entry.get("hidden_by"):
                continue
            device = devices.get(entry.get("device_id"), {})
            attrs = state.get("attributes", {})
            domain = eid.split(".")[0]
            room = areas.get(entry.get("area_id") or device.get("area_id"), "")
            entities[eid] = Entity(
                id=eid,
                name=attrs.get("friendly_name", eid),
                room=room,
                domain=domain,
                state=state["state"],
                category=entry.get("entity_category"),
                attributes={k: v for k, v in attrs.items() if k in ATTRIBUTES},
                capabilities=capabilities(domain, attrs, self.services),
            )
        self.entities, self.last_sync, self.error = entities, time.time(), None
        return list(entities.values())

    async def read(self, entity_id):
        if entity_id not in self.entities:
            raise ValueError("Unknown entity")
        raw = await self.request("GET", "/states/" + quote(entity_id, safe=""))
        return {
            "state": raw["state"],
            "attributes": {k: v for k, v in raw.get("attributes", {}).items() if k in ATTRIBUTES},
        }

    async def execute(self, action):
        validate_action(action, self.entities)
        domain = self.entities[action.entity_id].domain
        await self.request(
            "POST",
            f"/services/{domain}/{action.service}",
            json={"entity_id": action.entity_id, **action.parameters},
        )


def validate_action(action, entities):
    entity = entities.get(action.entity_id)
    if not entity or action.service not in entity.capabilities:
        raise ValueError("Entity capability is not available")
    capability = entity.capabilities[action.service]
    schema = capability["parameters"]
    if set(action.parameters) - set(schema):
        raise ValueError("Unsupported action parameters")
    for name, rule in schema.items():
        if name not in action.parameters:
            if rule.get("required"):
                raise ValueError("Required action parameter missing")
            continue
        value = action.parameters[name]
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError("Value outside capability options")
        if rule.get("type") == "number":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not rule["min"] <= value <= rule["max"]
            ):
                raise ValueError("Value outside capability range")
    return capability["confirm"] or entity.category in ("config", "diagnostic")
