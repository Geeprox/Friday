"""米家电源属性的官方 SDK 读写。由已授权的设备计划调用。"""

import asyncio
import hashlib
import json
import logging
import re
import sys

logging.getLogger('miot').setLevel(logging.CRITICAL)


def grant_binding(saved):
    uid = (saved.get('user_info') or {}).get('uid')
    region = saved.get('cloud_server')
    registration = saved.get('oauth_uuid')
    if not all(isinstance(value, str) and value for value in (uid, region, registration)):
        raise ValueError('mijia_authorization_required')
    return hashlib.sha256(json.dumps([region, uid, registration]).encode()).hexdigest()


def family_binding(grant, home_id):
    return hashlib.sha256(json.dumps([grant, str(home_id)]).encode()).hexdigest()


def response_value(rows, did, siid, piid, *, write):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError('mijia_property_unconfirmed')
    row = rows[0]
    if (str(row.get('did')) != did or row.get('siid') != siid or row.get('piid') != piid
            or type(row.get('code')) is not int or row['code'] != 0):
        raise ValueError('mijia_property_unconfirmed')
    if write:
        return {'accepted': True}
    if type(row.get('value')) is not bool:
        raise ValueError('mijia_property_unconfirmed')
    return {'power': 'on' if row['value'] else 'off'}


def property_request(request):
    if set(request) - {'did', 'siid', 'piid', 'grant', 'power'}:
        raise ValueError('mijia_invalid_binding')
    did, siid, piid = request['did'], request['siid'], request['piid']
    desired = request.get('power')
    if (not isinstance(did, str) or not re.fullmatch(r'(?:[0-9]{1,32}|blt\.[0-9]\.[a-z0-9]{1,64})', did)
            or any(type(iid) is not int or not 1 <= iid <= 65535 for iid in (siid, piid))
            or desired not in (None, 'on', 'off')):
        raise ValueError('mijia_invalid_binding')
    return did, siid, piid, desired


async def run(request):
    from miot.types import MIoTGetPropertyParam, MIoTSetPropertyParam

    from .miot_session import MiSession

    did, siid, piid, desired = property_request(request)
    session = MiSession()
    saved = await session.current()
    if grant_binding(saved) != request.get('grant'):
        raise ValueError('mijia_authorization_required')
    api = session.http(cloud_server=saved['cloud_server'], access_token=saved['access_token'])
    try:
        target = dict(did=did, siid=siid, piid=piid)
        if desired is None:
            rows = await api.get_props_async([MIoTGetPropertyParam(**target)])
        else:
            rows = await api.set_props_async([MIoTSetPropertyParam(**target, value=desired == 'on')])
        return response_value(rows, did, siid, piid, write=desired is not None)
    finally:
        await api.deinit_async()


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(asyncio.wait_for(run(json.loads(sys.stdin.read(4096))), 8))))
    except Exception:
        # 不记录原始上游异常，可能含有设备标识和授权数据。
        print('{"error":"mijia_property_unconfirmed"}')
