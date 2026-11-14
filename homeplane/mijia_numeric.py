"""已登记米家设备的数值属性绑定、校验和尺度转换。"""

import math
import re
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Capability, Contract, Entity, PhysicalDevice
from .devices import xiaomi_identifier
from .mijia_control import registry_prefix
from .mijia_lan_client import FAMILY

INTEGER_FORMATS = {'uint8', 'uint16', 'uint32', 'uint64', 'int8', 'int16', 'int32', 'int64', 'int'}
SETTINGS = {
    'brightness': ('light.set', 'light', 'brightness', {'percentage', None}, 0, 100),
    'color_temp_kelvin': ('light.color_temperature', 'light', 'color-temperature', {'kelvin'}, 1000, 15000),
    'temperature': ('climate.temperature', 'climate', 'target-temperature', {'celsius'}, 0, 65),
}
OBSERVATION_PREFIX = 'mijia-lan.'
# 米家规格的属性类型、标准单位和 HA 原登记的设备类别必须同时匹配。
SENSORS = {
    'temperature': ('temperature', 'celsius', '°C', -100, 150, 'temperature'),
    'humidity': ('relative-humidity', 'percentage', '%', 0, 100, 'humidity'),
    'battery': ('battery-level', 'percentage', '%', 0, 100, 'sensor.state'),
    'illuminance': ('illumination', 'lux', 'lx', 0, 1000000, 'sensor.state'),
}


def finite_numeric(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


class NumericRange(Contract):
    minimum: float
    maximum: float
    step: float | None = Field(default=None, gt=0)
    integer: bool

    @model_validator(mode='after')
    def validate_scale(self):
        if (self.minimum >= self.maximum or self.step is not None and not .000001 <= self.step <= self.maximum - self.minimum
                or self.integer and any(
            value is not None and not value.is_integer() for value in (self.minimum, self.maximum, self.step)
        )):
            raise ValueError('mijia_invalid_range')
        return self

    def accepts(self, value, *, exact_step):
        if (not finite_numeric(value) or not self.minimum <= value <= self.maximum
                or self.integer and type(value) is not int):
            return False
        if exact_step and self.step is not None:
            ticks = (value - self.minimum) / self.step
            return math.isclose(ticks, round(ticks), abs_tol=1e-7, rel_tol=0)
        return True


class Setting(Contract):
    key: Literal['brightness', 'color_temp_kelvin', 'temperature']
    piid: int = Field(strict=True, ge=1, le=65535)
    scale: NumericRange

    @property
    def operation(self):
        return SETTINGS[self.key][0]

    @model_validator(mode='after')
    def validate_setting(self):
        _, _, _, _, low, high = SETTINGS[self.key]
        scale = self.scale
        if (scale.step is None or not low <= scale.minimum < scale.maximum <= high
                or self.key != 'temperature' and not scale.integer):
            raise ValueError('mijia_invalid_setting')
        return self

    def capability(self):
        if self.key == 'brightness':
            return Capability(name=self.operation, access='write', support='supported', minimum=1, maximum=255,
                              step=1, unit='ha_brightness')
        units = {'color_temp_kelvin': 'K', 'temperature': '°C'}
        return Capability(name=self.operation, access='write', support='supported', minimum=self.scale.minimum,
                          maximum=self.scale.maximum, step=self.scale.step, unit=units[self.key])

    def wire_value(self, value):
        scale = self.scale
        if self.key == 'brightness':
            if type(value) is not int or not 1 <= value <= 255:
                raise ValueError('mijia_invalid_setting')
            ticks = round((value - 1) / 254 * (scale.maximum - scale.minimum) / scale.step)
            value = scale.minimum + ticks * scale.step
        if scale.integer and finite_numeric(value) and scale.minimum <= value <= scale.maximum and int(value) == value:
            value = int(value)
        if not scale.accepts(value, exact_step=True):
            raise ValueError('mijia_invalid_setting')
        return value

    def view_value(self, value):
        if not self.scale.accepts(value, exact_step=True):
            raise ValueError('mijia_invalid_readback')
        if self.key != 'brightness':
            return value
        return max(1, min(255, round(
            1 + 254 * (value - self.scale.minimum) / (self.scale.maximum - self.scale.minimum)
        )))


class Sensor(Contract):
    entity: Entity
    device: PhysicalDevice
    identity: str = Field(pattern=r'^[a-z]{2}_(?:[0-9]{1,32}|blt\.[0-9]\.[a-z0-9]{1,64})$')
    model: str
    urn: str
    registry_uid: str
    siid: int = Field(strict=True, ge=1, le=65535)
    piid: int = Field(strict=True, ge=1, le=65535)
    device_class: Literal['temperature', 'humidity', 'battery', 'illuminance']
    scale: NumericRange

    @model_validator(mode='after')
    def validate_binding(self):
        if (self.entity.kind != 'sensor' or not self.entity.id.startswith('ha:') or ':provisional:' in self.entity.id
                or self.entity.simulated or self.device.simulated
                or self.device.category != 'device' or not any(
                    b.source == 'xiaomi-identity' and b.external_id == self.identity for b in self.device.bindings
                )):
            raise ValueError('mijia_invalid_sensor')
        return self

    @property
    def property_key(self):
        return self.identity, self.siid, self.piid

    @property
    def reference(self):
        return (self.entity.id, self.device.id, self.property_key, self.model, self.urn, self.registry_uid,
                self.device_class, self.scale.model_dump_json())

    @property
    def capability(self):
        _, _, unit, _, _, name = SENSORS[self.device_class]
        return Capability(name=name, access='read', support='supported', unit=unit)


class DeviceSettings(Contract):
    binding: str = Field(pattern=FAMILY.pattern + '$')
    settings: list[Setting] = Field(max_length=len(SETTINGS))

    @model_validator(mode='after')
    def unique_properties(self):
        if (len({setting.key for setting in self.settings}) != len(self.settings)
                or len({setting.piid for setting in self.settings}) != len(self.settings)):
            raise ValueError('mijia_invalid_setting')
        return self


class NumericCatalog(Contract):
    family: str = Field(pattern=FAMILY.pattern + '$')
    legacy_approval: str = Field(pattern=FAMILY.pattern + '$')
    devices: dict[str, DeviceSettings] = Field(max_length=512)
    sensors: list[Sensor] = Field(max_length=512)

    @model_validator(mode='after')
    def unique_sensors(self):
        if (len({sensor.entity.id for sensor in self.sensors}) != len(self.sensors)
                or len({sensor.property_key for sensor in self.sensors}) != len(self.sensors)):
            raise ValueError('mijia_invalid_sensor')
        return self


def numeric_scale(prop, units, bounds, *, setting, integer_only=False):
    fmt = prop.get('format')
    integer = fmt in INTEGER_FORMATS
    native_unit = prop.get('unit')
    if (native_unit not in units or not (integer or fmt == 'float') or (integer_only and not integer)
            or 'read' not in prop['access'] or setting and 'write' not in prop['access']
            or prop.get('value-list') is not None):
        return None
    raw = prop.get('value-range')
    if raw is None and not setting:
        return NumericRange(minimum=bounds[0], maximum=bounds[1], integer=integer)
    if (not isinstance(raw, list) or len(raw) != 3
            or not all(finite_numeric(x) and abs(x) <= 1000000 for x in raw)):
        return None
    low, high, step = raw
    if not bounds[0] <= low < high <= bounds[1] or not .000001 <= step <= high - low:
        return None
    if not math.isclose((high - low) / step, round((high - low) / step), abs_tol=1e-7, rel_tol=0):
        return None
    if integer and any(int(x) != x for x in raw):
        return None
    return NumericRange(minimum=low, maximum=high, step=step, integer=integer)


def setting_bindings(spec, target, capabilities):
    # 电源的绑定已验证过官方规格，其他设置只能来自同一个服务。
    service = next(row for row in spec['services'] if row['iid'] == target.siid)
    matches = []
    for key, (operation, kind, name, units, low, high) in SETTINGS.items():
        cap = next((cap for cap in capabilities if cap.name == operation), None)
        if target.entity.kind != kind or cap is None or cap.minimum is None or cap.maximum is None:
            continue
        props = [p for p in service.get('properties', []) if p['type'].split(':')[2:4] == ['property', name]]
        if len(props) != 1 or props[0]['iid'] == target.piid:
            continue
        prop = props[0]
        scale = numeric_scale(prop, units, (low, high), setting=True, integer_only=key != 'temperature')
        if scale is None:
            continue
        if key == 'temperature' and (cap.unit not in (None, '°C') or
                (cap.minimum, cap.maximum, cap.step) != (scale.minimum, scale.maximum, scale.step)):
            continue
        if key == 'color_temp_kelvin' and not cap.minimum <= scale.minimum < scale.maximum <= cap.maximum:
            continue
        matches.append(Setting(key=key, piid=prop['iid'], scale=scale))
    return matches


def sensor_binding(spec, source, entity, device, attrs):
    from .embodied.miot_spec import properties

    device_class = attrs.get('device_class')
    if not isinstance(device_class, str) or device_class not in SENSORS:
        return None
    name, unit, ha_unit, low, high, _ = SENSORS[device_class]
    if attrs.get('unit_of_measurement') != ha_unit:
        return None
    urn, model = source.catalog['urn'], source.catalog.get('model')
    prefix = registry_prefix(spec, 'sensor', urn, model)
    unique = source.registry['unique_id']
    match = re.fullmatch(prefix + rf'_{name.replace("-", "_")}_p_(\d+)_(\d+)', unique) if prefix else None
    if match is None or xiaomi_identifier(match[1]) != source.identity:
        return None
    properties(spec)
    siid, piid = int(match[2]), int(match[3])
    prop = next((p for s in spec['services'] if s['iid'] == siid for p in s.get('properties', [])
                 if p['iid'] == piid and p['type'].split(':')[2:4] == ['property', name]), None)
    scale = numeric_scale(prop, {unit}, (low, high), setting=False) if prop else None
    if scale is None:
        return None
    return Sensor(entity=entity.model_copy(update={'capabilities': [], 'state_version': None}),
                  device=device.model_copy(update={'capabilities': [], 'function_ids': [], 'limitations': []}),
                  identity=source.identity, model=model, urn=urn, registry_uid=unique, siid=siid,
                  piid=piid, device_class=device_class, scale=scale)
