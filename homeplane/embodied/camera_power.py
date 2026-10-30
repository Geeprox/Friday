"""Explicit per-camera power commands; reads/recovery never open a shutter."""
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import time

from .paths import PRIVATE, camera, camera_storage
from .ptz_control import atomic, read_lease

logging.getLogger('miot').setLevel(logging.CRITICAL)


class PowerTransport:
    def __init__(self,selected):
        from miot.cloud import MIoTHttpClient
        from .miot_spec import camera_property
        self.selected=selected
        self.ref=camera_property(selected,'camera_power','read')
        if camera_property(selected,'camera_power','write')!=self.ref:raise ValueError('camera_power_unavailable')
        props=[p for s in selected.get('spec',{}).get('services',[]) if s.get('iid')==self.ref[0]
               for p in s.get('properties',[]) if p.get('iid')==self.ref[1]]
        if len(props)!=1 or props[0].get('format')!='bool':raise ValueError('camera_power_unavailable')
        login=json.loads((PRIVATE/'miot-session.json').read_text())
        self.api=MIoTHttpClient(cloud_server=selected.get('region','cn'),access_token=login['access_token'])

    async def read(self):
        from miot.types import MIoTGetPropertyParam
        rows=await asyncio.wait_for(self.api.get_props_async([MIoTGetPropertyParam(
            did=self.selected['device']['did'],siid=self.ref[0],piid=self.ref[1])]),12)
        if (len(rows)!=1 or rows[0].get('code')!=0 or type(rows[0].get('value')) is not bool
                or str(rows[0].get('did'))!=str(self.selected['device']['did'])
                or (rows[0].get('siid'),rows[0].get('piid'))!=self.ref):
            raise ValueError('camera_power_unavailable')
        return rows[0]['value']

    async def write(self,value):
        from miot.types import MIoTSetPropertyParam
        result=await asyncio.wait_for(self.api.set_prop_async(MIoTSetPropertyParam(
            did=self.selected['device']['did'],siid=self.ref[0],piid=self.ref[1],value=value)),12)
        if result.get('code')!=0:raise ValueError('camera_power_unavailable')

    async def close(self):await self.api.deinit_async()


class PowerController:
    def __init__(self,selected,transport,directory,*,clock=time.time):
        self.selected,self.io,self.directory,self.clock=selected,transport,directory,clock

    async def status(self):
        enabled=await self.io.read()
        if type(enabled) is not bool:raise ValueError('camera_power_unavailable')
        revision=hashlib.sha256(json.dumps([self.selected['id'],self.selected['device']['did'],enabled]).encode()).hexdigest()
        return dict(camera=self.selected['id'],enabled=enabled,revision=revision,basis='reported',physically_confirmed=False)

    async def set(self,enabled,revision,event):
        if type(enabled) is not bool or not re.fullmatch(r'[a-f0-9]{64}',revision or '') or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',event or ''):
            raise ValueError('invalid_camera_power_request')
        fd=os.open(self.directory/'power.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            path=self.directory/'power-ledger.json';ledger=read_lease(path) if path.exists() else {}
            key=hashlib.sha256(event.encode()).hexdigest();request=dict(enabled=enabled,revision=revision)
            if key in ledger:
                if ledger[key]['request']!=request:raise ValueError('camera_power_conflict')
                return ledger[key]['result']
            current=await self.status()
            if current['revision']!=revision:raise ValueError('camera_power_changed')
            result=dict(status='unknown_outcome',camera=self.selected['id'],acknowledged=False,physically_confirmed=False)
            ledger[key]=dict(request=request,result=result)
            atomic(path,dict(list(ledger.items())[-96:]))
            # Persist the newer human intent before network IO. Recovery may
            # never undo it or replay an uncertain opening after a crash.
            atomic(self.directory/'power-intent.json',dict(camera=self.selected['id'],enabled=enabled,at=self.clock()))
            if current['enabled']==enabled:
                result=dict(current,status='reported_match',acknowledged=False)
            else:
                try:
                    await self.io.write(enabled);result['acknowledged']=True
                    for _ in range(5):
                        current=await self.status()
                        if current['enabled']==enabled:
                            result=dict(current,status='reported_match',acknowledged=True);break
                        await asyncio.sleep(.4)
                except (ValueError,OSError,TimeoutError):pass
            ledger[key]['result']=result;atomic(path,dict(list(ledger.items())[-96:]))
            return result
        finally:os.close(fd)


async def run():
    from .miot_session import MiSession
    await MiSession(PRIVATE).current()
    request=json.loads(sys.stdin.read(2048));selected=camera(request['camera'])
    io=PowerTransport(selected)
    try:
        owner=PowerController(selected,io,camera_storage(selected['id']))
        if request['action']=='read':return await owner.status()
        if request['action']!='set':raise ValueError('invalid_camera_power_request')
        return await owner.set(request['enabled'],request['revision'],request['event_id'])
    finally:await io.close()


if __name__=='__main__':
    try:print(json.dumps(asyncio.run(run())))
    except Exception:print(json.dumps({'status':'unavailable','code':'camera_power_unavailable'}))
