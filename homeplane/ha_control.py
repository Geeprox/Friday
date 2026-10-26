"""Fixed HA operation mappings admitted by live service/entity evidence.

No raw service/URL/body authority; no model dependency; one absolute operation,
one send claim, reported readback only. Physical effect remains unconfirmed.
"""

import math
import re
from datetime import UTC, datetime

from .contracts import Capability

from .runtime_log import observe

OPERATIONS = (
    "light.set",
    "switch.set",
    "fan.set",
    "fan.percentage",
    "climate.mode",
    "climate.temperature",
    "cover.position",
    "cover.state",
    "lock.set",
    "number.value",
    "select.option",
)
BINDING = re.compile(r"^(light|switch|fan|climate|cover|lock|number|select)\.[a-z0-9_]+$")


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def services(adapter, domain):
    if not isinstance(adapter._services, list):
        return set()
    return {
        key
        for s in adapter._services
        if isinstance(s, dict) and s.get("domain") == domain and isinstance(s.get("services"), dict)
        for key in s["services"]
    }


def bindings(entity):
    external = [b.external_id for b in entity.bindings if b.source == "ha"]
    registry = [b.external_id for b in entity.bindings if b.source == "ha-device-registry"]
    if len(external) != 1 or len(registry) != 1 or not BINDING.fullmatch(external[0]):
        return None
    if ":provisional:" in entity.id or external[0].split(".")[0] != entity.kind:
        return None
    return external[0], registry[0]


def admitted_capabilities(adapter, entity, authority):
    bound = bindings(entity)
    if bound is None:
        return []
    external, _ = bound
    domain = entity.kind
    known = services(adapter, domain)
    attrs = adapter._control_attributes.get(external, {})
    feature = attrs.get("supported_features", 0)
    feature = feature if type(feature) is int else 0
    allowed = set(authority.operations)
    caps = []

    def add(operation, **fields):
        if operation in allowed:
            caps.append(Capability(name=operation, access="write", support="supported", **fields))

    if domain in {"light", "switch", "fan"} and {"turn_on", "turn_off"} <= known:
        fields = {}
        if domain == "light":
            modes = attrs.get("supported_color_modes", [])
            if isinstance(modes, list) and any(
                m in {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
                for m in modes
                if isinstance(m, str)
            ):
                fields = {"unit": "ha_brightness", "minimum": 1, "maximum": 255, "step": 1}
        add(domain + ".set", **fields)
    if domain == "fan" and "set_percentage" in known and feature & 1:
        step = attrs.get("percentage_step")
        add(
            "fan.percentage",
            unit="%",
            minimum=0,
            maximum=100,
            step=step if finite(step) and step > 0 else None,
        )
    if domain == "climate":
        modes = attrs.get("hvac_modes", [])
        if (
            "set_hvac_mode" in known
            and isinstance(modes, list)
            and 0 < len(modes) <= 16
            and all(
                m in {"off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"}
                for m in modes
                if isinstance(m, str)
            )
            and all(isinstance(m, str) for m in modes)
        ):
            add("climate.mode", choices=list(dict.fromkeys(modes)))
        low, high, step = attrs.get("min_temp"), attrs.get("max_temp"), attrs.get("target_temp_step")
        if "set_temperature" in known and feature & 1 and finite(low) and finite(high) and low < high:
            unit = attrs.get("temperature_unit")
            add(
                "climate.temperature",
                minimum=low,
                maximum=high,
                step=step if finite(step) and step > 0 else None,
                unit=unit if isinstance(unit, str) and len(unit) <= 16 else None,
            )
    if domain == "cover" and "set_cover_position" in known and feature & 4:
        add("cover.position", unit="%", minimum=0, maximum=100, step=1)
    if domain == "cover" and {"open_cover", "close_cover"} <= known and feature & 3 == 3:
        add("cover.state", choices=["open", "closed"])
    if domain == "lock" and {"lock", "unlock"} <= known:
        add("lock.set", choices=["locked", "unlocked"])
    if domain == "number" and "set_value" in known:
        low, high, step = attrs.get("min"), attrs.get("max"), attrs.get("step")
        if finite(low) and finite(high) and low < high:
            unit = attrs.get("unit_of_measurement")
            add(
                "number.value",
                minimum=low,
                maximum=high,
                step=step if finite(step) and step > 0 else None,
                unit=unit if isinstance(unit, str) and len(unit) <= 24 else None,
            )
    if domain == "select" and "select_option" in known:
        options = attrs.get("options", [])
        if (
            isinstance(options, list)
            and 0 < len(options) <= 128
            and all(
                isinstance(o, str) and 1 <= len(o) <= 128 and not any(ord(c) < 32 for c in o) for o in options
            )
        ):
            add("select.option", choices=list(dict.fromkeys(options)))
    return caps


def command(operation, desired):
    """Only server-chosen domain/service and a validated typed parameter shape."""
    if operation in {"light.set", "switch.set", "fan.set"}:
        return (
            operation.split(".")[0],
            "turn_on" if desired["power"] == "on" else "turn_off",
            {k: v for k, v in desired.items() if k == "brightness"},
        )
    if operation == "lock.set":
        return "lock", "lock" if desired["lock"] == "locked" else "unlock", {}
    if operation == "cover.state":
        return "cover", "open_cover" if desired["state"] == "open" else "close_cover", {}
    domain, service, key = {
        "fan.percentage": ("fan", "set_percentage", "percentage"),
        "climate.mode": ("climate", "set_hvac_mode", "hvac_mode"),
        "climate.temperature": ("climate", "set_temperature", "temperature"),
        "cover.position": ("cover", "set_cover_position", "position"),
        "number.value": ("number", "set_value", "value"),
        "select.option": ("select", "select_option", "option"),
    }[operation]
    return domain, service, {key: desired[key]} if key else {}


def parameter_error(capability, operation, desired):
    if capability is None:
        return "unsupported"
    if operation == "light.set":
        if "brightness" in desired:
            if capability.minimum is None or capability.maximum is None:
                return "unsupported"
            if not capability.minimum <= desired["brightness"] <= capability.maximum:
                return "unsupported_parameters"
        return None
    key = {
        "switch.set": "power",
        "fan.set": "power",
        "fan.percentage": "percentage",
        "climate.mode": "hvac_mode",
        "climate.temperature": "temperature",
        "cover.position": "position",
        "cover.state": "state",
        "lock.set": "lock",
        "number.value": "value",
        "select.option": "option",
    }[operation]
    value = desired[key]
    if capability.choices and value not in capability.choices:
        return "unsupported_parameters"
    if finite(value):
        if (
            capability.minimum is None
            or capability.maximum is None
            or not capability.minimum <= value <= capability.maximum
        ):
            return "unsupported_parameters"
        if capability.step:
            ratio = (value - capability.minimum) / capability.step
            if not math.isclose(ratio, round(ratio), abs_tol=1e-6):
                return "unsupported_parameters"
    return None


class HADeviceWriter:
    def __init__(self, adapter, entity, operation):
        bound = bindings(entity)
        if bound is None:
            raise ValueError("invalid_actuator_binding")
        self.adapter, self.external, self.operation = adapter, bound[0], operation

    @observe('device.call', upstream='ha')
    async def send(self, desired):
        domain, service, parameters = command(self.operation, desired)
        if domain != self.external.split(".")[0]:
            raise ValueError("invalid_actuator_binding")
        async with self.adapter._client.stream(
            "POST",
            self.adapter.url + "/api/services/" + domain + "/" + service,
            json={"entity_id": self.external, **parameters},
            headers={"Authorization": "Bearer " + self.adapter._token},
            follow_redirects=False,
            timeout=4,
        ) as response:
            if response.status_code in {400, 401, 403, 404, 405, 422}:
                return "rejected_by_ha"
            return "accepted" if response.status_code == 200 else "service_outcome_unknown"

    @observe('device.readback', upstream='ha')
    async def readback(self):
        row = await self.adapter._get("/api/states/" + self.external)
        if (
            not isinstance(row, dict)
            or row.get("entity_id") != self.external
            or not isinstance(row.get("attributes", {}), dict)
        ):
            raise ValueError("invalid_readback")
        value, attrs = row.get("state"), row.get("attributes", {})
        if not isinstance(value, str) or value in {"unknown", "unavailable"}:
            raise ValueError("unavailable_readback")
        if self.operation in {"light.set", "switch.set", "fan.set"}:
            if value not in {"on", "off"}:
                raise ValueError("unavailable_readback")
            reported = {"power": value}
            if self.operation == "light.set":
                brightness = attrs.get("brightness")
                if brightness is not None and (type(brightness) is not int or not 0 <= brightness <= 255):
                    raise ValueError("invalid_readback")
                reported["brightness"] = brightness
        elif self.operation == "climate.mode":
            reported = {"hvac_mode": value}
        elif self.operation == "climate.temperature":
            reported = {"temperature": attrs.get("temperature")}
        elif self.operation == "fan.percentage":
            reported = {"percentage": attrs.get("percentage")}
        elif self.operation == "cover.position":
            reported = {"position": attrs.get("current_position")}
        elif self.operation == "cover.state":
            reported = {"state": value}
        elif self.operation == "lock.set":
            reported = {"lock": value}
        elif self.operation == "number.value":
            try:
                numeric = float(value)
            except ValueError:
                raise ValueError("invalid_readback") from None
            if not math.isfinite(numeric):
                raise ValueError("invalid_readback")
            reported = {"value": numeric}
        else:
            reported = {"option": value}
        if any(
            type(v) not in (int, float, str, type(None)) or isinstance(v, float) and not math.isfinite(v)
            for v in reported.values()
        ):
            raise ValueError("invalid_readback")
        return {
            **reported,
            "received_at": datetime.now(UTC).isoformat(),
            "evidence": "HA reported state; not independently physically confirmed",
        }


def reported_matches(desired, reported):
    return all(
        reported.get(key) == value and type(reported.get(key)) is not bool for key, value in desired.items()
    )
