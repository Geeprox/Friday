"""在当前米家家庭中登记有明确电源属性的设备。"""

import asyncio
import math
import time
from dataclasses import dataclass
from uuid import uuid4

from .contracts import Binding, Entity, PhysicalDevice
from .devices import _digest, xiaomi_identifier
from .embodied.miot_spec import properties
from .local_camera_home import LocalCameraHome
from .mijia_control import POWER_BY_KIND
from .rule_compiler import digest


KINDS = {
    'switch': ('switch', {'switch'}),
    'outlet': ('switch', {'switch', 'outlet'}),
    'light': ('light', {'light'}),
    'fan': ('fan', {'fan'}),
}
MODE_PROPERTIES = {'mode', 'switch-mode', 'wireless-mode', 'relay-mode', 'operation-mode'}


def miot_name(urn, category):
    parts = urn.split(':') if isinstance(urn, str) else []
    return parts[3] if len(parts) >= 5 and parts[0] == 'urn' and parts[2] == category else None


def power_ports(urn, spec):
    category = KINDS.get(miot_name(urn, 'device'))
    if category is None or not isinstance(spec, dict) or spec.get('type') != urn:
        return None
    kind, services = category
    properties(spec)
    ports = []
    for service in spec['services']:
        names = [miot_name(p['type'], 'property') for p in service.get('properties', [])]
        if kind == 'switch' and MODE_PROPERTIES.intersection(names):
            return None
        if miot_name(service.get('type'), 'service') not in services:
            continue
        on = [prop for prop, name in zip(service.get('properties', []), names) if name == 'on']
        if not on:
            continue
        if len(on) != 1 or on[0].get('format') != 'bool' or not {'read', 'write'} <= set(on[0]['access']):
            return None
        ports.append((service['iid'], on[0]['iid']))
    return (kind, sorted(ports)) if 1 <= len(ports) <= 8 else None


@dataclass(frozen=True)
class EnrollmentCandidate:
    identity: str
    name: str
    room: str
    urn: str
    model: str
    context: tuple[str, str, str]

    @property
    def revision(self):
        return digest([self.identity, self.name, self.room, self.urn, self.model, self.context])

    @property
    def device_id(self):
        return 'device:xiaomi:' + _digest(self.identity)

    @property
    def did(self):
        return self.identity.split('_', 1)[1]

    def targets(self, kind, ports):
        from .mijia_power_home import PowerTarget

        physical = Binding(source='xiaomi-identity', external_id=self.identity)
        device = PhysicalDevice(id=self.device_id, name=self.name, room=self.room, kind=kind,
                                function_ids=[], bindings=[physical], identity_basis='xiaomi_identifier')
        result = []
        for index, (siid, piid) in enumerate(ports, 1):
            reference = digest([self.context[1], self.identity, siid, piid])
            entity = Entity(id='mijia:property:' + reference, name=self.name if len(ports) == 1 else f'{self.name} 通道 {index}',
                            room=self.room, kind=kind, physical_source_id=device.id,
                            bindings=[Binding(source='mijia-property', external_id=reference)])
            result.append(PowerTarget(entity=entity, device=device, identity=self.identity, model=self.model, urn=self.urn,
                                      registry_uid=reference, siid=siid, piid=piid, operation=POWER_BY_KIND[kind]))
        return result


def enrollment_candidates(home, context=None):
    if not isinstance(home.core.adapter, LocalCameraHome) or not home.core.actions._owner_owned:
        return {}
    context = context if context is not None else home.discovery_context()
    if context is None:
        return {}
    rows = home.power.service.discovery_report.get('devices', ())
    if not isinstance(rows, list):
        return {}
    models = context[3]
    existing = home.enrollment_identities(context)
    devices = {}
    for row in rows:
        identity = xiaomi_identifier(row.get('identity'))
        if identity is None or identity in existing or row.get('kind') != 'device':
            continue
        entry = models.get(identity) or {}
        urn, model = entry.get('urn'), entry.get('model')
        name, room = row.get('name'), row.get('room')
        if (miot_name(urn, 'device') not in KINDS or not isinstance(model, str) or not 1 <= len(model) <= 96
                or not isinstance(name, str) or not 1 <= len(name) <= 80 or not isinstance(room, str) or len(room) > 80):
            continue
        devices[row['id']] = EnrollmentCandidate(identity, name, room, urn, model, context[:3])
    return devices


def native_fingerprint(context, target):
    return digest(['mijia-enrolled-property/1', context, target.reference])


async def enroll(home, identifier, revision):
    from .mijia_power_home import MAX_TARGETS, NativeDeviceCatalog

    candidate = enrollment_candidates(home).get(identifier)
    if candidate is None or candidate.revision != revision:
        raise ValueError('mijia_enrollment_changed')
    family, household, grant = candidate.context
    if not home.power.lan.ready(family, candidate.did):
        raise ValueError('mijia_enrollment_offline')

    async with asyncio.timeout(15):
        spec = await home.power._spec(candidate.urn)
        match = power_ports(candidate.urn, spec)
        if match is None:
            raise ValueError('mijia_enrollment_unsupported')
        targets = candidate.targets(*match)
        readings = {}
        for target in targets:
            row = await home.power.lan.request(
                {'did': candidate.did, 'siid': target.siid, 'piid': target.piid, 'grant': grant}, family=family
            )
            if row.get('power') not in ('on', 'off'):
                raise ValueError('mijia_enrollment_offline')
            readings[target.entity.id] = row['power']

    async with home.core.actions._lock:
        if candidate != enrollment_candidates(home).get(identifier) or not home.power.lan.ready(family, candidate.did):
            raise ValueError('mijia_enrollment_changed')
        previous, _ = home.native_catalog()
        if previous is None:
            legacy, _ = home.core.actions.legacy_authority(include_inactive=True)
            previous = NativeDeviceCatalog(family=family, household=household, epoch=uuid4().hex,
                                           enabled=legacy is None or legacy.enabled, targets=[])
        updated_targets = [t for t in previous.targets if t.identity != candidate.identity] + targets
        if len(updated_targets) > MAX_TARGETS:
            raise ValueError('mijia_enrollment_limit')
        updated = NativeDeviceCatalog(family=family, household=household, epoch=previous.epoch,
                                     enabled=previous.enabled, targets=sorted(updated_targets, key=lambda t: t.entity.id))
        if updated != previous:
            home.save_native_catalog(updated)
        context = (family, grant)
        for target in targets:
            home.record((context, target.reference), readings[target.entity.id], lambda: True)
        return candidate.device_id


async def native_power_operation(manager, request, grant, family, region, group, dids, current, directory):
    from .embodied.mijia_lan import property_operation
    from .embodied.mijia_power import property_request
    from .mijia_power_home import read_native_catalog

    payload = dict(request)
    fingerprint = payload.pop('native_power')
    expected, deadline = payload.pop('authority_revision', None), payload.pop('deadline', None)
    did, siid, piid, desired = property_request(payload)
    write = desired is not None

    def qualified():
        if not current():
            return False
        if write:
            remaining = deadline - time.monotonic() if type(deadline) in (int, float) else -1
            if not math.isfinite(remaining) or not 0 < remaining <= 60:
                return False
        try:
            catalog, revision = read_native_catalog(directory)
        except (OSError, ValueError):
            return False
        if catalog.family != family or (write and (not catalog.enabled or revision != expected)):
            return False
        key = (f'{region}_{did}', siid, piid)
        return any(t.property_key == key and native_fingerprint((family, grant), t) == fingerprint
                   for t in catalog.targets)

    if not qualified():
        raise ValueError('mijia_permission_changed')
    return await property_operation(manager, payload, grant, group, dids, qualified)
