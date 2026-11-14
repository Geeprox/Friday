"""保留现有设备绑定，在 HA 暂不可用时从同一米家中枢读写电源。"""

import asyncio
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from .mijia_control import POWER_BY_KIND, MijiaPowerWriter, power_property
from .mijia_lan_client import FAMILY

CATALOG = 'mijia-power-catalog.json'
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


def catalog_available(directory):
    try:
        return bool(read_catalog(directory).targets)
    except (OSError, ValueError):
        return False


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


def ha_available(adapter):
    health = adapter.health()
    return (health.get('transport') == 'online' and health.get('authentication_available') is True
            and health.get('registry_available') is True and not health.get('observation_gap'))


@dataclass(frozen=True)
class Sample:
    value: str
    received_at: datetime
    expires: float
    version: str


class MijiaPowerHome:
    def __init__(self, core, power):
        self.core, self.power = core, power
        self.path = core.actions.authority_path.with_name(CATALOG)
        self.catalog = None
        try:
            self.catalog = read_catalog(self.path.parent)
        except (OSError, ValueError):
            pass
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
        snapshot = adapter._last
        devices = {fid: device for device in snapshot.devices if device.category == 'device'
                   for fid in device.function_ids}
        # 能切换工作模式的开关通道须先支持独立模式读取，才可以从 HA 迁移。
        mode_devices = {adapter._registry.get(e, {}).get('device_id') for e in adapter._switch_channels}
        candidates = []
        duplicates = Counter()
        for entity in snapshot.entities:
            device = devices.get(entity.id)
            operation = POWER_BY_KIND.get(entity.kind)
            if (device is None or entity.simulated or device.simulated
                    or operation is None or not bindings(entity)):
                continue
            source = self.power.registry_source(adapter, entity, device)
            if source is None:
                continue
            urn, model = source.catalog['urn'], source.catalog.get('model')
            unique = source.registry['unique_id']
            spec = self.power.specs.get(urn)
            try:
                match = (power_property(spec, unique, entity.kind, urn, model) if spec is not None
                         else known.get((source.identity, urn, model, unique, entity.kind)))
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            if match is None or match[0] != source.identity:
                continue
            identity, siid, piid = match
            duplicates[(identity, siid, piid)] += 1
            if ((authority.external_entity_ids is not None and source.external not in authority.external_entity_ids)
                    or not any(c.name == operation for c in admitted_capabilities(adapter, entity, authority))
                    or (entity.kind == 'switch' and source.registry['device_id'] in mode_devices)):
                continue
            if spec is not None and entity.kind == 'switch' and any(
                row['type'].split(':')[2:4] == ['property', 'mode']
                for service in spec['services'] if service['iid'] == siid
                for row in service.get('properties', [])
            ):
                continue
            candidates.append(PowerTarget(
                entity=entity.model_copy(update={'capabilities': [], 'state_version': None}),
                device=device.model_copy(update={'capabilities': [], 'function_ids': [], 'limitations': []}),
                identity=identity, urn=urn, model=model, registry_uid=unique, siid=siid, piid=piid, operation=operation,
            ))
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

    def active(self, authority=None):
        catalog = self.catalog
        if catalog is None or not catalog.targets:
            return None, {}
        if authority is None:
            authority, _ = self.core.actions.authority(include_inactive=True)
        if not isinstance(authority, HomeDeviceAuthority) or cover_approval(authority) != catalog.legacy_approval:
            return None, {}
        context = self.context()
        if context is None:
            return None, {}
        family, household, grant, models = context
        if (family, household) != (catalog.family, catalog.household):
            return None, {}
        targets = {t.entity.id: t for t in catalog.targets if t.operation in authority.operations
                   and (authority.external_entity_ids is None or bindings(t.entity)[0] in authority.external_entity_ids)
                   and models.get(t.identity) == {'urn': t.urn, 'model': t.model}}
        return (family, grant), targets

    def uses_lan_home(self, adapter):
        return isinstance(adapter, LocalCameraHome) or (
            isinstance(adapter, HAReadOnlyAdapter) and not ha_available(adapter))

    def sample_for(self, context, target):
        value = self.samples.get((context, target.reference))
        did = target.identity.split('_', 1)[1]
        if value and time.monotonic() < value.expires and self.power.lan.ready(context[0], did):
            return value
        return None

    def ready_count(self):
        context, targets = self.active()
        return sum(self.sample_for(context, t) is not None for t in targets.values())

    def fingerprint(self, entity_id):
        context, targets = self.active()
        if (target := targets.get(entity_id)) is None:
            return None
        return hashlib.sha256(repr((context, target.reference, self.catalog.legacy_approval)).encode()).hexdigest()

    def state_version(self, entity_id):
        context, targets = self.active()
        target = targets.get(entity_id)
        sample = self.sample_for(context, target) if target else None
        return sample.version if sample else None

    def snapshot(self, adapter):
        if not self.uses_lan_home(adapter):
            return None
        context, targets = self.active()
        if not targets:
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
            entities.append(entity.model_copy(update={
                'capabilities': [Capability(name=capability, access='read', support='supported')]
                + ([Capability(name=target.operation, access='write', support='supported')] if sample else []),
                'state_version': sample.version if sample else None,
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
        versions = [(e.id, e.state_version) for e in entities]
        version = hashlib.sha256(repr((context, versions)).encode()).hexdigest()
        return Snapshot(mode=MODE, version=f'{MODE}-{version}', generated_at=now, entities=entities,
                        observations=observations, states=states, rules=[], events=[],
                        devices=[d.model_copy(update={'function_ids': groups[d.id]}) for d in devices.values()],
                        limitations=['mijia_power_only', 'mijia_poll_is_not_an_event_source'])

    def writer(self, entity_id, *, context=None, target=None, deadline=None, revision=None, guard=None):
        if target is None:
            context, targets = self.active()
            target = targets.get(entity_id)
        if target is None:
            return None
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

        def record(reported):
            if not current():
                return
            previous = self.samples.get(key)
            now = time.monotonic()
            value = reported['power']
            version = (previous.version if previous and previous.expires > now and previous.value == value
                       else uuid4().hex)
            self.samples[key] = Sample(value, datetime.fromisoformat(reported['received_at']),
                                       now + SAMPLE_TTL, version)

        if not self.power.lan.ready(family, did):
            return None
        return MijiaPowerWriter(request, {'did': did, 'siid': siid, 'piid': piid, 'grant': grant},
                                current, channel='lan', family=family, on_readback=record)

    async def sample_once(self):
        if not self.uses_lan_home(self.core.adapter):
            self.samples.clear()
            self.last_poll.clear()
            return
        # 操作与路由探测优先，后台每轮只占用一个局域网读取。
        if self.core.actions._lock.locked() or self.power.lan.io.locked():
            return
        context, targets = self.active()
        keys = {t.reference for t in targets.values()}
        self.samples = {k: v for k, v in self.samples.items() if k[0] == context and k[1] in keys}
        self.last_poll = {k: v for k, v in self.last_poll.items() if k[0] == context and k[1] in keys}
        now = time.monotonic()
        for target in sorted(targets.values(), key=lambda t: self.last_poll.get((context, t.reference), 0)):
            key = context, target.reference
            if now - self.last_poll.get(key, now - 20) < 20:
                break
            writer = self.writer(target.entity.id, context=context, target=target)
            if writer is None:
                continue
            self.last_poll[key] = now
            try:
                await writer.readback()
            except (OSError, ValueError, TimeoutError, httpx.HTTPError):
                self.samples.pop(key, None)
            break

    async def sample(self):
        while True:
            await self.sample_once()
            await asyncio.sleep(.35)
