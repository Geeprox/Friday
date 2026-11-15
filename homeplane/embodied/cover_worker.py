"""执行已授权的米家窗帘操作，无需摄像头或 HA 会话。"""

import asyncio
import fcntl
import hashlib
import json
import math
import os
import sys
from time import monotonic
from types import SimpleNamespace

from homeplane.action_contracts import HomeDeviceAuthority
from homeplane.actions import cover_approval, read_cover_authority, read_device_authority

from .mijia_lan import home_input
from .mijia_power import grant_binding
from .miot_session import MiSession
from .miot_spec import cover_binding, cover_revision
from .paths import PRIVATE
from .ptz_control import atomic, read_lease
from .smart_home import Curtain


def authorized(permission, revision, operation, deadline, directory):
    if not isinstance(permission, dict) or (deadline is not None and (
        type(deadline) not in (float, int) or not math.isfinite(deadline)
        or not 0 < deadline - monotonic() <= 1.5
    )):
        return False
    authority, version = read_device_authority(directory / 'action-authority.json', include_generation=True)
    if (not isinstance(authority, HomeDeviceAuthority) or authority.external_entity_ids is not None
            or version != permission.get('authority_revision') or operation not in authority.operations):
        return False
    try:
        scope = read_cover_authority(directory)
        if (scope.legacy_approval != cover_approval(authority) or scope.household != permission.get('household')
                or scope.binding != permission.get('binding') or scope.binding != revision):
            return False
        config, _, inputs = home_input(directory)
        return (inputs == permission.get('inputs') and revision == cover_revision(cover_binding(config))
                and grant_binding(read_lease(directory / 'miot-session.json')) == permission.get('grant'))
    except (OSError, ValueError, TypeError, KeyError):
        return False


class LocalCoverPort:
    """以固定的三项状态与一个写入参数复用同一窗帘动作逻辑。"""

    def __init__(self, call, permission, binding, deadline):
        self.call, self.permission, self.binding, self.deadline = call, permission, binding, deadline

    async def get_props_async(self, params):
        reply = await self.call({'cover': 'read'})
        values = reply.get('values')
        if not isinstance(values, list) or len(values) != len(params):
            raise ValueError('mijia_property_unconfirmed')
        return [{'did': p.did, 'siid': p.siid, 'piid': p.piid, 'code': 0, 'value': value}
                for p, value in zip(params, values)]

    async def set_prop_async(self, param):
        name = next((name for name in ('target-position', 'motor-control')
                     if self.binding['refs'][name] == (param.siid, param.piid)), None)
        if name is None or param.did != str(self.binding['did']):
            raise ValueError('mijia_invalid_binding')
        reply = await self.call({'cover': name, 'value': param.value, 'permission': self.permission,
                                 'deadline': self.deadline})
        return {'code': 0 if reply.get('accepted') is True else -1}

    async def deinit_async(self):
        pass


async def run(request, *, directory=PRIVATE, curtain_factory=Curtain, local=None):
    action = request['action']
    config, _, _ = home_input(directory)
    binding = cover_binding(config)
    revision = cover_revision(binding)
    curtain = curtain_factory()
    channel = 'cloud'
    try:
        if action == 'read':
            if local is not None:
                curtain.bind(binding, LocalCoverPort(local, request.get('permission'), binding, None),
                             SimpleNamespace, SimpleNamespace, 'MiOT LAN')
                return await curtain.read()
            await curtain.connect(binding=binding, session=MiSession(directory))
            return dict(curtain.state)
        if isinstance(action, str) and action in {'open', 'close', 'stop'}:
            operation = 'cover.state'
        elif isinstance(action, str) and action.startswith('position:') and len(action) <= 12 \
                and action[9:].isascii() and action[9:].isdigit() and 0 <= int(action[9:]) <= 100:
            operation = 'cover.position'
        else:
            raise ValueError('unsupported_action')
        event = request['event_id']
        if not isinstance(event, str) or not 1 <= len(event) <= 160:
            raise ValueError('invalid_event')
        permission = request.get('permission')
        deadline = request.get('dispatch_deadline')
        if request.get('expected_revision') != revision or not authorized(permission, revision, operation, deadline, directory):
            return {'status': 'rejected', 'code': 'unauthorized', 'write_attempted': False}

        lock = os.open(directory / 'cover-command.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'rejected', 'code': 'busy', 'write_attempted': False}
            path = directory / 'cover-ledger.json'
            try:
                entries = read_lease(path)
            except FileNotFoundError:
                entries = {}
            key = hashlib.sha256((revision + ':' + event).encode()).hexdigest()
            legacy = hashlib.sha256(event.encode()).hexdigest()
            if key in entries or legacy in entries:
                row = entries[key] if key in entries else entries[legacy]
                if row.get('requested_action', action) != action:
                    return {'status': 'rejected', 'code': 'source_conflict', 'write_attempted': False}
                return {**row, 'write_attempted': row.get('write_attempted',
                        row.get('acknowledged') is True or row.get('status') == 'unknown_outcome')}

            def claim():
                if not authorized(permission, revision, operation, deadline, directory):
                    return False
                entries[key] = {'requested_action': action, 'status': 'unknown_outcome', 'write_attempted': True}
                atomic(path, dict(list(entries.items())[-128:]))
                return True

            preflight = None
            if local is not None:
                port = LocalCoverPort(local, permission, binding, deadline)
                curtain.bind(binding, port, SimpleNamespace, SimpleNamespace, 'MiOT LAN')
                try:
                    preflight = await curtain.read()
                    channel = 'lan'
                except (OSError, ValueError, TimeoutError, RuntimeError):
                    # 父进程只能在只读预检无法完成时切换到米家云端。
                    return {'status': 'local_preflight_unavailable', 'write_attempted': False}
            if channel == 'cloud':
                await curtain.connect(binding=binding, session=MiSession(directory), expected_grant=permission['grant'],
                                      preflight=False)
            result = await curtain.act(action, event, claim=claim, preflight=preflight)
            if curtain.monitor:
                await curtain.monitor
            receipt = {k: result[k] for k in ('status', 'code', 'acknowledged', 'write_attempted',
                       'reached_target', 'position_changed') if k in result}
            receipt['channel'] = channel
            if key in entries or result.get('reached_target') is True:
                entries[key] = dict(receipt, requested_action=action)
                atomic(path, dict(list(entries.items())[-128:]))
            return receipt
        finally:
            os.close(lock)
    finally:
        await curtain.close()


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(run(json.loads(sys.stdin.read(4096))))))
    except Exception:
        print(json.dumps({'status': 'unavailable', 'acknowledged': False, 'reached_target': False}))
