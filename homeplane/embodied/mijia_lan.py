"""米家中枢的独立只读探测和已授权电源操作。仅使用父进程的 stdin/stdout。"""

import asyncio
import fcntl
import json
import logging
import os
import re
import secrets
import stat
import sys

from .mijia_inventory import home_devices, home_inputs, home_scope
from .mijia_power import family_binding, grant_binding, property_request
from .miot_session import MiSession
from .miot_spec import cover_binding, cover_revision
from .paths import configuration
from .ptz_control import atomic, read_lease


def home_input(directory):
    config_path = directory / 'config.json'
    membership_path = directory / 'home-room-membership.json'
    cfg = configuration(config_path) if config_path.exists() else {}
    membership = read_lease(membership_path) if membership_path.exists() else {}
    return cfg, membership, home_inputs(cfg, membership)


def private_directory(root, key):
    """给同一个账号与家庭复用独立客户端证书和虚拟编号。"""
    cache = root / 'mijia-lan'
    area = cache / key
    for path in (root, cache, area):
        if path != root:
            path.mkdir(mode=0o700, exist_ok=True)
        entry = path.lstat()
        if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
            raise ValueError('mijia_lan_private_workspace')
    fd = os.open(area / 'owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client_path = area / 'client.json'
        if not client_path.exists():
            atomic(client_path, {'virtual_did': str(secrets.randbits(63) + 1)})
        client = read_lease(client_path).get('virtual_did')
        if not isinstance(client, str) or not re.fullmatch(r'[0-9]{1,20}', client):
            raise ValueError('mijia_lan_private_workspace')
        return area, client, fd
    except BaseException:
        os.close(fd)
        raise


def local_result(row, did, siid, piid, *, write):
    if not isinstance(row, dict) or type(row.get('code')) is not int or row['code'] not in {0, 1}:
        raise ValueError('mijia_property_unconfirmed')
    if write:
        # 本地写响应由 SDK 关联至本次 RPC。返回明确属性时仍逐项核对。
        if (str(row.get('did')) != did or row.get('siid', siid) != siid
                or row.get('piid', piid) != piid):
            raise ValueError('mijia_property_unconfirmed')
        return {'accepted': True}
    if type(row.get('value')) is not bool:
        raise ValueError('mijia_property_unconfirmed')
    return {'power': 'on' if row['value'] else 'off'}


async def property_operation(manager, request, grant, group, dids, scope_current):
    did, siid, piid, power = property_request(request)
    if (not scope_current() or request.get('grant') != grant or did not in dids
            or not manager.can_control(did) or manager.in_local_cooldown(did)
            or (manager.local_device(did) or {}).get('group_id') != group):
        raise ValueError('mijia_lan_unavailable')
    try:
        async with asyncio.timeout(1.0 if power is None else 4.0):
            if power is None:
                row = await manager.get_prop_async(did, siid, piid)
            else:
                row = await manager.set_prop_async(did, siid, piid, power == 'on')
    except TimeoutError:
        manager.note_local_failure(did)
        raise
    return local_result(row, did, siid, piid, write=power is not None)


async def migrated_power_operation(manager, request, grant, family, region, group, dids, current, directory):
    from homeplane.mijia_power_home import authorize_send

    request = dict(request)
    revision, deadline = request.pop('authority_revision', None), request.pop('deadline', None)
    did, siid, piid, desired = property_request(request)
    if desired is None or not authorize_send(directory, revision, deadline, family, region, did, siid, piid):
        raise ValueError('mijia_permission_changed')
    return await property_operation(manager, request, grant, group, dids, current)


async def cover_operation(manager, request, grant, group, dids, scope_current, directory):
    from .cover_worker import authorized

    config, _, _ = home_input(directory)
    binding = cover_binding(config)
    revision = cover_revision(binding)
    did = str(binding['did'])
    if (not scope_current() or request.get('grant') != grant or request.get('did') != did
            or request.get('binding') != revision or did not in dids or not manager.can_control(did)
            or manager.in_local_cooldown(did) or (manager.local_device(did) or {}).get('group_id') != group):
        raise ValueError('mijia_lan_unavailable')
    refs = binding['refs']
    if request['cover'] == 'read':
        unreliable = binding.get('feedback_quality') == 'unreliable'
        names = ('current-position', 'fault') if unreliable else ('current-position', 'target-position', 'fault')
        async with asyncio.timeout(1.0), asyncio.TaskGroup() as tasks:
            readers = {name: tasks.create_task(manager.get_prop_async(did, *refs[name])) for name in names}
        rows = {name: reader.result() for name, reader in readers.items()}
        if (not scope_current() or any(not isinstance(r, dict) or type(r.get('code')) is not int
                                       or r['code'] != 0 or type(r.get('value')) is not int for r in rows.values())):
            raise ValueError('mijia_property_unconfirmed')
        # 该设备的角度回报已标为不可靠。保留缺失值，仍要求当前位置和故障实读。
        return {'values': [rows.get(name, {}).get('value') for name in
                           ('current-position', 'target-position', 'fault')]}
    value = request.get('value')
    parameter = request['cover']
    if parameter == 'target-position' and type(value) is int and 0 <= value <= 100:
        operation = 'cover.position'
    elif parameter == 'motor-control' and type(value) is int and value in binding['commands'].values():
        operation = 'cover.state'
    else:
        raise ValueError('mijia_invalid_binding')
    if not authorized(request.get('permission'), revision, operation, request.get('deadline'), directory):
        raise ValueError('mijia_lan_unavailable')
    siid, piid = refs[parameter]
    try:
        async with asyncio.timeout(4.0):
            row = await manager.set_prop_async(did, siid, piid, value)
    except TimeoutError:
        manager.note_local_failure(did)
        raise
    return local_result(row, did, siid, piid, write=True)


async def serve(reader, session):
    from miot.central_hub import CentralHubManager
    from miot.network import MIoTNetwork
    from miot.storage import MIoTStorage

    saved = await session.current()
    grant = grant_binding(saved)
    cfg, membership, scope = home_input(session.directory)
    region = saved['cloud_server']
    api = session.http(cloud_server=region, access_token=saved['access_token'])
    net = MIoTNetwork()
    hub = None
    lock = None
    try:
        async with asyncio.timeout(16):
            homes = await api.get_homes_async()
            home = home_scope(list(homes.values()), cfg, membership)
            allowed = home_devices(home)
            if not 1 <= len(allowed) <= 512 or not home.group_id:
                return
            family = family_binding(grant, home.home_id)
            area, client, lock = private_directory(session.directory, family)
            await net.init_async()
            hub = CentralHubManager(
                storage=MIoTStorage(str(area)), http_client=api,
                uid=saved['user_info']['uid'], cloud_server=region, virtual_did=client,
                home_ids_provider=lambda: {str(home.home_id)}, network=net,
            )
            if not hub.enabled:
                return
            await hub.init_async()
        print(json.dumps({'ready': True, 'family': family}), flush=True)

        def current():
            try:
                latest = read_lease(session.path)
                if grant_binding(latest) != grant or home_input(session.directory)[2] != scope:
                    return False
                api.update_http_header(access_token=latest['access_token'])
                return True
            except (OSError, ValueError, TypeError, KeyError):
                return False

        while packet := await reader.readline():
            reply = {'error': 'mijia_lan_unavailable'}
            ident = None
            try:
                request = json.loads(packet)
                ident = request.pop('id')
                if type(ident) is not int or not current():
                    return
                if request.pop('family') != family:
                    raise ValueError('mijia_lan_unavailable')
                if request == {'routes': True}:
                    routes = sorted(did for did in allowed if hub.can_control(did)
                                    and not hub.in_local_cooldown(did)
                                    and (hub.local_device(did) or {}).get('group_id') == home.group_id)
                    reply = {'connected': hub.is_ready, 'routes': routes,
                             'push': sum(hub.can_push(did) for did in routes)}
                elif 'cover' in request:
                    reply = await cover_operation(hub, request, grant, home.group_id, allowed, current, session.directory)
                elif 'authority_revision' in request or 'deadline' in request:
                    reply = await migrated_power_operation(
                        hub, request, grant, family, region, home.group_id, allowed, current, session.directory,
                    )
                else:
                    reply = await property_operation(hub, request, grant, home.group_id, allowed, current)
            except (OSError, ValueError, RuntimeError, TimeoutError, TypeError, KeyError, ExceptionGroup):
                pass
            print(json.dumps({'id': ident, **reply}), flush=True)
    finally:
        try:
            if hub is not None:
                await hub.deinit_async()
        finally:
            try:
                try:
                    await net.deinit_async()
                finally:
                    await api.deinit_async()
            finally:
                if lock is not None:
                    os.close(lock)


async def main():
    reader = asyncio.StreamReader(limit=4096)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
    )
    try:
        await serve(reader, MiSession())
    finally:
        transport.close()


if __name__ == '__main__':
    # SDK 错误详情可能包含家庭和设备标识。
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        asyncio.run(main())
    except Exception:
        sys.exit(1)
