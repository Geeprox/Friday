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

import httpx

from .devices import xiaomi_identifier
from .mijia_lan_client import FAMILY, MijiaLanClient


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


def power_property(spec, unique_id, kind, device_urn, device_model):
    """按 Xiaomi Home 的属性或服务注册标识精确绑定，不解析用户改过的实体名。"""
    from .embodied.miot_spec import properties

    model = device_model.split('.') if isinstance(device_model, str) else []
    if (len(model) < 2 or not all(re.fullmatch(r'[a-z0-9-]{1,40}', part) for part in model)
            or spec.get('type') != device_urn):
        return None
    vendor, product = re.escape(model[0][:9]), re.escape(model[-1][:20])
    prefix = rf'(?:xiaomi_home|{re.escape(kind)})\.{vendor}_([a-z]{{2}}_(?:\d{{1,32}}|blt[._]\d[._][a-z0-9]{{1,64}}))_{product}'
    # 规格 URN 仅确定型号的能力；厂商和型号段来自米家目录独立的 model 字段。
    # 支持注册表中的设备、服务和 on 属性；服务描述是显示文本，不参与绑定。
    matches = re.fullmatch(prefix + r'(?:_on_p_(\d+)_(\d+)|_s_(\d+)(?:_[^\r\n]{0,128})?)?', unique_id)
    if not matches:
        return None
    identity, property_siid, property_iid, service_siid = matches.groups()
    siid = int(property_siid or service_siid) if (property_siid or service_siid) else None
    properties(spec)
    allowed_services = {'light': {'light'}, 'switch': {'switch', 'outlet'}, 'fan': {'fan'},
                        'climate': {'air-conditioner', 'heater', 'thermostat'},
                        'humidifier': {'humidifier', 'dehumidifier'}, 'water_heater': {'water-heater'}}
    urn_parts = device_urn.split(':')
    device_kind = urn_parts[3] if len(urn_parts) > 3 and urn_parts[2] == 'device' else None
    expected = allowed_services.get(kind, set())
    if siid is None and device_kind not in expected:
        return None
    candidates = []
    for service in spec['services']:
        if siid is not None and service['iid'] != siid:
            continue
        service_urn = service.get('type', '').split(':')
        if len(service_urn) < 4 or service_urn[2] != 'service' or service_urn[3] not in expected:
            continue
        for row in service.get('properties', []):
            if (row['type'].split(':')[2:4] == ['property', 'on']
                    and (property_iid is None or int(property_iid) == row['iid'])):
                candidates.append((service['iid'], row))
    if len(candidates) != 1:
        return None
    service_id, prop = candidates[0]
    if prop.get('format') == 'bool' and {'read', 'write'} <= set(prop['access']):
        return xiaomi_identifier(identity), service_id, prop['iid']
    return None


POWER_OPERATIONS = frozenset({'light.set', 'switch.set', 'fan.set', 'climate.power', 'humidifier.power', 'water_heater.power'})


class MijiaPower:
    """直连只接管已有授权的确定性电源操作，使用原有实体 ID。"""

    def __init__(self, service, *, spec=None, request=request_property, lan=None):
        if spec is None:
            from .embodied.mijia_inventory import public_spec
            spec = public_spec
        self.service, self.spec, self.request = service, spec, request
        self.lan = lan if lan is not None else MijiaLanClient(self.current_family)
        self.specs = {}
        self.warming = None

    def current_family(self):
        report = getattr(self.service, 'discovery_report', {})
        checked = getattr(self.service, 'discovery_checked_at', None)
        family = report.get('family')
        if (report.get('state') == 'ready' and checked is not None
                and 0 <= time.monotonic() - checked <= 330
                and isinstance(family, str) and FAMILY.fullmatch(family)):
            return family
        return None

    def start(self, core):
        self.lan.start()
        self.warming = asyncio.create_task(self._warm(core), name='friday-mijia-specs')

    async def close(self):
        if self.warming is not None:
            self.warming.cancel()
            await asyncio.gather(self.warming, return_exceptions=True)
            self.warming = None
        await self.lan.close()

    async def _spec(self, urn):
        if urn not in self.specs:
            self.specs[urn] = await self.spec(urn)
            if len(self.specs) > 128:
                self.specs.pop(next(iter(self.specs)))
        return self.specs[urn]

    async def _warm(self, core):
        # 只预取家庭已匹配设备的公开规格；不会读取或改变设备属性。
        retry = {}
        limit = asyncio.Semaphore(3)

        async def read(urn):
            try:
                async with limit, asyncio.timeout(6):
                    await self._spec(urn)
            except (OSError, ValueError, TimeoutError, httpx.HTTPError):
                retry[urn] = time.monotonic() + 60

        while True:
            family = self.current_family()
            snapshot = getattr(core.adapter, '_last', None)
            if family and snapshot is not None and self.lan.ready(family):
                models = self.service.discovery_report.get('models', {})
                identifiers = {binding.external_id for device in snapshot.devices
                               for binding in device.bindings if binding.source == 'xiaomi-identity'}
                now = time.monotonic()
                retry = {urn: until for urn, until in retry.items() if until > now}
                urns = {row['urn'] for identity in identifiers
                        if (stable := xiaomi_identifier(identity)) and isinstance(row := models.get(stable), dict)
                        and isinstance(row.get('urn'), str) and self.lan.ready(family, stable.split('_', 1)[1])}
                fresh = sorted(urns - self.specs.keys() - retry.keys())[:24]
                await asyncio.gather(*(read(urn) for urn in fresh))
            await asyncio.sleep(5)

    @staticmethod
    def state_agrees(snapshot, entity, observed):
        name = f'{entity.id}:{entity.kind}.state'
        value = next((state.value for state in snapshot.states if state.id == name), None)
        if value == 'off':
            return observed['power'] == 'off'
        if value == 'on' or (entity.kind == 'climate' and value in {
            'heat', 'cool', 'auto', 'dry', 'heat_cool', 'fan_only',
        }):
            return observed['power'] == 'on'
        return True

    async def writer(self, adapter, snapshot, entity, operation, desired, *, channel=None, quick=False):
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
        catalog = models.get(identity)
        if not isinstance(catalog, dict) or not isinstance(catalog.get('urn'), str):
            return None
        urn = catalog['urn']
        did = identity.split('_', 1)[1]
        family = self.current_family()
        local = bool(family and channel != 'cloud' and self.lan.ready(family, did))
        if (channel == 'lan' and not local) or (quick and (not local or urn not in self.specs)):
            return None
        try:
            spec = await self._spec(urn)
            match = power_property(spec, row['unique_id'], entity.kind, urn, catalog.get('model'))
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None
        def current():
            refreshed = self.service.discovery_report
            device = adapter._devices.get(row['device_id'], {})
            return (refreshed.get('state') == 'ready' and refreshed.get('grant') == grant
                    and refreshed.get('models', {}).get(identity) == catalog and adapter._registry.get(external) == row
                    and any(namespace == 'xiaomi_home' and xiaomi_identifier(value) == identity
                            for entry in device.get('identifiers', []) if isinstance(entry, (list, tuple)) and len(entry) == 2
                            for namespace, value in [entry]))

        if not match or match[0] != identity or not current():
            return None
        _, siid, piid = match
        binding = {'did': did, 'siid': siid, 'piid': piid, 'grant': grant}
        if not local:
            return MijiaPowerWriter(self.request, binding, current)

        async def request_lan(payload):
            return await self.lan.request(payload, family=family)

        def local_current():
            return current() and self.lan.ready(family, did)

        return MijiaPowerWriter(request_lan, binding, local_current, channel='lan', family=family)


@dataclass(frozen=True)
class MijiaPowerWriter:
    request: object

    binding: dict
    current: Callable[[], bool]
    channel: str = 'cloud'
    family: str | None = None

    def fingerprint(self):
        # 保持旧版云端回执的核对方式。局域网回执绑定同一家庭和通道。
        payload = self.binding if self.channel == 'cloud' else {
            **self.binding, 'channel': self.channel, 'family': self.family,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def send(self, desired):
        result = await self.request({**self.binding, **desired})
        return 'accepted' if result.get('accepted') is True else 'service_outcome_unknown'

    async def readback(self):
        result = await self.request(self.binding)
        if result.get('power') not in ('on', 'off'):
            raise ValueError('unavailable_readback')
        source = 'mijia_lan_readback' if self.channel == 'lan' else 'mijia_property_readback'
        return dict(power=result['power'], received_at=datetime.now(UTC).isoformat(), evidence=source)
