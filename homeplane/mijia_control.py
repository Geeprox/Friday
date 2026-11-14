"""用米家家庭目录及公开规格为现有设备计划选择直连通道。"""

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from .devices import xiaomi_identifier


async def request_property(request):
    from .embodied.paths import DATA, PROJECT, PYTHON

    process = await asyncio.create_subprocess_exec(
        str(PYTHON), '-B', '-m', 'homeplane.embodied.mijia_power', cwd=PROJECT,
        env=dict(os.environ, PYTHONPATH=str(PROJECT), FRIDAY_EMBODIED_DATA=str(DATA)),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        raw, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), 8)
        if process.returncode or len(raw) > 2048:
            raise ValueError('mijia_property_unconfirmed')
        result = json.loads(raw)
        if not isinstance(result, dict) or 'error' in result:
            raise ValueError('mijia_property_unconfirmed')
        return result
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def power_property(spec, unique_id, kind, device_urn):
    """按 Xiaomi Home 的属性或服务注册标识精确绑定，不解析用户改过的实体名。"""
    from .embodied.miot_spec import properties

    parts = device_urn.split(':') if isinstance(device_urn, str) else []
    model = parts[5].split('.') if len(parts) >= 6 else []
    if len(model) < 3 or spec.get('type') != device_urn:
        return None
    vendor, product = re.escape(model[0][:9]), re.escape(model[-1][:20])
    matches = re.fullmatch(rf'(?:xiaomi_home|{re.escape(kind)})\.{vendor}_(.+)_{product}_(?:on_p_(\d+)_(\d+)|s_(\d+)(?:_[a-z0-9_-]+)?)',
                           unique_id)
    if not matches:
        return None
    identity, property_siid, property_iid, service_siid = matches.groups()
    siid = int(property_siid or service_siid)
    properties(spec)
    relevant = [service for service in spec['services'] if service.get('iid') == siid]
    if len(relevant) != 1:
        return None
    service = relevant[0]
    allowed_services = {'light': {'light'}, 'switch': {'switch', 'outlet'}, 'fan': {'fan'},
                        'climate': {'air-conditioner', 'heater', 'thermostat'},
                        'humidifier': {'humidifier', 'dehumidifier'}, 'water_heater': {'water-heater'}}
    service_urn = service.get('type', '').split(':')
    if len(service_urn) < 4 or service_urn[2] != 'service' or service_urn[3] not in allowed_services.get(kind, set()):
        return None
    on = [row for row in service['properties'] if row['type'].split(':')[3:4] == ['on']
          and (property_iid is None or int(property_iid) == row['iid'])]
    if len(on) == 1 and on[0].get('format') == 'bool' and {'read', 'write'} <= set(on[0]['access']):
        return xiaomi_identifier(identity), siid, on[0]['iid']
    return None


POWER_OPERATIONS = frozenset({'light.set', 'switch.set', 'fan.set', 'climate.power', 'humidifier.power', 'water_heater.power'})


class MijiaPower:
    """直连只接管已有授权的确定性电源操作，使用原有实体 ID。"""

    def __init__(self, service, *, spec=None, request=request_property):
        if spec is None:
            from .embodied.mijia_inventory import public_spec
            spec = public_spec
        self.service, self.spec, self.request = service, spec, request
        self.specs = {}

    async def writer(self, adapter, snapshot, entity, operation, desired):
        if (operation not in POWER_OPERATIONS or operation.split('.', 1)[0] != entity.kind
                or set(desired) != {'power'} or desired['power'] not in {'on', 'off'}):
            return None
        report = getattr(self.service, "discovery_report", {})
        updated_at = getattr(self.service, "discovery_checked_at", None)
        if (report.get('state') != 'ready' or updated_at is None
                or not 0 <= time.monotonic()-updated_at <= 330):
            return None
        registry_ids = [b.external_id for b in entity.bindings if b.source == 'ha-device-registry']
        external = next((b.external_id for b in entity.bindings if b.source == 'ha'), None)
        row = dict(adapter._registry.get(external, {}))
        if (registry_ids != [row.get('device_id')] or row.get('platform') != 'xiaomi_home'
                or not isinstance(row.get('unique_id'), str)):
            return None
        ids = {b.external_id for device in snapshot.devices if entity.id in device.function_ids
               for b in device.bindings if b.source == 'xiaomi-identity'}
        models = report.get('models', {})
        grant = report.get('grant')
        if len(ids) != 1 or not isinstance(grant, str) or not re.fullmatch(r'[a-f0-9]{64}', grant):
            return None
        identity = next(iter(ids))
        urn = models.get(identity)
        if not isinstance(urn, str):
            return None
        try:
            if urn not in self.specs:
                self.specs[urn] = await self.spec(urn)
                if len(self.specs) > 128:
                    self.specs.pop(next(iter(self.specs)))
            match = power_property(self.specs[urn], row['unique_id'], entity.kind, urn)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None
        def current():
            refreshed = self.service.discovery_report
            device = adapter._devices.get(row['device_id'], {})
            return (refreshed.get('state') == 'ready' and refreshed.get('grant') == grant
                    and refreshed.get('models', {}).get(identity) == urn and adapter._registry.get(external) == row
                    and any(namespace == 'xiaomi_home' and xiaomi_identifier(value) == identity
                            for entry in device.get('identifiers', []) if isinstance(entry, (list, tuple)) and len(entry) == 2
                            for namespace, value in [entry]))

        if not match or match[0] != identity or not current():
            return None
        _, siid, piid = match
        return MijiaPowerWriter(self.request, {'did': identity.split('_', 1)[1], 'siid': siid,
                                               'piid': piid, 'grant': grant}, current)


@dataclass(frozen=True)
class MijiaPowerWriter:
    request: object

    binding: dict
    current: Callable[[], bool]

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()

    async def send(self, desired):
        result = await self.request({**self.binding, **desired})
        return 'accepted' if result.get('accepted') is True else 'service_outcome_unknown'

    async def readback(self):
        result = await self.request(self.binding)
        if result.get('power') not in ('on', 'off'):
            raise ValueError('unavailable_readback')
        return dict(power=result['power'], received_at=datetime.now(UTC).isoformat(), evidence='mijia_property_readback')
