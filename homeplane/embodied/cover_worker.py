"""One fixed registered MiOT curtain, no camera or audio side effects."""
import asyncio
import fcntl
import hashlib
import json
import os
import sys
from datetime import UTC,datetime
from .paths import PRIVATE

async def run():
    request=json.loads(sys.stdin.read(2048));action=request['action']
    from .smart_home import Curtain
    curtain=Curtain()
    if action=='read':
        try:
            await curtain.connect();return await curtain.read()
        finally:await curtain.close()
    from homeplane.action_contracts import HomeDeviceAuthority
    authority=HomeDeviceAuthority.model_validate_json((PRIVATE/'action-authority.json').read_bytes())
    if not authority.enabled or authority.expires_at is not None and authority.expires_at<=datetime.now(UTC):raise ValueError('unauthorized')
    event=request['event_id']
    if not isinstance(event,str)or not 1<=len(event)<=160:raise ValueError('invalid_event')
    if action not in {'open','close','stop'}and not (action.startswith('position:')and action[9:].isdigit()and 0<=int(action[9:])<=100):raise ValueError('unsupported_action')
    lock=os.open(PRIVATE/'cover-command.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=PRIVATE/'cover-ledger.json';entries=json.loads(path.read_text())if path.exists()else{}
        key=hashlib.sha256(event.encode()).hexdigest()
        if key in entries:
            if entries[key].get('requested_action',action)!=action:return {'status':'rejected','acknowledged':False,'reached_target':False}
            return entries[key]
        from .ptz_control import atomic
        entries[key]={'requested_action':action,'status':'unknown_outcome','acknowledged':False,'reached_target':False}
        atomic(path,dict(list(entries.items())[-128:]))
        await curtain.connect()
        result=await curtain.act(action,event)
        if curtain.monitor:await curtain.monitor
        result={k:result.get(k)for k in ('status','acknowledged','reached_target','position_changed')}
        entries[key]=dict(result,requested_action=action);atomic(path,dict(list(entries.items())[-128:]));return result
    finally:
        await curtain.close();os.close(lock)

if __name__=='__main__':
    try:print(json.dumps(asyncio.run(run())))
    except Exception:print(json.dumps({'status':'unavailable','acknowledged':False,'reached_target':False}))
