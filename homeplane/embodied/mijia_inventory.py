"""列出同一家庭的米家设备，用户确认后可保存只供手动预览的摄像头。"""

import asyncio
import fcntl
import hashlib
import json
import os
import re

from .miot_session import MiSession, authorization_error
from .miot_spec import camera_property
from .paths import PRIVATE, configuration
from .ptz_control import atomic, read_lease


def home_devices(home):
    return {str(did) for did in home.dids or []} | {
        str(did) for room in home.room_list.values() for did in room.dids or []
    }


def home_scope(homes, config, membership):
    cameras = {str(camera['device']['did']) for camera in config.get('cameras', [])}
    matches = [home for home in homes if cameras and cameras <= home_devices(home)]
    if len(matches) == 1:
        return matches[0]
    exact = [home for home in homes if str(home.home_id) == str(membership.get('home_id') or '')
             and (not cameras or cameras & home_devices(home))]
    if len(exact) == 1:
        return exact[0]
    if not cameras and not membership and len(homes) == 1:
        return homes[0]
    raise ValueError('home_selection_required')


def is_camera(device):
    urn = device.urn.split(':')
    return len(urn) >= 5 and urn[2] == 'device' and urn[3] in {
        'camera', 'ip-camera', 'doorbell', 'video-doorbell'
    } or '.camera.' in device.model


def reference(region, home_id, did):
    return hashlib.sha256(json.dumps([region, str(home_id), str(did)]).encode()).hexdigest()


def device_rows(region, home, devices):
    return {reference(region, home.home_id, did): device for did, device in devices.items()}


async def read_inventory(session, config, membership):
    saved = await session.current()
    region = saved['cloud_server']
    api = session.http(cloud_server=region, access_token=saved['access_token'])
    try:
        homes = list((await api.get_homes_async(fetch_share_home=True)).values())
        home = home_scope(homes, config, membership)
        dids = home_devices(home)
        if len(dids) > 512:
            raise ValueError('inventory_limit')
        devices = await api.get_devices_async(home_infos=[home], fetch_share_home=True) if dids else {}
        return region, device_rows(region, home, devices)
    finally:
        await api.deinit_async()


def public_inventory(region, rows):
    devices = {}
    for candidate, device in rows.items():
        room = (device.room_name or '').strip()[:80] if device.room_id != device.home_id else ''
        # 仅在本机服务内用区域与编号去重。界面使用不可变的候选引用。
        devices[candidate] = dict(id=candidate, identity=f'{region}_{device.did}',
                                  name=display_name(device),
                                  room=room, kind='camera' if is_camera(device) else 'device',
                                  online=device.online is True)
        for child in device.sub_devices.values():
            child_ref = reference(region, device.home_id, child.did)
            devices[child_ref] = dict(id=child_ref, identity=f'{region}_{child.did}',
                                      name=display_name(child), room=room,
                                      kind='device', online=child.online is True)
        if len(devices) > 512:
            raise ValueError('inventory_limit')
    return list(devices.values())


def display_name(device):
    return ''.join(char for char in device.name if char.isprintable()).strip()[:80] or device.model[:80]


async def camera_spec(device):
    # 只访问公开的设备规格，用型号 URN 校验权限和布尔隐私开关。
    import httpx

    urn = device.urn
    if not re.fullmatch(r'urn:miot-spec(?:-v2)?:device:[a-z0-9-]+:[A-Fa-f0-9]+:[A-Za-z0-9.:-]{1,144}', urn):
        raise ValueError('camera_spec_unavailable')
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            async with client.stream('GET', 'https://miot-spec.org/miot-spec-v2/instance',
                                     params={'type': urn}) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        raise ValueError('camera_spec_too_large')
        raw = json.loads(data)
        if not isinstance(raw, dict) or raw.get('type') != urn:
            raise ValueError('camera_spec_mismatch')
        ref = camera_property({'spec': raw}, 'camera_power', 'read')
        service = next(service for service in raw['services'] if service['iid'] == ref[0])
        prop = next(prop for prop in service['properties'] if prop['iid'] == ref[1])
        if prop.get('format') != 'bool':
            raise ValueError('camera_power_missing')
        # 手动预览只保留确认隐私挡板所需的只读规格。
        return {'type': urn, 'services': [{'iid': ref[0], 'type': service['type'],
            'properties': [{'iid': ref[1], 'type': prop['type'], 'format': 'bool', 'access': ['read']}]}]}
    except (httpx.HTTPError, OSError, ValueError, TimeoutError, StopIteration):
        raise ValueError('camera_spec_unavailable') from None


async def connect_camera(candidate, session, directory, config, membership, read_spec=camera_spec):
    if not config.get('enabled') or not config.get('main_origin') or not config.get('viewer_port'):
        raise ValueError('camera_setup_required')
    region, rows = await read_inventory(session, config, membership)
    device = rows.get(candidate)
    if device is None or not is_camera(device) or region != 'cn':
        raise ValueError('camera_setup_required')
    existing = next((camera['id'] for camera in config['cameras']
                     if camera['device']['did'] == device.did), None)
    if existing is not None:
        return {'state': 'connected', 'camera': existing}
    if len(config['cameras']) >= 8:
        raise ValueError('camera_limit')
    camera_id = 'cam_' + candidate[:10]
    spec = await read_spec(device)
    config_path = directory / 'config.json'
    lock_path = directory / 'camera-registration.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = configuration(config_path)
        if current != config:
            if any(camera['id'] == camera_id and camera['device']['did'] == device.did
                   for camera in current.get('cameras', [])):
                return {'state': 'connected', 'camera': camera_id}
            raise ValueError('inventory_changed')
        if any(camera['id'] == camera_id for camera in current['cameras']):
            raise ValueError('camera_limit')
        current['cameras'].append(dict(id=camera_id, name=display_name(device),
                                       room=(device.room_name or '其他')[:80] if device.room_id != device.home_id else '其他',
                                       model=device.model,
                                       region=region, device=device.model_dump(mode='json', exclude={'sub_devices'}),
                                       spec=spec, preview_only=True, allow_open=False, takeover=False))
        current['cameras'][-1]['device']['sub_devices'] = {}
        atomic(config_path, current)
        return {'state': 'connected', 'camera': camera_id}
    finally:
        os.close(fd)


async def run(request, directory=PRIVATE):
    action = request.get('action')
    try:
        config_path = directory / 'config.json'
        membership_path = directory / 'home-room-membership.json'
        config = configuration(config_path) if config_path.exists() else {}
        membership = read_lease(membership_path) if membership_path.exists() else {}
        session = MiSession(directory)
        if action == 'inventory':
            region, rows = await read_inventory(session, config, membership)
            return {'state': 'ready', 'region': region, 'devices': public_inventory(region, rows)}
        if action == 'connect' and re.fullmatch(r'[a-f0-9]{64}', request.get('candidate') or ''):
            return await connect_camera(request['candidate'], session, directory, config, membership)
        raise ValueError('invalid_request')
    except Exception as error:
        allowed = {'home_selection_required', 'mijia_authorization_required', 'camera_setup_required',
                   'camera_spec_unavailable', 'inventory_changed', 'camera_limit'}
        code = 'mijia_authorization_required' if authorization_error(error) else str(error)
        return {'state': code if code in allowed else 'unavailable'}


if __name__ == '__main__':
    import sys

    try:
        print(json.dumps(asyncio.run(asyncio.wait_for(run(json.loads(sys.stdin.read(2048))), 35))))
    except Exception:
        print(json.dumps({'state': 'unavailable'}))
