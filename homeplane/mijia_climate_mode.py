"""已有米家空调的模式绑定与本地中枢执行。"""

import asyncio
import hashlib
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, ClassVar, Literal

from pydantic import Field, StrictInt, model_validator

from .action_contracts import HomeDeviceAuthority
from .actions import _read_authority_file, cover_approval, read_device_authority
from .contracts import Capability, Contract
from .mijia_lan_client import FAMILY
from .mijia_numeric import INTEGER_FORMATS


CLIMATE_MODE_CATALOG = 'mijia-climate-mode-catalog.json'
MODE_OPERATION = 'climate.mode'
Mode = Literal['auto', 'cool', 'dry', 'fan_only', 'heat', 'heat_cool']
MODE_NAMES = dict(auto='auto', cool='cool', dry='dry', fan='fan_only', heat='heat', heat_cool='heat_cool')


class ClimateMode(Contract):
    binding: str = Field(pattern=FAMILY.pattern + '$')
    piid: int = Field(strict=True, ge=1, le=65535)
    modes: dict[Mode, StrictInt] = Field(min_length=1, max_length=6)
    key: ClassVar[str] = 'hvac_mode'
    operation: ClassVar[str] = MODE_OPERATION

    @model_validator(mode='after')
    def distinct_modes(self):
        if (len(set(self.modes.values())) != len(self.modes)
                or any(not 0 <= value <= 65535 for value in self.modes.values())):
            raise ValueError('mijia_invalid_climate_modes')
        return self

    def capability(self):
        return Capability(name=self.operation, access='write', support='supported',
                          choices=['off', *self.modes])

    def wire_value(self, value):
        if value == 'off':
            return None
        if not isinstance(value, str) or value not in self.modes:
            raise ValueError('mijia_invalid_climate_mode')
        return self.modes[value]


class ClimateModeCatalog(Contract):
    family: str = Field(pattern=FAMILY.pattern + '$')
    legacy_approval: str = Field(pattern=FAMILY.pattern + '$')
    devices: dict[str, ClimateMode] = Field(max_length=512)


def read_climate_catalog(directory):
    body, _ = _read_authority_file(directory / CLIMATE_MODE_CATALOG, limit=256 * 1024)
    return ClimateModeCatalog.model_validate_json(body)


def climate_mode_binding(spec, target, capabilities, binding):
    if target.entity.kind != 'climate':
        return None
    cap = next((cap for cap in capabilities if cap.name == MODE_OPERATION), None)
    if cap is None or 'off' not in (cap.choices or []):
        return None
    service = next(row for row in spec['services'] if row['iid'] == target.siid)
    if service['type'].split(':')[2:4] != ['service', 'air-conditioner']:
        return None
    props = [p for p in service['properties'] if p['type'].split(':')[2:4] == ['property', 'mode']]
    if len(props) != 1:
        return None
    prop = props[0]
    items = prop.get('value-list')
    if (prop['iid'] == target.piid or prop.get('format') not in INTEGER_FORMATS
            or not {'read', 'write'} <= set(prop['access'])
            or not isinstance(items, list) or not 1 <= len(items) <= 32):
        return None
    modes, values = {}, set()
    for row in items:
        if not isinstance(row, dict):
            return None
        # 标准规格可提供 name，较早版本以英文 description 表示相同枚举。
        name = row.get('name', row.get('description'))
        value = row.get('value')
        if not isinstance(name, str) or type(value) is not int or value in values:
            return None
        values.add(value)
        mode = MODE_NAMES.get(name.strip().lower().replace(' ', '_'))
        if mode is None or mode not in cap.choices:
            continue
        if mode in modes:
            return None
        modes[mode] = value
    return ClimateMode(binding=binding, piid=prop['iid'], modes=dict(sorted(modes.items()))) if modes else None


def mode_revision(setting):
    return hashlib.sha256(setting.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class ClimateModeWriter:
    setting: ClimateMode
    request: Callable[[dict], Awaitable[dict]]
    current: Callable[[], bool]
    record: Callable[[str, str | None], None]
    invalidate: Callable[[], None]
    channel: str = 'lan'

    def fingerprint(self):
        return mode_revision(self.setting)

    async def send(self, desired):
        mode = desired[self.setting.key]
        self.setting.wire_value(mode)
        try:
            result = await self.request({self.setting.key: mode})
        finally:
            # 开机及切换模式是两个属性，失败后先撤回先前的状态，再等新读数。
            self.invalidate()
        return 'accepted' if result.get('accepted') is True else 'service_outcome_unknown'

    async def readback(self):
        row = await self.request({})
        power, mode = row.get('power'), row.get(self.setting.key)
        if (power == 'off' and mode != 'off' or power == 'on' and mode not in (None, *self.setting.modes)
                or power not in ('on', 'off')):
            raise ValueError('mijia_invalid_readback')
        self.record(power, mode)
        return {'power': power, self.setting.key: mode, 'received_at': datetime.now(UTC).isoformat(),
                'evidence': 'mijia_lan_readback'}

    def matches(self, desired, reported):
        mode = desired[self.setting.key]
        return reported.get(self.setting.key) == mode and reported.get('power') == ('off' if mode == 'off' else 'on')


def authorized_mode(directory, family, identity, request):
    from .mijia_power_home import bindings, read_catalog, target_digest

    write = 'hvac_mode' in request
    authority, revision = read_device_authority(directory / 'action-authority.json',
                                                include_inactive=not write, include_generation=True)
    if not isinstance(authority, HomeDeviceAuthority) or MODE_OPERATION not in authority.operations:
        raise ValueError('mijia_permission_changed')
    if write:
        deadline = request.get('deadline')
        remaining = deadline - time.monotonic() if type(deadline) in (int, float) else -1
        if not math.isfinite(remaining) or not 0 < remaining <= 60 or revision != request['authority_revision']:
            raise ValueError('mijia_permission_changed')
    try:
        power = read_catalog(directory)
        modes = read_climate_catalog(directory)
    except (OSError, ValueError):
        raise ValueError('mijia_permission_changed') from None
    if (modes.family, modes.legacy_approval) != (power.family, power.legacy_approval) or (
        power.family, power.legacy_approval
    ) != (family, cover_approval(authority)):
        raise ValueError('mijia_permission_changed')
    matches = []
    for target in power.targets:
        setting = modes.devices.get(target.entity.id)
        if (target.identity == identity and target.entity.kind == 'climate'
                and setting is not None and setting.binding == target_digest(target) and setting.piid != target.piid
                and mode_revision(setting) == request['registered_climate_mode']
                and (authority.external_entity_ids is None or bindings(target.entity)[0] in authority.external_entity_ids)):
            matches.append((target, setting))
    if len(matches) != 1:
        raise ValueError('mijia_permission_changed')
    return matches[0]


async def climate_mode_operation(manager, request, grant, family, region, group, dids, current, directory):
    from .embodied.mijia_lan import local_result

    write = 'hvac_mode' in request
    keys = {'registered_climate_mode', 'did', 'grant'}
    if write:
        keys |= {'hvac_mode', 'authority_revision', 'deadline'}
    did = request.get('did')
    if request.keys() != keys or not isinstance(did, str) or did not in dids or request['grant'] != grant:
        raise ValueError('mijia_property_unsupported')
    identity = f'{region}_{did}'

    def qualified():
        if (not current() or not manager.can_control(did) or manager.in_local_cooldown(did)
                or (manager.local_device(did) or {}).get('group_id') != group):
            raise ValueError('mijia_lan_unavailable')
        return authorized_mode(directory, family, identity, request)

    target, setting = qualified()

    async def read_power():
        row = await manager.get_prop_async(did, target.siid, target.piid)
        if not confirmed_read(row, did, target.siid, target.piid):
            raise ValueError('mijia_property_unconfirmed')
        return local_result(row, did, target.siid, target.piid, write=False)['power']

    async def send(piid, value):
        qualified()
        row = await manager.set_prop_async(did, target.siid, piid, value)
        local_result(row, did, target.siid, piid, write=True)

    try:
        async with asyncio.timeout(4 if write else 1):
            mode = request.get('hvac_mode')
            wire = setting.wire_value(mode) if write else None
            power = await read_power()
            if not write:
                if power == 'off':
                    qualified()
                    return {'power': power, 'hvac_mode': 'off'}
                row = await manager.get_prop_async(did, target.siid, setting.piid)
                qualified()
                value = row.get('value') if confirmed_read(row, did, target.siid, setting.piid) else None
                observed = next((key for key, code in setting.modes.items() if type(value) is int and code == value), None)
                return {'power': power, 'hvac_mode': observed}
            if mode == 'off':
                if power != 'off':
                    await send(target.piid, False)
            else:
                if power == 'off':
                    await send(target.piid, True)
                    # 中枢接收开机命令后，需看到设备确实报告已开机才写第二个属性。
                    for _ in range(3):
                        qualified()
                        if await read_power() == 'on':
                            break
                        await asyncio.sleep(.15)
                    else:
                        raise ValueError('mijia_property_unconfirmed')
                await send(setting.piid, wire)
            return {'accepted': True}
    except TimeoutError:
        manager.note_local_failure(did)
        raise


def confirmed_read(row, did, siid, piid):
    # SDK 的请求关联到属性。返回含地址时还需逐项一致。
    if not isinstance(row, dict) or type(row.get('code')) is not int or row['code'] != 0:
        return False
    received_siid, received_piid = row.get('siid', siid), row.get('piid', piid)
    return (str(row.get('did', did)) == did and type(received_siid) is int and received_siid == siid
            and type(received_piid) is int and received_piid == piid)
