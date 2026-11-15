"""按米家规格绑定已登记的窗帘，并独立验证实际行程读回。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import math
import re
import time
from typing import Literal

from pydantic import Field, model_validator

from .action_contracts import HomeDeviceAuthority
from .actions import _read_authority_file, cover_approval, read_device_authority
from .contracts import Capability, Contract, Entity, PhysicalDevice
from .devices import xiaomi_identifier
from .embodied.miot_spec import properties
from .embodied.paths import configuration
from .ha_control import bindings
from .mijia_control import registry_prefix
from .mijia_lan_client import FAMILY
from .mijia_numeric import INTEGER_FORMATS


COVER_CATALOG = 'mijia-cover-catalog.json'
COVER_CATALOG_LIMIT = 512 * 1024
MOTOR_COMMANDS = {'open': 'open', 'up': 'open', 'close': 'closed',
                  'down': 'closed', 'stop': 'stopped', 'pause': 'stopped'}
MOTION_STATES = {'opening': 'opening', 'closing': 'closing', 'stop': 'stopped',
                 'stopped': 'stopped', 'pause': 'stopped', 'paused': 'stopped'}
NO_FAULT = {'nofault': 'clear', 'nofaults': 'clear', 'normal': 'clear', 'none': 'clear',
            'nofaultdetected': 'clear'}


def reserved_covers(config):
    """同一设备由手工绑定独占，尤其是使用叶片角度的设备。"""
    covers = config.get('miot_covers', {})
    if not isinstance(covers, dict):
        raise ValueError('mijia_invalid_cover_config')
    return {f"{row.get('region', 'cn')}_{row['did']}" for row in covers.values()
            if isinstance(row, dict) and isinstance(row.get('did'), (int, str))}


class CoverTarget(Contract):
    entity: Entity
    device: PhysicalDevice
    identity: str = Field(pattern=r'^[a-z]{2}_(?:[0-9]{1,32}|blt\.[0-9]\.[a-z0-9]{1,64})$')
    model: str
    urn: str
    registry_uid: str
    siid: int = Field(strict=True, ge=1, le=65535)
    position_piid: int | None = Field(default=None, strict=True, ge=1, le=65535)
    target_piid: int | None = Field(default=None, strict=True, ge=1, le=65535)
    target_readable: bool = False
    target_writable: bool = False
    step: int = Field(default=1, strict=True, ge=1, le=100)
    motor_piid: int | None = Field(default=None, strict=True, ge=1, le=65535)
    commands: dict[Literal['open', 'closed', 'stopped'], int] = Field(default_factory=dict)
    status_piid: int | None = Field(default=None, strict=True, ge=1, le=65535)
    motion: dict[Literal['opening', 'closing', 'stopped'], int] = Field(default_factory=dict)
    fault_piid: int = Field(strict=True, ge=1, le=65535)

    @model_validator(mode='after')
    def check_binding(self):
        ids = [x for x in (self.position_piid, self.target_piid, self.motor_piid, self.status_piid, self.fault_piid)
               if x is not None]
        if (self.entity.kind != 'cover' or not bindings(self.entity) or self.entity.simulated
                or self.device.simulated or self.device.category != 'device'
                or not any(b.source == 'xiaomi-identity' and b.external_id == self.identity
                           for b in self.device.bindings) or 100 % self.step or len(ids) != len(set(ids))
                or bool(self.target_piid) != (self.target_readable or self.target_writable)
                or not (self.position_piid or self.target_readable and self.status_piid)
                or not self.operations or bool(self.commands) != bool(self.motor_piid)
                or self.commands and not {'open', 'closed'} <= self.commands.keys()
                or bool(self.motion) != bool(self.status_piid)
                or any(len(set(mapping.values())) != len(mapping) or
                       any(not -2147483648 <= v <= 4294967295 for v in mapping.values())
                       for mapping in (self.commands, self.motion))):
            raise ValueError('mijia_invalid_cover_binding')
        return self

    @property
    def service_key(self):
        return self.identity, self.siid

    @property
    def reference(self):
        return (self.entity.id, self.device.id, self.identity, self.model, self.urn, self.registry_uid,
                bindings(self.entity), self.siid, self.position_piid, self.target_piid, self.target_readable,
                self.target_writable, self.step, self.motor_piid,
                tuple(sorted(self.commands.items())), self.status_piid, tuple(sorted(self.motion.items())),
                self.fault_piid)

    @property
    def operations(self):
        return frozenset(c.name for c in self.capabilities())

    def capabilities(self):
        caps = []
        if self.target_writable:
            caps.append(Capability(name='cover.position', access='write', support='supported',
                                   unit='%', minimum=0, maximum=100, step=self.step))
        if self.commands:
            caps.append(Capability(name='cover.state', access='write', support='supported',
                                   choices=list(self.commands)))
        return caps

    def wire(self, operation, value):
        if (operation == 'cover.position' and self.target_writable and type(value) is int
                and 0 <= value <= 100 and value % self.step == 0):
            return self.target_piid, value
        if operation == 'cover.state' and isinstance(value, str) and value in self.commands:
            return self.motor_piid, self.commands[value]
        raise ValueError('mijia_invalid_cover_command')


class CoverCatalog(Contract):
    household: str = Field(pattern=FAMILY.pattern + '$')
    family: str = Field(pattern=FAMILY.pattern + '$')
    legacy_approval: str = Field(pattern=FAMILY.pattern + '$')
    targets: list[CoverTarget] = Field(max_length=512)

    @model_validator(mode='after')
    def unique_bindings(self):
        if (len({t.entity.id for t in self.targets}) != len(self.targets)
                or len({t.service_key for t in self.targets}) != len(self.targets)):
            raise ValueError('mijia_invalid_cover_binding')
        return self


def read_cover_catalog(directory):
    body, _ = _read_authority_file(directory / COVER_CATALOG, limit=COVER_CATALOG_LIMIT)
    return CoverCatalog.model_validate_json(body)


def cover_digest(target):
    return hashlib.sha256(repr(target.reference).encode()).hexdigest()


def _codes(prop, aliases, access):
    if prop is None or access not in prop['access'] or prop.get('format') not in INTEGER_FORMATS:
        return {}
    raw = prop.get('value-list')
    if not isinstance(raw, list):
        return {}
    result, seen = {}, set()
    for row in raw:
        if (not isinstance(row, dict) or type(value := row.get('value')) is not int
                or not -2147483648 <= value <= 4294967295 or value in seen):
            return {}
        seen.add(value)
        name = aliases.get(re.sub(r'[^a-z]', '', str(row.get('description', '')).lower()))
        if name is not None:
            if name in result:
                return {}
            result[name] = value
    return result


def _position_step(prop, access):
    if (prop is None or access not in prop['access'] or prop.get('format') not in INTEGER_FORMATS
            or prop.get('unit') not in (None, 'percentage') or prop.get('value-list') is not None):
        return None
    limits = prop.get('value-range')
    if (isinstance(limits, list) and len(limits) == 3 and all(type(v) is int for v in limits)
            and limits[:2] == [0, 100] and 1 <= limits[2] <= 100 and 100 % limits[2] == 0):
        return limits[2]
    return None


def cover_binding(spec, source, entity, device, capabilities):
    urn, model = source.catalog['urn'], source.catalog.get('model')
    unique = source.registry['unique_id']
    prefix = registry_prefix(spec, 'cover', urn, model)
    family_kind = urn.split(':')[2:4]
    if prefix is None or family_kind != ['device', 'curtain']:
        return None
    match = re.fullmatch(prefix + r'(?:_s_(\d+)(?:_[^\r\n]{0,128})?)?', unique)
    if match is None or xiaomi_identifier(match[1]) != source.identity:
        return None
    properties(spec)
    services = [s for s in spec['services']
                if s['type'].split(':')[2:4] == ['service', 'curtain']
                and (match[2] is None or s['iid'] == int(match[2]))]
    if len(services) != 1:
        return None
    service, = services
    props = properties({'services': [service]})
    current = props.get('current-position')
    position = current['piid'] if _position_step(current, 'read') is not None else None
    # 存在却无法核对的行程读数可能是叶片角度。没有这个属性的设备才走目标值回报。
    if 'current-position' in props and position is None:
        return None
    fault = props.get('fault')
    if fault is None or 'read' not in fault['access'] or fault.get('format') not in INTEGER_FORMATS:
        return None
    if 'value-list' in fault:
        clear = _codes(fault, NO_FAULT, 'read')
        if clear.get('clear') != 0:
            return None
    caps = {c.name: c for c in capabilities}
    target = props.get('target-position')
    step = _position_step(target, 'write') if 'cover.position' in caps else None
    motor = props.get('motor-control')
    allowed = caps['cover.state'].choices if 'cover.state' in caps else ()
    commands = {k: v for k, v in _codes(motor, MOTOR_COMMANDS, 'write').items() if k in allowed}
    if not {'open', 'closed'} <= commands.keys():
        commands = {}
    status = props.get('status')
    motion = _codes(status, MOTION_STATES, 'read')
    target_readable = _position_step(target, 'read') is not None
    if not (step or commands) or (position is None and not (target_readable and motion)):
        return None
    return CoverTarget(
        entity=entity.model_copy(update={'capabilities': [], 'state_version': None}),
        device=device.model_copy(update={'capabilities': [], 'function_ids': [], 'limitations': []}),
        identity=source.identity, model=model, urn=urn, registry_uid=unique, siid=service['iid'],
        position_piid=position, target_piid=target['piid'] if step or target_readable else None,
        target_readable=target_readable, target_writable=step is not None, step=step or 1,
        motor_piid=motor['piid'] if commands else None, commands=commands,
        status_piid=status['piid'] if motion else None, motion=motion,
        fault_piid=fault['piid'],
    )


@dataclass(frozen=True)
class CoverReading:
    position: int | None
    motion: str | None
    fault: int
    target_position: int | None

    @property
    def healthy(self):
        return self.fault == 0

    @property
    def state(self):
        if not self.healthy:
            return 'unavailable'
        if self.motion in ('opening', 'closing'):
            return self.motion
        if self.position is None:
            return 'stopped' if self.motion == 'stopped' else 'position_unavailable'
        return 'open' if self.position > 0 else 'closed'


@dataclass(frozen=True)
class CoverWriter:
    target: CoverTarget
    request: Callable[[dict], Awaitable[dict]]
    current: Callable[[], bool]
    record: Callable[[CoverReading], None]
    family: str
    operation: str | None = None
    desired: dict | None = None
    channel: str = 'lan'

    def fingerprint(self):
        return hashlib.sha256(repr((self.family, self.target.reference, self.operation)).encode()).hexdigest()

    async def send(self, desired):
        key, = desired
        result = await self.request({'operation': self.operation, 'value': desired[key]})
        return 'accepted' if result.get('accepted') is True else 'service_outcome_unknown'

    async def preflight(self):
        # 停止仍经故障检查。运动中的新位置不应使已经发起的停止计划失效。
        return await self.readback(record=self.desired != {'state': 'stopped'})

    async def readback(self, *, record=True):
        result = await self.request({})
        position, motion, fault, target_position = (
            result.get(key) for key in ('position', 'motion', 'fault', 'target_position'))
        if (not _percent_value(position, required=self.target.position_piid is not None)
                or not _percent_value(target_position, required=self.target.target_readable)
                or motion is not None and not any(motion == state for state in self.target.motion)
                or type(fault) is not int or not 0 <= fault <= 4294967295):
            raise ValueError('mijia_invalid_readback')
        reading = CoverReading(position, motion, fault, target_position)
        if record or not reading.healthy:
            self.record(reading)
        if not reading.healthy:
            raise ValueError('mijia_cover_fault')
        return {'position': position, 'target_position': target_position, 'state': motion or reading.state, 'fault': fault,
                'received_at': datetime.now(UTC).isoformat(), 'evidence': 'mijia_lan_readback'}

    def confirms_arrival(self, desired):
        if desired == {'state': 'stopped'}:
            return 'stopped' in self.target.motion
        return self.target.position_piid is not None

    @staticmethod
    def matches(desired, reported):
        state = desired.get('state')
        if state == 'stopped':
            return reported.get('state') == 'stopped'
        if reported.get('state') in ('opening', 'closing'):
            return False
        position = {'open': 100, 'closed': 0}.get(state, desired.get('position'))
        return type(position) is int and reported.get('position') == position


def _percent_value(value, *, required):
    return (type(value) is int and 0 <= value <= 100) if required else value is None


def authorized_cover(directory, family, identity, request):
    write = 'operation' in request
    try:
        catalog = read_cover_catalog(directory)
        authority, version = read_device_authority(directory / 'action-authority.json',
                                                   include_inactive=not write, include_generation=True)
        if (not isinstance(authority, HomeDeviceAuthority) or catalog.family != family
                or catalog.legacy_approval != cover_approval(authority)):
            raise ValueError('mijia_permission_changed')
        target = next(t for t in catalog.targets if t.identity == identity
                      and cover_digest(t) == request['registered_cover'])
        try:
            reserved = reserved_covers(configuration(directory / 'config.json'))
        except FileNotFoundError:
            reserved = set()
        allowed = target.operations.intersection(authority.operations)
        if identity in reserved or not allowed or (authority.external_entity_ids is not None
                           and bindings(target.entity)[0] not in authority.external_entity_ids):
            raise ValueError('mijia_permission_changed')
        if write:
            deadline = request['deadline']
            remaining = deadline - time.monotonic() if type(deadline) in (int, float) else -1
            if (request['operation'] not in allowed or request['authority_revision'] != version
                    or not math.isfinite(remaining) or not 0 < remaining <= 60):
                raise ValueError('mijia_permission_changed')
            target.wire(request['operation'], request['value'])
        return target
    except (OSError, TypeError, ValueError, KeyError, StopIteration):
        raise ValueError('mijia_permission_changed') from None


async def registered_cover_operation(manager, request, grant, family, region, group, dids, current, directory):
    from .embodied.mijia_lan import local_result

    write = 'operation' in request
    keys = {'registered_cover', 'did', 'grant'}
    if write:
        keys |= {'operation', 'value', 'authority_revision', 'deadline'}
    if request.keys() != keys or not isinstance(request.get('registered_cover'), str):
        raise ValueError('mijia_property_unsupported')
    did = request['did']

    def routed():
        return (current() and request['grant'] == grant and did in dids and manager.can_control(did)
                and not manager.in_local_cooldown(did) and (manager.local_device(did) or {}).get('group_id') == group)

    if not isinstance(did, str) or not routed():
        raise ValueError('mijia_lan_unavailable')
    identity = f'{region}_{did}'
    target = authorized_cover(directory, family, identity, request)

    async def read(piid):
        if piid is None:
            return None
        row = await manager.get_prop_async(did, target.siid, piid)
        if (not isinstance(row, dict) or type(row.get('code')) is not int or row['code'] != 0
                or type(row.get('value')) is not int or str(row.get('did', did)) != did
                or row.get('siid', target.siid) != target.siid or row.get('piid', piid) != piid):
            raise ValueError('mijia_property_unsupported')
        return row['value']

    try:
        if not write:
            try:
                async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
                    position = tasks.create_task(read(target.position_piid))
                    motion = tasks.create_task(read(target.status_piid))
                    fault = tasks.create_task(read(target.fault_piid))
                    target_position = tasks.create_task(read(target.target_piid if target.target_readable else None))
            except ExceptionGroup as error:
                raise ValueError('mijia_property_unsupported') from error
            if (not _percent_value(position.result(), required=target.position_piid is not None)
                    or not _percent_value(target_position.result(), required=target.target_readable)):
                raise ValueError('mijia_property_unsupported')
            if (not routed() or authorized_cover(directory, family, identity, request).reference != target.reference):
                raise ValueError('mijia_permission_changed')
            state = next((name for name, code in target.motion.items() if code == motion.result()), None)
            return {'position': position.result(), 'motion': state, 'fault': fault.result(),
                    'target_position': target_position.result()}
        deadline = min(4., request['deadline'] - time.monotonic())
        if deadline <= 0:
            raise ValueError('mijia_permission_changed')
        async with asyncio.timeout(deadline):
            # 检查故障和授权后只写一次。未知结果由上层只读对账。
            if await read(target.fault_piid) != 0 or not routed():
                raise ValueError('mijia_permission_changed')
            updated = authorized_cover(directory, family, identity, request)
            if updated.reference != target.reference:
                raise ValueError('mijia_permission_changed')
            piid, value = updated.wire(request['operation'], request['value'])
            row = await manager.set_prop_async(did, target.siid, piid, value)
    except TimeoutError:
        manager.note_local_failure(did)
        raise
    return local_result(row, did, target.siid, piid, write=True)
