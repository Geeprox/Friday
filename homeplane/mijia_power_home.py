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
from .quality import resolve

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
        return bool(read_numeric_catalog(directory).sensors)
    except (OSError, ValueError):
        return False


def target_digest(target):
    return hashlib.sha256(repr(target.reference).encode()).hexdigest()


def authorize_send(directory, revision, deadline, family, region, did, siid, piid):
    remaining = deadline - time.monotonic() if type(deadline) in (int, float) else -1
    if not math.isfinite(remaining) or not 0 < remaining <= 60:
        return False
    authority, version = read_device_authority(directory / 'action-authority.json', include_generation=True)
    if not isinstance(authority, HomeDeviceAuthority) or version != revision:
        return False
    catalog = read_catalog(directory)
    if catalog.family != family or catalog.legacy_approval != cover_approval(authority):
        return False
    targets = [target for target in catalog.targets if target.property_key == (f'{region}_{did}', siid, piid)]
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
    value: str | int | float
    received_at: datetime
    expires: float
    version: str
    comparison: str | int | float


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
        self.samples = {}
        self.last_poll = {}

    def context(self):
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
            _, _, inputs = home_input(directory)
            if (inputs != report.get('inputs')
                    or grant_binding(read_lease(directory / 'miot-session.json')) != grant):
                return None
        except (OSError, TypeError, ValueError, KeyError):
            return None
        models = report.get('models')
        return (family, household, grant, models) if isinstance(models, dict) else None

    def migrate(self):
        from .embodied.ptz_control import atomic

        adapter = self.core.adapter
        authority, _ = self.core.actions.authority(include_inactive=True)
        if (not isinstance(adapter, HAReadOnlyAdapter) or not isinstance(authority, HomeDeviceAuthority)
                or not ha_available(adapter) or adapter._last is None
                or not adapter._services
                or authority.ha_instance != adapter.instance_id or authority.ha_url.rstrip('/') != adapter.url):
            return
        context = self.context()
        if context is None:
            return
        family, household, _, _ = context
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
        numeric_devices = {}
        for entity in snapshot.entities:
            device = devices.get(entity.id)
            operation = POWER_BY_KIND.get(entity.kind)
            if (device is None or entity.simulated or device.simulated
                    or (entity.kind != 'sensor' and (operation is None or not bindings(entity)))):
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

    def active(self, authority=None):
        catalog = self.catalog
        if catalog is None and self.numeric is None:
            return None, {}
        if authority is None:
            authority, _ = self.core.actions.authority(include_inactive=True)
        if not isinstance(authority, HomeDeviceAuthority):
            return None, {}
        context = self.context()
        if context is None:
            return None, {}
        family, household, grant, models = context
        approval = cover_approval(authority)
        if catalog is None or (family, household, approval) != (catalog.family, catalog.household, catalog.legacy_approval):
            if self.numeric and (family, approval) == (self.numeric.family, self.numeric.legacy_approval):
                return (family, grant), {}
            return None, {}
        targets = {t.entity.id: t for t in catalog.targets if t.operation in authority.operations
                   and (authority.external_entity_ids is None or bindings(t.entity)[0] in authority.external_entity_ids)
                   and models.get(t.identity) == {'urn': t.urn, 'model': t.model}}
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
        if (numeric is None or catalog is None
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

    def ready_count(self, *, include_sensors=True):
        context, targets = self.active()
        sensors = self.active_sensors().values() if include_sensors else ()
        return sum(self.sample_for(context, t) is not None for t in (*targets.values(), *sensors))

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
        if primary is None:
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
        return hashlib.sha256(repr((context, target.reference, self.catalog.legacy_approval)).encode()).hexdigest()

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
            if sample and target.operation not in write_caps:
                write_caps[target.operation] = Capability(name=target.operation, access='write', support='supported')
            entities.append(entity.model_copy(update={
                'capabilities': read_caps + list(write_caps.values()),
                'state_version': self.version(context, target),
            }))
            states.append(CanonicalState(
                id=f'{entity.id}:{capability}', value=sample.value if sample else None,
                quality='healthy' if sample else 'unknown',
                reasons=['mijia_lan_property_read' if sample else 'mijia_lan_read_unavailable'],
                selected_source=entity.physical_source_id if sample else None,
                as_of=sample.received_at if sample else now,
                valid_until=sample.received_at + timedelta(seconds=SAMPLE_TTL) if sample else None,
            ))
            if sample:
                observations.append(Observation(
                    entity_id=entity.id, capability=capability, value=sample.value,
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
                return await self.power.lan.request(payload, family=family)
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

    async def sample_once(self):
        if not self.uses_lan_home(self.core.adapter):
            self.samples.clear()
            self.last_poll.clear()
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
        for reference, target, setting in sorted(entries, key=lambda entry: self.last_poll.get((context, entry[0]), 0)):
            key = context, reference
            if now - self.last_poll.get(key, now - 20) < 20:
                continue
            did = target.identity.split('_', 1)[1]
            if not self.power.lan.ready(context[0], did):
                continue
            if setting and target.entity.kind == 'light':
                power = self.sample_for(context, target)
                if power is None or power.value != 'on':
                    self.samples.pop(key, None)
                    continue
            self.last_poll[key] = now
            try:
                if setting:
                    await self.setting_writer(context, target, setting).readback()
                elif isinstance(target, Sensor):
                    await self.read_sensor(context, target)
                else:
                    writer = self.writer(target.entity.id, context=context, target=target)
                    await writer.readback()
            except (OSError, ValueError, TimeoutError, httpx.HTTPError):
                self.samples.pop(key, None)
            break

    async def sample(self):
        while True:
            await self.sample_once()
            await asyncio.sleep(.35)
