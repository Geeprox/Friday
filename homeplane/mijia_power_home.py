"""保留现有设备绑定，在 HA 暂不可用时从同一米家中枢读写设备。"""

import asyncio
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable
from uuid import uuid4

import httpx
from pydantic import Field, model_validator

from .action_contracts import HomeDeviceAuthority
from .actions import _read_authority_file, cover_approval, read_device_authority
from .contracts import CanonicalState, Capability, Contract, Entity, Observation, PhysicalDevice, Snapshot
from .ha import HAReadOnlyAdapter
from .ha_control import admitted_capabilities, bindings
from .local_camera_home import LocalCameraHome
from .mijia_control import MIJIA_POWER_MODE as MODE
from .mijia_control import POWER_BY_KIND, MijiaPowerWriter, power_intent, power_property
from .mijia_lan_client import FAMILY
from .mijia_numeric import (
    OBSERVATION_PREFIX,
    DeviceSettings,
    NumericCatalog,
    Sensor,
    Setting,
    sensor_binding,
    setting_bindings,
)
from .mijia_registered_cover import (
    COVER_CATALOG,
    COVER_CATALOG_LIMIT,
    CoverCatalog,
    CoverReading,
    CoverTarget,
    CoverWriter,
    cover_binding,
    cover_digest,
    read_cover_catalog,
    reserved_covers,
)
from .quality import resolve
from .rule_compiler import digest
from .rule_contracts import (
    CLOCK_TRIGGERS,
    ClimateInstruction,
    DeviceInstruction,
    LightInstruction,
    NumericCondition,
)

CATALOG = 'mijia-power-catalog.json'
NUMERIC_CATALOG = 'mijia-numeric-catalog.json'
MAX_TARGETS = 512
MAX_CATALOG_BYTES = 512 * 1024
SAMPLE_TTL = 45


class PowerTarget(Contract):
    entity: Entity
    device: PhysicalDevice
    identity: str = Field(pattern=r'^[a-z]{2}_(?:[0-9]{1,32}|blt\.[0-9]\.[a-z0-9]{1,64})$')
    model: str
    urn: str
    registry_uid: str
    siid: int = Field(strict=True, ge=1, le=65535)
    piid: int = Field(strict=True, ge=1, le=65535)
    operation: str

    @property
    def property_key(self):
        return self.identity, self.siid, self.piid

    @property
    def reference(self):
        # 名称和所在房间可变化，设备和通道标识才参与发送校验。
        return (self.entity.id, self.device.id, self.property_key, self.model, self.urn, self.registry_uid, self.operation,
                bindings(self.entity))

    @model_validator(mode='after')
    def validate_binding(self):
        if (self.entity.simulated or self.device.simulated or self.device.category != 'device'
                or POWER_BY_KIND.get(self.entity.kind) != self.operation or not bindings(self.entity)
                or not any(b.source == 'xiaomi-identity' and b.external_id == self.identity
                           for b in self.device.bindings)):
            raise ValueError('mijia_power_catalog_binding')
        return self


class PowerCatalog(Contract):
    household: str = Field(pattern=FAMILY.pattern + '$')
    family: str = Field(pattern=FAMILY.pattern + '$')
    legacy_approval: str = Field(pattern=FAMILY.pattern + '$')
    targets: list[PowerTarget] = Field(max_length=MAX_TARGETS)

    @model_validator(mode='after')
    def unique_targets(self):
        if (len({t.entity.id for t in self.targets}) != len(self.targets)
                or len({t.property_key for t in self.targets}) != len(self.targets)):
            raise ValueError('mijia_power_catalog_binding')
        return self


def read_catalog(directory):
    body, _ = _read_authority_file(directory / CATALOG, limit=MAX_CATALOG_BYTES)
    return PowerCatalog.model_validate_json(body)


def read_numeric_catalog(directory):
    body, _ = _read_authority_file(directory / NUMERIC_CATALOG, limit=MAX_CATALOG_BYTES)
    return NumericCatalog.model_validate_json(body)


def catalog_available(directory):
    try:
        if read_catalog(directory).targets:
            return True
    except (OSError, ValueError):
        pass
    try:
        if read_numeric_catalog(directory).sensors:
            return True
    except (OSError, ValueError):
        pass
    try:
        return bool(read_cover_catalog(directory).targets)
    except (OSError, ValueError):
        return False


def target_digest(target):
    return hashlib.sha256(repr(target.reference).encode()).hexdigest()


def authorize_power_read(directory, family, region, did, siid, piid):
    authority, _ = read_device_authority(directory / 'action-authority.json', include_inactive=True)
    return isinstance(authority, HomeDeviceAuthority) and power_binding_allowed(
        directory, authority, family, (f'{region}_{did}', siid, piid))


def authorize_send(directory, revision, deadline, family, region, did, siid, piid):
    remaining = deadline - time.monotonic() if type(deadline) in (int, float) else -1
    if not math.isfinite(remaining) or not 0 < remaining <= 60:
        return False
    authority, version = read_device_authority(directory / 'action-authority.json', include_generation=True)
    if not isinstance(authority, HomeDeviceAuthority) or version != revision:
        return False
    return power_binding_allowed(directory, authority, family, (f'{region}_{did}', siid, piid))


def power_binding_allowed(directory, authority, family, property_key):
    try:
        catalog = read_catalog(directory)
    except (OSError, ValueError):
        return False
    if catalog.family != family or catalog.legacy_approval != cover_approval(authority):
        return False
    targets = [target for target in catalog.targets if target.property_key == property_key]
    if len(targets) != 1 or targets[0].operation not in authority.operations:
        return False
    scope = authority.external_entity_ids
    return scope is None or any(b.source == 'ha' and b.external_id in scope for b in targets[0].entity.bindings)


def authorize_numeric(directory, family, region, did, siid, piid, request):
    """在工作进程发令前重新对照本机保存的范围和精确属性。"""
    try:
        numeric = read_numeric_catalog(directory)
        write = 'value' in request
        authority, version = read_device_authority(directory / 'action-authority.json',
                                                   include_inactive=not write, include_generation=True)
        if (not isinstance(authority, HomeDeviceAuthority) or numeric.family != family
                or numeric.legacy_approval != cover_approval(authority)):
            raise ValueError('mijia_permission_changed')
        if write:
            deadline = request.get('deadline')
            remaining = deadline - time.monotonic() if type(deadline) in (int, float) else -1
            if (not math.isfinite(remaining) or not 0 < remaining <= 60
                    or request.get('authority_revision') != version):
                raise ValueError('mijia_permission_changed')
        identity = f'{region}_{did}'
        if not write and any(s.property_key == (identity, siid, piid) for s in numeric.sensors):
            return None
        power = read_catalog(directory)
        if (power.family, power.legacy_approval) != (numeric.family, numeric.legacy_approval):
            raise ValueError('mijia_permission_changed')
        matches = []
        for target in power.targets:
            bound = numeric.devices.get(target.entity.id)
            if (target.identity != identity or target.siid != siid or target.piid == piid
                    or bound is None or bound.binding != target_digest(target)):
                continue
            if authority.external_entity_ids is not None and bindings(target.entity)[0] not in authority.external_entity_ids:
                continue
            for setting in bound.settings:
                if (setting.piid == piid and setting.operation in authority.operations
                        and (not write or setting.scale.accepts(request['value'], exact_step=True))):
                    matches.append((target, setting))
        if len(matches) != 1:
            raise ValueError('mijia_permission_changed')
        target, _ = matches[0]
        return target.piid if write and target.entity.kind == 'light' else None
    except (OSError, ValueError) as error:
        raise ValueError('mijia_permission_changed') from error


def ha_available(adapter):
    health = adapter.health()
    return (health.get('transport') == 'online' and health.get('authentication_available') is True
            and health.get('registry_available') is True and not health.get('observation_gap'))


@dataclass(frozen=True)
class Sample:
    value: str | int | float | CoverReading
    received_at: datetime
    expires: float
    version: str
    comparison: str | int | float | CoverReading


@dataclass(frozen=True)
class SettingWriter:
    setting: Setting
    binding: dict
    family: str
    request: Callable[[dict], Awaitable[dict]]
    current: Callable[[], bool]
    record: Callable[[int | float, int | float], None]
    channel: str = 'lan'

    def fingerprint(self):
        return hashlib.sha256(repr((self.family, self.binding, self.setting.model_dump_json())).encode()).hexdigest()

    async def send(self, desired):
        value = self.setting.wire_value(desired[self.setting.key])
        result = await self.request({**self.binding, 'value': value})
        return 'accepted' if result.get('accepted') is True else 'service_outcome_unknown'

    async def readback(self):
        result = await self.request(self.binding)
        raw = result.get('value')
        value = self.setting.view_value(raw)
        self.record(value, raw)
        return {self.setting.key: value, 'wire_value': raw, 'received_at': datetime.now(UTC).isoformat(),
                'evidence': 'mijia_lan_readback'}

    def matches(self, desired, reported):
        try:
            return reported.get('wire_value') == self.setting.wire_value(desired[self.setting.key])
        except ValueError:
            return False


class MijiaPowerHome:
    def __init__(self, core, power):
        self.core, self.power = core, power
        self.path = core.actions.authority_path.with_name(CATALOG)
        self.catalog = None
        try:
            self.catalog = read_catalog(self.path.parent)
        except (OSError, ValueError):
            pass
        try:
            self.numeric = read_numeric_catalog(self.path.parent)
        except (OSError, ValueError):
            self.numeric = None
        try:
            self.covers = read_cover_catalog(self.path.parent)
        except (OSError, ValueError):
            self.covers = None
        self.samples = {}
        self.last_poll = {}
        self.last_hint_check = 0.0
        self.hints = set()
        self.watch_context = None

    def discovery_context(self):
        from .embodied.mijia_lan import home_input
        from .embodied.mijia_power import grant_binding
        from .embodied.ptz_control import read_lease

        family = self.power.current_family()
        report = self.power.service.discovery_report
        household, grant = report.get('household'), report.get('grant')
        if not family or not all(isinstance(v, str) and FAMILY.fullmatch(v) for v in (household, grant)):
            return None
        directory = self.path.parent
        try:
            configuration, _, inputs = home_input(directory)
            if (inputs != report.get('inputs')
                    or grant_binding(read_lease(directory / 'miot-session.json')) != grant):
                return None
        except (OSError, TypeError, ValueError, KeyError):
            return None
        models = report.get('models')
        return (family, household, grant, models, configuration) if isinstance(models, dict) else None

    def context(self):
        discovery = self.discovery_context()
        return discovery[:4] if discovery else None

    def migrate(self):
        from .embodied.ptz_control import atomic

        adapter = self.core.adapter
        authority, _ = self.core.actions.authority(include_inactive=True)
        if (not isinstance(adapter, HAReadOnlyAdapter) or not isinstance(authority, HomeDeviceAuthority)
                or not ha_available(adapter) or adapter._last is None
                or not adapter._services
                or authority.ha_instance != adapter.instance_id or authority.ha_url.rstrip('/') != adapter.url):
            return
        context = self.discovery_context()
        if context is None:
            return
        family, household, _, _, configuration = context
        approval = cover_approval(authority)
        previous = self.catalog
        known = ({(t.identity, t.urn, t.model, t.registry_uid, t.entity.kind): t.property_key for t in previous.targets}
                 if previous and (previous.family, previous.household, previous.legacy_approval)
                 == (family, household, approval) else {})
        previous_numeric = self.numeric
        if previous_numeric and (previous_numeric.family, previous_numeric.legacy_approval) != (family, approval):
            previous_numeric = None
        known_sensors = {(s.identity, s.urn, s.model, s.registry_uid, s.device_class): s
                         for s in previous_numeric.sensors} if previous_numeric else {}
        snapshot = adapter._last
        devices = {fid: device for device in snapshot.devices if device.category == 'device'
                   for fid in device.function_ids}
        # 能切换工作模式的开关通道须先支持独立模式读取，才可以从 HA 迁移。
        mode_devices = {adapter._registry.get(e, {}).get('device_id') for e in adapter._switch_channels}
        candidates = []
        duplicates = Counter()
        sensor_candidates = []
        cover_candidates = []
        cover_claims = Counter()
        previous_covers = self.covers
        known_covers = ({t.entity.id: t for t in previous_covers.targets}
                        if previous_covers and (previous_covers.family, previous_covers.household,
                                                previous_covers.legacy_approval) == (family, household, approval)
                        else {})
        reserved = reserved_covers(configuration)
        numeric_devices = {}
        for entity in snapshot.entities:
            device = devices.get(entity.id)
            operation = POWER_BY_KIND.get(entity.kind)
            if (device is None or entity.simulated or device.simulated
                    or (entity.kind != 'sensor' and (not bindings(entity)
                                                    or operation is None and entity.kind != 'cover'))):
                continue
            source = self.power.registry_source(adapter, entity, device)
            if source is None:
                continue
            urn, model = source.catalog['urn'], source.catalog.get('model')
            unique = source.registry['unique_id']
            spec = self.power.specs.get(urn)
            if source.registry.get('disabled_by'):
                continue
            if entity.kind == 'sensor':
                if source.registry.get('hidden_by'):
                    continue
                attrs = adapter._control_attributes.get(source.external, {})
                try:
                    if spec is not None:
                        sensor = sensor_binding(spec, source, entity, device, attrs)
                    else:
                        sensor = known_sensors.get((source.identity, urn, model, unique, attrs.get('device_class')))
                        if sensor is not None:
                            if sensor.entity.id != entity.id or attrs.get('unit_of_measurement') != sensor.capability.unit:
                                sensor = None
                            else:
                                sensor = sensor.model_copy(update={
                                    'entity': entity.model_copy(update={'capabilities': [], 'state_version': None}),
                                    'device': device.model_copy(update={
                                        'capabilities': [], 'function_ids': [], 'limitations': [],
                                    }),
                                })
                except (IndexError, KeyError, TypeError, ValueError):
                    continue
                if sensor is not None:
                    sensor_candidates.append(sensor)
                continue
            if entity.kind == 'cover':
                if source.identity in reserved:
                    continue
                capabilities = admitted_capabilities(adapter, entity, authority)
                try:
                    if spec is not None:
                        target = cover_binding(spec, source, entity, device, capabilities)
                    else:
                        target = known_covers.get(entity.id)
                        same_binding = target and (target.device.id, target.identity, target.urn,
                                                  target.model, target.registry_uid, bindings(target.entity)) == (
                            device.id, source.identity, urn, model, unique, bindings(entity))
                        if same_binding and all(any(cap.name == c.name and set(c.choices or []) <= set(cap.choices or [])
                                              for cap in capabilities) for c in target.capabilities()):
                            target = target.model_copy(update={
                                'entity': entity.model_copy(update={'capabilities': [], 'state_version': None}),
                                'device': device.model_copy(update={'capabilities': [], 'function_ids': [], 'limitations': []}),
                            })
                        else:
                            target = None
                except (IndexError, KeyError, TypeError, ValueError):
                    continue
                if target is not None:
                    cover_claims[target.service_key] += 1
                    if authority.external_entity_ids is None or source.external in authority.external_entity_ids:
                        cover_candidates.append(target)
                continue
            try:
                match = (power_property(spec, unique, entity.kind, urn, model) if spec is not None
                         else known.get((source.identity, urn, model, unique, entity.kind)))
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            if match is None or match[0] != source.identity:
                continue
            identity, siid, piid = match
            duplicates[(identity, siid, piid)] += 1
            capabilities = admitted_capabilities(adapter, entity, authority)
            if ((authority.external_entity_ids is not None and source.external not in authority.external_entity_ids)
                    or not any(c.name == operation for c in capabilities)
                    or (entity.kind == 'switch' and source.registry['device_id'] in mode_devices)):
                continue
            if spec is not None and entity.kind == 'switch' and any(
                row['type'].split(':')[2:4] == ['property', 'mode']
                for service in spec['services'] if service['iid'] == siid
                for row in service.get('properties', [])
            ):
                continue
            target = PowerTarget(
                entity=entity.model_copy(update={'capabilities': [], 'state_version': None}),
                device=device.model_copy(update={'capabilities': [], 'function_ids': [], 'limitations': []}),
                identity=identity, urn=urn, model=model, registry_uid=unique, siid=siid, piid=piid, operation=operation,
            )
            candidates.append(target)
            try:
                if spec is not None:
                    settings = setting_bindings(spec, target, capabilities)
                else:
                    old = previous_numeric.devices.get(entity.id) if previous_numeric else None
                    supported = {cap.name for cap in capabilities if cap.minimum is not None and cap.maximum is not None}
                    settings = [s for s in old.settings if s.operation in supported] if (
                        old and old.binding == target_digest(target)
                    ) else []
            except (KeyError, TypeError, ValueError, StopIteration):
                settings = []
            if settings:
                numeric_devices[entity.id] = DeviceSettings(binding=target_digest(target), settings=settings)
        targets = sorted((t for t in candidates if duplicates[t.property_key] == 1), key=lambda t: t.entity.id)
        new = PowerCatalog(household=household, family=family, legacy_approval=approval,
                           targets=targets[:MAX_TARGETS])
        if new != self.catalog and (targets or self.catalog is not None):
            body = new.model_dump(mode='json')
            # 上限与冷启动读取一致。
            if len(json.dumps(body, ensure_ascii=False, indent=2).encode()) > MAX_CATALOG_BYTES:
                raise ValueError('mijia_power_catalog_size')
            atomic(self.path, body)
            self.catalog = new
        sensor_count = Counter(s.property_key for s in sensor_candidates)
        sensors = sorted((s for s in sensor_candidates if sensor_count[s.property_key] == 1), key=lambda s: s.entity.id)
        claimed = Counter(t.property_key for t in new.targets)
        claimed.update(s.property_key for s in sensors)
        claimed.update((t.identity, t.siid, s.piid) for t in new.targets if t.entity.id in numeric_devices
                       for s in numeric_devices[t.entity.id].settings)
        # 同一属性若又有独立实体或其他控制解释，只保留已经确认的电源端口。
        devices_with_settings = {}
        for target in new.targets:
            bound = numeric_devices.get(target.entity.id)
            if bound is None:
                continue
            allowed = [s for s in bound.settings if claimed[(target.identity, target.siid, s.piid)] == 1]
            if allowed:
                devices_with_settings[target.entity.id] = bound.model_copy(update={'settings': allowed})
        numeric = NumericCatalog(family=family, legacy_approval=approval, sensors=sensors[:MAX_TARGETS],
                                 devices=devices_with_settings)
        if numeric != self.numeric and (numeric.sensors or numeric.devices or self.numeric is not None):
            body = numeric.model_dump(mode='json')
            if len(json.dumps(body, ensure_ascii=False, indent=2).encode()) > MAX_CATALOG_BYTES:
                raise ValueError('mijia_numeric_catalog_size')
            atomic(self.path.with_name(NUMERIC_CATALOG), body)
            self.numeric = numeric
        covers = CoverCatalog(family=family, household=household, legacy_approval=approval,
                              targets=sorted((t for t in cover_candidates if cover_claims[t.service_key] == 1),
                                             key=lambda t: t.entity.id)[:MAX_TARGETS])
        if covers != self.covers and (covers.targets or self.covers is not None):
            body = covers.model_dump(mode='json')
            if len(json.dumps(body, ensure_ascii=False, indent=2).encode()) > COVER_CATALOG_LIMIT:
                raise ValueError('mijia_cover_catalog_size')
            atomic(self.path.with_name(COVER_CATALOG), body)
            self.covers = covers

    def active(self, authority=None):
        catalog = self.catalog
        cover_catalog = self.covers
        if catalog is None and self.numeric is None and cover_catalog is None:
            return None, {}
        if authority is None:
            authority, _ = self.core.actions.authority(include_inactive=True)
        if not isinstance(authority, HomeDeviceAuthority):
            return None, {}
        context = self.discovery_context()
        if context is None:
            return None, {}
        family, household, grant, models, configuration = context
        approval = cover_approval(authority)
        power_matches = catalog and (family, household, approval) == (
            catalog.family, catalog.household, catalog.legacy_approval)
        cover_matches = cover_catalog and (family, household, approval) == (
            cover_catalog.family, cover_catalog.household, cover_catalog.legacy_approval)
        numeric_matches = self.numeric and (family, approval) == (self.numeric.family, self.numeric.legacy_approval)
        if not (power_matches or cover_matches or numeric_matches):
            return None, {}
        targets = {t.entity.id: t for t in (catalog.targets if power_matches else []) if t.operation in authority.operations
                   and (authority.external_entity_ids is None or bindings(t.entity)[0] in authority.external_entity_ids)
                   and models.get(t.identity) == {'urn': t.urn, 'model': t.model}}
        if cover_matches:
            try:
                reserved = reserved_covers(configuration)
            except ValueError:
                reserved = {t.identity for t in cover_catalog.targets}
            targets.update({t.entity.id: t for t in cover_catalog.targets if t.identity not in reserved
                            and t.operations.intersection(authority.operations)
                            and (authority.external_entity_ids is None or bindings(t.entity)[0] in authority.external_entity_ids)
                            and models.get(t.identity) == {'urn': t.urn, 'model': t.model}})
        return (family, grant), targets

    def numeric_active(self):
        numeric = self.numeric
        context = self.context() if numeric else None
        if context is None:
            return None
        authority, _ = self.core.actions.authority(include_inactive=True)
        if (not isinstance(authority, HomeDeviceAuthority) or numeric.family != context[0]
                or numeric.legacy_approval != cover_approval(authority)):
            return None
        return context

    def active_sensors(self):
        context = self.numeric_active()
        if context is None:
            return {}
        models = context[3]
        return {s.entity.id: s for s in self.numeric.sensors
                if models.get(s.identity) == {'urn': s.urn, 'model': s.model}}

    def settings_for(self, target):
        numeric, catalog = self.numeric, self.catalog
        if (not isinstance(target, PowerTarget) or numeric is None or catalog is None
                or (numeric.family, numeric.legacy_approval) != (catalog.family, catalog.legacy_approval)):
            return []
        bound = numeric.devices.get(target.entity.id)
        return [s for s in bound.settings if s.piid != target.piid] if bound and bound.binding == target_digest(target) else []

    def uses_lan_home(self, adapter):
        return isinstance(adapter, LocalCameraHome) or (
            isinstance(adapter, HAReadOnlyAdapter) and not ha_available(adapter))

    def sample_for(self, context, target):
        return self._sample(context, target.reference, target.identity)

    def _sample(self, context, reference, identity):
        if context is None:
            return None
        value = self.samples.get((context, reference))
        did = identity.split('_', 1)[1]
        if value and time.monotonic() < value.expires and self.power.lan.ready(context[0], did):
            return value
        return None

    def ready_count(self, *, include_sensors=True, include_covers=True):
        context, targets = self.active()
        sensors = self.active_sensors().values() if include_sensors else ()
        return sum(self.version(context, t) is not None for t in (*targets.values(), *sensors)
                   if include_covers or not isinstance(t, CoverTarget))

    def connected_targets(self):
        context, targets = self.active()
        if context is None:
            return None, {}
        return context, {t.entity.id: t for t in (*targets.values(), *self.active_sensors().values())
                         if self.power.lan.ready(context[0], t.identity.split('_', 1)[1])}

    def schedule_bindings(self, rule, authority):
        """只解析规则所用的精确属性。瞬时读数和网关连通性不改变绑定。"""
        if (not isinstance(rule.trigger, CLOCK_TRIGGERS) or not isinstance(authority, HomeDeviceAuthority)
                or rule.execution.require_wan_independent):
            return None
        context, targets = self.active(authority)
        if context is None:
            return None
        # SDK 读数没有设备采样时刻，仍可展示，但不能接替有质量要求的规则条件。
        if any(isinstance(condition, NumericCondition) for condition in rule.conditions):
            return None
        references = {}
        for action in rule.actions:
            if isinstance(action, DeviceInstruction):
                operation, desired = action.operation, action.desired
            elif isinstance(action, (LightInstruction, ClimateInstruction)):
                if isinstance(action, LightInstruction) and (action.brightness is not None or action.require_no_bright_flash):
                    return None
                operation = 'light.set' if isinstance(action, LightInstruction) else 'climate.power'
                desired = {'power': action.power}
            else:
                continue
            target = targets.get(action.target)
            if target is None or operation not in authority.operations:
                return None
            references[target.reference] = (target, None)
            if isinstance(target, CoverTarget):
                # 定时任务只能保存绝对目标，临时停止动作继续从交互入口发出。
                try:
                    if desired == {'state': 'stopped'}:
                        return None
                    target.wire(operation, next(iter(desired.values())))
                except (StopIteration, ValueError):
                    return None
                continue
            if target.operation == operation and power_intent(desired):
                continue
            # HA 调光和设色温可同时开灯。直连的灯光属性写入依赖灯已打开，不能替代原动作。
            if target.entity.kind == 'light':
                return None
            setting = self.resolve_setting(context, target, operation, desired, require_sample=False)
            if setting is None:
                return None
            try:
                setting.wire_value(desired[setting.key])
            except ValueError:
                return None
            references[self.setting_key(target, setting)] = (target, setting)
        return (context, references) if references else None

    @staticmethod
    def schedule_scope(bindings):
        context, references = bindings
        return digest(['mijia-clock/1', context, sorted(references, key=repr)])

    def schedule_matches_ha(self, schedule_bindings, adapter, snapshot):
        """注册 ID 可保留，仍需核对当前小米设备、型号和属性注册键。"""
        _, references = schedule_bindings
        entities = {entity.id: entity for entity in snapshot.entities}
        devices = {device.id: device for device in snapshot.devices}
        for target, setting in references.values():
            if setting is not None:
                continue
            entity = entities.get(target.entity.id)
            device = devices.get(target.device.id)
            if entity is None or device is None or entity.id not in device.function_ids:
                return False
            source = self.power.registry_source(adapter, entity, device)
            if (source is None or bindings(entity) != bindings(target.entity)
                    or source.identity != target.identity or source.registry['unique_id'] != target.registry_uid
                    or source.catalog.get('model') != target.model or source.catalog['urn'] != target.urn):
                return False
        return True

    def schedule_routable(self, bindings):
        context, references = bindings
        return all(self.power.lan.ready(context[0], target.identity.split('_', 1)[1])
                   for target, _ in references.values())

    def schedule_fresh(self, bindings):
        context, references = bindings
        return all((self.version(context, target) is not None if isinstance(target, CoverTarget)
                    else self._sample(context, reference, target.identity) is not None)
                   for reference, (target, _) in references.items())

    async def read_schedule(self, bindings, *, refresh=False):
        context, references = bindings
        for reference, (target, setting) in references.items():
            if not refresh and (self.version(context, target) is not None if isinstance(target, CoverTarget)
                                else self._sample(context, reference, target.identity) is not None):
                continue
            writer = (self.setting_writer(context, target, setting) if setting else
                      self.writer(target.entity.id, context=context, target=target))
            if writer is None:
                raise ValueError('mijia_lan_unavailable')
            await writer.readback()

    def unqualified_references(self, targets):
        # 保留现有手势目标，并检查本地通道能表达它实际绑定的动作。
        from .embodied.household import DeviceCommands, supported_actions
        from .embodied.interaction import load

        interactions = load()
        if not interactions.enabled:
            return set()
        missing = set()
        for binding in interactions.bindings:
            if not binding.enabled:
                continue
            if binding.target == 'friday:configured-cover':
                permission = self.core.actions.cover_permission() or {}
                actions = supported_actions({'kind': 'cover', 'source_kind': 'miot',
                                             'capabilities': permission.get('operations', [])})
            elif isinstance(target := targets.get(binding.target), CoverTarget):
                caps = {c.name: c.model_dump() for c in target.capabilities()}
                item = {'kind': 'cover', 'source_kind': 'cover', 'capabilities': list(caps), 'capability_specs': caps}
                try:
                    operation, desired = DeviceCommands.parameters(item, binding.action)
                    target.wire(operation, next(iter(desired.values())))
                except (KeyError, ValueError):
                    missing.add(binding.target)
                continue
            elif isinstance(target, PowerTarget):
                actions = supported_actions({'kind': target.entity.kind, 'capabilities': [target.operation]})
            else:
                actions = ()
            if binding.action not in actions:
                missing.add(binding.target)
        return missing

    async def qualify_source(self):
        # 切换先实读一个准确绑定的属性。网关在线或缓存里有设备都不能代替读回。
        context, targets = self.connected_targets()
        for target in list(targets.values())[:4]:
            try:
                if isinstance(target, Sensor):
                    await self.read_sensor(context, target)
                else:
                    writer = self.writer(target.entity.id, context=context, target=target)
                    if writer is None:
                        continue
                    await writer.readback()
            except (OSError, ValueError, TimeoutError, httpx.HTTPError):
                continue

            def current():
                live, connected = self.connected_targets()
                return (live == context and connected.get(target.entity.id) == target
                        and self.sample_for(context, target) is not None)

            if current():
                return current
        return None

    def active_setting(self, context, target, setting):
        if (sample := self.sample_for(context, target)) is None:
            return None
        if target.entity.kind == 'light' and sample.value != 'on':
            return None
        return setting if self.setting_sample(context, target, setting) else None

    def setting_sample(self, context, target, setting):
        return self._sample(context, self.setting_key(target, setting), target.identity)

    def setting_key(self, target, setting):
        return target.reference, setting.model_dump_json()

    def version(self, context, target):
        primary = self.sample_for(context, target)
        if primary is None or (isinstance(primary.value, CoverReading) and not primary.value.healthy):
            return None
        settings = [(s.key, sample.version) for s in self.settings_for(target)
                    if (sample := self.setting_sample(context, target, s)) is not None]
        if not settings:
            return primary.version
        return hashlib.sha256(repr((primary.version, settings)).encode()).hexdigest()

    def resolve_setting(self, context, target, operation, desired, *, require_sample=True):
        matching = [s for s in self.settings_for(target) if s.operation == operation
                    and set(desired) == ({'power', s.key} if s.key == 'brightness' else {s.key})
                    and (s.key != 'brightness' or desired.get('power') == 'on')]
        if len(matching) != 1:
            return None
        return self.active_setting(context, target, matching[0]) if require_sample else matching[0]

    def intent_error(self, target_id, operation, desired, authority):
        context, targets = self.active(authority)
        target = targets.get(target_id)
        if target is None:
            return 'unauthorized'
        if isinstance(target, CoverTarget):
            if operation not in authority.operations:
                return 'unauthorized'
            try:
                key = 'position' if operation == 'cover.position' else 'state'
                target.wire(operation, desired[key])
            except (KeyError, ValueError):
                return 'unsupported_parameters'
            return None
        if target.operation == operation and power_intent(desired):
            return None
        if operation not in authority.operations:
            return 'unauthorized'
        setting = self.resolve_setting(context, target, operation, desired)
        if setting is None:
            return 'unsupported'
        try:
            setting.wire_value(desired[setting.key])
        except ValueError:
            return 'unsupported_parameters'
        return None

    def fingerprint(self, entity_id):
        context, targets = self.active()
        if (target := targets.get(entity_id)) is None:
            return None
        catalog = self.covers if isinstance(target, CoverTarget) else self.catalog
        return hashlib.sha256(repr((context, target.reference, catalog.legacy_approval)).encode()).hexdigest()

    def state_version(self, entity_id):
        context, targets = self.active()
        target = targets.get(entity_id)
        return self.version(context, target) if target else None

    def snapshot(self, adapter):
        if not self.uses_lan_home(adapter):
            return None
        context, targets = self.active()
        sensors = self.active_sensors()
        if not targets and not sensors:
            return None
        now = datetime.now(UTC)
        entities, observations, states = [], [], []
        groups = defaultdict(list)
        devices = {}
        for target in targets.values():
            device, entity = target.device, target.entity
            groups[device.id].append(entity.id)
            devices[device.id] = device
            sample = self.sample_for(context, target)
            capability = f'{entity.kind}.state'
            known = self.settings_for(target)
            writable = [s for setting in known if (s := self.active_setting(context, target, setting)) is not None]
            read_caps = [Capability(name=capability, access='read', support='supported')]
            write_caps = {s.operation: s.capability() for s in writable}
            value = sample.value if sample else None
            healthy = sample is not None
            if isinstance(target, CoverTarget):
                if sample:
                    value, healthy = sample.value.state, sample.value.healthy
                    if healthy:
                        for key, amount in (('current_position', sample.value.position),
                                            ('target_position', sample.value.target_position)):
                            if amount is not None:
                                observations.append(Observation(
                                    entity_id=entity.id, capability=OBSERVATION_PREFIX + key, value=amount,
                                    physical_source_id=entity.physical_source_id, ingest_source=MODE,
                                    received_at=sample.received_at, freshness_basis='upstream_report',
                                    semantics='latched_state', transport_health='online',
                                ))
                if healthy:
                    write_caps = {c.name: c for c in target.capabilities()}
            elif sample and target.operation not in write_caps:
                write_caps[target.operation] = Capability(name=target.operation, access='write', support='supported')
            entities.append(entity.model_copy(update={
                'capabilities': read_caps + list(write_caps.values()),
                'state_version': self.version(context, target),
            }))
            states.append(CanonicalState(
                id=f'{entity.id}:{capability}', value=value,
                quality='healthy' if healthy else 'unknown',
                reasons=['mijia_lan_property_read' if healthy else 'mijia_lan_read_unavailable'],
                selected_source=entity.physical_source_id if healthy else None,
                as_of=sample.received_at if sample else now,
                valid_until=sample.received_at + timedelta(seconds=SAMPLE_TTL) if sample else None,
            ))
            if sample:
                observations.append(Observation(
                    entity_id=entity.id, capability=capability, value=value,
                    physical_source_id=entity.physical_source_id, ingest_source=MODE,
                    received_at=sample.received_at, freshness_basis='upstream_report',
                    semantics='latched_state', transport_health='online',
                ))
            for setting in known:
                reported = self.setting_sample(context, target, setting)
                if reported is None:
                    continue
                observations.append(Observation(
                    entity_id=entity.id, capability=OBSERVATION_PREFIX + setting.key, value=reported.value,
                    physical_source_id=entity.physical_source_id, ingest_source=MODE,
                    received_at=reported.received_at, freshness_basis='upstream_report',
                    semantics='latched_state', transport_health='online',
                ))
        for sensor in sensors.values():
            device, entity, cap = sensor.device, sensor.entity, sensor.capability
            sample = self.sample_for(context, sensor)
            groups[device.id].append(entity.id)
            devices.setdefault(device.id, device)
            entities.append(entity.model_copy(update={'capabilities': [cap], 'state_version': sample.version if sample else None}))
            observation = Observation(
                entity_id=entity.id, capability=cap.name, unit=cap.unit, value=sample.value if sample else None,
                physical_source_id=entity.physical_source_id, ingest_source=MODE,
                received_at=sample.received_at if sample else now, freshness_basis='upstream_report',
                semantics='periodic_sample', transport_health='online' if sample else 'offline',
            )
            observations.append(observation)
            states.append(resolve(observation, now, ttl_seconds=SAMPLE_TTL))
        versions = [(e.id, e.state_version) for e in entities]
        version = hashlib.sha256(repr((context, versions)).encode()).hexdigest()
        return Snapshot(mode=MODE, version=f'{MODE}-{version}', generated_at=now, entities=entities,
                        observations=observations, states=states, rules=[], events=[],
                        devices=[d.model_copy(update={'function_ids': groups[d.id]}) for d in devices.values()],
                        limitations=['mijia_poll_is_not_an_event_source'])

    def writer(self, entity_id, *, context=None, target=None, deadline=None, revision=None, guard=None,
               operation=None, desired=None, reconcile=False):
        if target is None:
            context, targets = self.active()
            target = targets.get(entity_id)
        if target is None:
            return None
        if isinstance(target, CoverTarget):
            return self.cover_writer(context, target, operation=operation, desired=desired,
                                     deadline=deadline, revision=revision, guard=guard)
        if desired and not power_intent(desired):
            setting = self.resolve_setting(context, target, operation, desired, require_sample=not reconcile)
            if setting is None:
                return None
            return self.setting_writer(context, target, setting, deadline=deadline, revision=revision, guard=guard)
        family, grant = context
        _, siid, piid = target.property_key
        did = target.identity.split('_', 1)[1]
        key = context, target.reference

        def current():
            refreshed, targets = self.active()
            selected = targets.get(entity_id)
            return (refreshed == context and selected is not None and selected.reference == target.reference
                    and self.power.lan.ready(family, did))

        async def request(payload):
            if 'power' not in payload:
                return await self.power.lan.request({**payload, 'registered_power': True}, family=family)
            if deadline is None or revision is None or guard is None:
                raise ValueError('mijia_permission_changed')
            return await self.power.lan.request(
                {**payload, 'authority_revision': revision, 'deadline': deadline}, family=family,
                guard=lambda: current() and guard())

        if not self.power.lan.ready(family, did):
            return None
        return MijiaPowerWriter(request, {'did': did, 'siid': siid, 'piid': piid, 'grant': grant},
                                current, channel='lan', family=family,
                                on_readback=lambda row: self.record(key, row['power'], current))

    def record(self, key, value, current, *, comparison=None):
        if not current():
            raise ValueError('mijia_permission_changed')
        previous = self.samples.get(key)
        now = time.monotonic()
        comparison = value if comparison is None else comparison
        version = (previous.version if previous and previous.expires > now and previous.comparison == comparison
                   else uuid4().hex)
        self.samples[key] = Sample(value, datetime.now(UTC), now + SAMPLE_TTL, version, comparison)

    def cover_writer(self, context, target, *, operation, desired, deadline, revision, guard):
        family, grant = context
        did = target.identity.split('_', 1)[1]
        binding = {'registered_cover': cover_digest(target), 'did': did, 'grant': grant}
        if not self.power.lan.ready(family, did):
            return None

        def current():
            refreshed, targets = self.active()
            selected = targets.get(target.entity.id)
            return (refreshed == context and selected is not None and selected.reference == target.reference
                    and self.power.lan.ready(family, did))

        async def request(fields):
            if not current():
                raise ValueError('mijia_permission_changed')
            if not fields:
                return await self.power.lan.request(binding, family=family)
            if deadline is None or revision is None or guard is None:
                raise ValueError('mijia_permission_changed')
            return await self.power.lan.request(
                {**binding, **fields, 'authority_revision': revision, 'deadline': deadline}, family=family,
                guard=lambda: current() and guard(),
            )

        return CoverWriter(target, request, current, lambda row: self.record((context, target.reference), row, current),
                           family, operation, desired)

    def setting_writer(self, context, target, setting, *, deadline=None, revision=None, guard=None):
        family, grant = context
        did = target.identity.split('_', 1)[1]
        binding = {'numeric': True, 'did': did, 'siid': target.siid, 'piid': setting.piid, 'grant': grant}

        def current():
            refreshed, targets = self.active()
            selected = targets.get(target.entity.id)
            return (refreshed == context and selected is not None and selected.reference == target.reference
                    and setting in self.settings_for(selected) and self.power.lan.ready(family, did))

        async def request(payload):
            if not current():
                raise ValueError('mijia_permission_changed')
            if 'value' not in payload:
                return await self.power.lan.request(payload, family=family)
            if deadline is None or revision is None or guard is None:
                raise ValueError('mijia_permission_changed')
            return await self.power.lan.request(
                {**payload, 'authority_revision': revision, 'deadline': deadline}, family=family,
                guard=lambda: current() and guard(),
            )

        return SettingWriter(setting, binding, family, request, current,
                             lambda value, raw: self.record((context, self.setting_key(target, setting)), value, current,
                                                            comparison=raw))

    async def read_sensor(self, context, sensor):
        family, grant = context
        did = sensor.identity.split('_', 1)[1]
        result = await self.power.lan.request(
            {'numeric': True, 'did': did, 'siid': sensor.siid, 'piid': sensor.piid, 'grant': grant}, family=family)
        value = result.get('value')
        if not sensor.scale.accepts(value, exact_step=False):
            raise ValueError('mijia_invalid_readback')

        def current():
            live = self.context()
            return (live is not None and (live[0], live[2]) == context and
                    self.active_sensors().get(sensor.entity.id) == sensor and self.power.lan.ready(family, did))

        self.record((context, sensor.reference), value, current)

    async def refresh_hints(self, context, entries, now):
        fetch = getattr(self.power.lan, 'property_notifications', None)
        if fetch is None or now - self.last_hint_check < 3:
            return
        if context != self.watch_context:
            self.hints.clear()
            self.watch_context = context
        refs = {}
        for reference, target, setting in entries:
            did = target.identity.split('_', 1)[1]
            if self.power.lan.ready(context[0], did):
                piids = (target.position_piid, target.status_piid, target.fault_piid,
                         target.target_piid if target.target_readable else None) if isinstance(target, CoverTarget) else (
                             setting.piid if setting else target.piid,)
                for piid in piids:
                    if piid is not None:
                        refs[(did, target.siid, piid)] = reference
        # 已登记开关优先订阅。超过预算的设备仍按普通周期读取。
        selected = dict(list(refs.items())[:48])
        self.hints.intersection_update(selected.values())
        self.last_hint_check = now
        try:
            changes = await fetch(context[0], [list(row) for row in selected])
        except (OSError, TimeoutError, ValueError):
            return
        self.hints.update(selected[row] for row in changes if row in selected)

    async def sample_once(self):
        if not self.uses_lan_home(self.core.adapter):
            # 正在评估直连切换时保留刚实读的短期证据，避免定时清理打断逐设备核验。
            now = time.monotonic()
            self.samples = {key: sample for key, sample in self.samples.items() if sample.expires > now}
            self.last_poll.clear()
            if self.watch_context is not None:
                fetch = getattr(self.power.lan, 'property_notifications', None)
                try:
                    if fetch is not None:
                        await fetch(self.watch_context[0], [])
                except (OSError, TimeoutError, ValueError):
                    pass
                self.watch_context = None
                self.hints.clear()
            return
        # 操作与路由探测优先，后台每轮只占用一个局域网读取。
        if self.core.actions._lock.locked() or self.power.lan.io.locked():
            return
        context, targets = self.active()
        sensors = self.active_sensors()
        if context is None:
            return
        entries = [(t.reference, t, None) for t in targets.values()]
        entries.extend((s.reference, s, None) for s in sensors.values())
        entries.extend((self.setting_key(t, s), t, s) for t in targets.values() for s in self.settings_for(t))
        keys = {key for key, _, _ in entries}
        self.samples = {k: v for k, v in self.samples.items() if k[0] == context and k[1] in keys}
        self.last_poll = {k: v for k, v in self.last_poll.items() if k[0] == context and k[1] in keys}
        now = time.monotonic()
        adapter = self.core.adapter
        await self.refresh_hints(context, entries, now)
        if adapter is not self.core.adapter or not self.uses_lan_home(adapter) or self.core.actions._lock.locked():
            return
        # 优先补足周期读回，避免频繁变化的设备挤掉其他设备。
        def order(entry):
            recent = self.last_poll.get((context, entry[0]), 0)
            return now - recent < 20, entry[0] not in self.hints, recent

        for reference, target, setting in sorted(entries, key=order):
            key = context, reference
            elapsed = now - self.last_poll.get(key, now - 20)
            if elapsed < 20 and (reference not in self.hints or elapsed < 2):
                continue
            did = target.identity.split('_', 1)[1]
            if not self.power.lan.ready(context[0], did):
                continue
            if setting and target.entity.kind == 'light':
                power = self.sample_for(context, target)
                if power is None or power.value != 'on':
                    self.samples.pop(key, None)
                    continue
            self.hints.discard(reference)
            self.last_poll[key] = now
            try:
                if setting:
                    await self.setting_writer(context, target, setting).readback()
                elif isinstance(target, Sensor):
                    await self.read_sensor(context, target)
                else:
                    writer = self.writer(target.entity.id, context=context, target=target)
                    if writer is not None:
                        await writer.readback()
            except (OSError, ValueError, TimeoutError, httpx.HTTPError):
                self.samples.pop(key, None)
            break

    async def sample(self):
        while True:
            await self.sample_once()
            await asyncio.sleep(.35)
