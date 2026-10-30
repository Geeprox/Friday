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
    "climate.power",
    "climate.temperature",
    "climate.fan_mode",
    "light.color_temperature",
    "cover.position",
    "cover.tilt",
    "cover.state",
    "lock.set",
    "number.value",
    "select.option",
    "media_player.power",
    "media_player.volume",
    "media_player.mute",
    "media_player.playback",
    "humidifier.power",
    "humidifier.humidity",
    "humidifier.mode",
    "water_heater.power",
    "water_heater.mode",

)
BINDING = re.compile(r"^(light|switch|fan|climate|cover|lock|number|select|media_player|humidifier|water_heater)\.[a-z0-9_]+$")


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
    if domain=='switch' and getattr(adapter,'_switch_channels',{}).get(external,{}).get('role') in {'wireless','unknown'}:
        return []
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
        if domain=='light' and 'color_temp' in attrs.get('supported_color_modes',[]):
            low,high=attrs.get('min_color_temp_kelvin'),attrs.get('max_color_temp_kelvin')
            if finite(low) and finite(high) and 1000<=low<high<=40000:
                add('light.color_temperature',minimum=low,maximum=high,step=1,unit='K')
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
        if {"turn_on", "turn_off"} <= known and feature & 384 == 384:
            add("climate.power", choices=["on", "off"])
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
        modes=attrs.get('fan_modes')
        if ('set_fan_mode' in known and feature&8 and isinstance(modes,list)
                and 0<len(modes)<=32 and all(isinstance(m,str) and 1<=len(m)<=128
                    and not any(ord(c)<32 for c in m) for m in modes)):
            add('climate.fan_mode',choices=list(dict.fromkeys(modes)))
    if domain in {"humidifier", "water_heater"}:
        if {"turn_on", "turn_off"} <= known and (domain == "humidifier" or feature & 8):
            add(domain + ".power", choices=["on", "off"])
        if domain == "humidifier":
            low, high = attrs.get("min_humidity"), attrs.get("max_humidity")
            step = attrs.get("target_humidity_step")
            if "set_humidity" in known and finite(low) and finite(high) and 0 <= low < high <= 100:
                add("humidifier.humidity",minimum=low,maximum=high,step=step if finite(step) and step > 0 else 1,unit="%")
        mode_key, service, flag = ("available_modes", "set_mode", 1) if domain == "humidifier" else ("operation_list", "set_operation_mode", 2)
        modes = attrs.get(mode_key)
        if service in known and feature & flag and isinstance(modes,list) and 0<len(modes)<=32 and all(isinstance(m,str) and 1<=len(m)<=128 and not any(ord(c)<32 for c in m) for m in modes):
            add(domain + ".mode",choices=list(dict.fromkeys(modes)))
    if domain == "cover" and "set_cover_tilt_position" in known and feature & 128:
        add("cover.tilt", unit="%", minimum=0, maximum=100, step=1)
    if domain == "cover" and "set_cover_position" in known and feature & 4:
        add("cover.position", unit="%", minimum=0, maximum=100, step=1)
    if domain == "cover" and {"open_cover", "close_cover"} <= known and feature & 3 == 3:
        add("cover.state", choices=["open", "closed"]+(['stopped'] if 'stop_cover' in known and feature&8 else []))
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
    if domain=='media_player':
        if {'turn_on','turn_off'}<=known and feature&384==384:
            add('media_player.power',choices=['on','off'])
        if 'volume_set' in known and feature&4:
            add('media_player.volume',minimum=0,maximum=1,step=.01)
        if 'volume_mute' in known and feature&8:
            add('media_player.mute')
        if {'media_play','media_pause'}<=known and feature&16385==16385:
            add('media_player.playback',choices=['playing','paused'])
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
        return "cover", {'open':'open_cover','closed':'close_cover','stopped':'stop_cover'}[desired['state']], {}
    if operation in {'media_player.power', 'climate.power', 'humidifier.power', 'water_heater.power'}:
        return operation.split('.')[0],'turn_on' if desired['power']=='on' else 'turn_off',{}
    if operation=='media_player.playback':
        return 'media_player','media_play' if desired['state']=='playing' else 'media_pause',{}
    if operation=='light.color_temperature':
        return 'light','turn_on',{'color_temp_kelvin':desired['color_temp_kelvin']}
    domain, service, key = {
        "fan.percentage": ("fan", "set_percentage", "percentage"),
        "humidifier.humidity":("humidifier","set_humidity","humidity"),
        "humidifier.mode":("humidifier","set_mode","mode"),
        "water_heater.mode":("water_heater","set_operation_mode","operation_mode"),
        "climate.mode": ("climate", "set_hvac_mode", "hvac_mode"),
        "climate.temperature": ("climate", "set_temperature", "temperature"),
        "cover.position": ("cover", "set_cover_position", "position"),
        "cover.tilt": ("cover", "set_cover_tilt_position", "tilt_position"),
        "number.value": ("number", "set_value", "value"),
        "select.option": ("select", "select_option", "option"),
        "climate.fan_mode": ('climate','set_fan_mode','fan_mode'),
        "media_player.volume": ('media_player','volume_set','volume_level'),
        "media_player.mute": ('media_player','volume_mute','is_volume_muted'),
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
        "humidifier.power":"power", "humidifier.humidity":"humidity", "humidifier.mode":"mode",
        "water_heater.power":"power", "water_heater.mode":"operation_mode",
        "switch.set": "power",
        "fan.set": "power",
        "fan.percentage": "percentage",
        "climate.mode": "hvac_mode",
        "climate.power": "power",
        "climate.temperature": "temperature",
        "cover.position": "position",
        "cover.tilt": "tilt_position",
        "cover.state": "state",
        "lock.set": "lock",
        "number.value": "value",
        "select.option": "option",
        "climate.fan_mode": 'fan_mode',
        "light.color_temperature": 'color_temp_kelvin',
        "media_player.power": 'power',
        "media_player.volume": 'volume_level',
        "media_player.mute": 'is_volume_muted',
        "media_player.playback": 'state',
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
        elif self.operation in {"humidifier.power", "water_heater.power"}:
            modes=attrs.get("operation_list",[]) if self.operation=="water_heater.power" else []
            reported={"power":"off" if value=="off" else "on" if value=="on" or value in modes else None}
        elif self.operation == "humidifier.humidity":
            reported={"humidity":attrs.get("humidity")}
        elif self.operation == "humidifier.mode":
            reported={"mode":attrs.get("mode")}
        elif self.operation == "water_heater.mode":
            reported={"operation_mode":attrs.get("operation_mode")}
        elif self.operation == "climate.mode":
            reported = {"hvac_mode": value}
        elif self.operation == "climate.temperature":
            reported = {"temperature": attrs.get("temperature")}
        elif self.operation=='climate.fan_mode':
            reported={'fan_mode':attrs.get('fan_mode')}
        elif self.operation=='light.color_temperature':
            reported={'color_temp_kelvin':attrs.get('color_temp_kelvin')}
        elif self.operation=='climate.power':
            reported={'power':'off' if value=='off' else 'on' if value in {'heat','cool','heat_cool','auto','dry','fan_only'} else None}
        elif self.operation=='media_player.power':
            reported={'power':'off' if value=='off' else 'on' if value in {'on','idle','playing','paused','buffering'} else None}
        elif self.operation=='media_player.volume':
            reported={'volume_level':attrs.get('volume_level')}
        elif self.operation=='media_player.mute':
            reported={'is_volume_muted':attrs.get('is_volume_muted')}
        elif self.operation=='media_player.playback':
            reported={'state':value}
        elif self.operation == "fan.percentage":
            reported = {"percentage": attrs.get("percentage")}
        elif self.operation == "cover.tilt":
            reported = {"tilt_position": attrs.get("current_tilt_position")}
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
            type(v) not in (int, float, str, type(None), bool) or isinstance(v, float) and not math.isfinite(v)
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
        reported.get(key) == value and (type(reported.get(key)) is bool if type(value) is bool
                                       else type(reported.get(key)) is not bool)
        for key, value in desired.items()
    )
