"""Live Mi Home manual scenes, bound by stable cloud IDs and one-shot intent."""
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import sys

from .paths import PRIVATE
from .ptz_control import atomic,read_lease

logging.getLogger('miot').setLevel(logging.CRITICAL)
digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


class SceneCommands:
    def __init__(self,api,homes,directory,*,region):
        self.api,self.homes,self.directory,self.region=api,homes,directory,region

    async def inventory(self):
        values=await self.api.get_manual_scenes_async(list(self.homes.values()))
        result={}
        for scene in values.values():
            if scene.home_id not in self.homes:continue
            ref=digest([self.region,scene.home_id,scene.scene_id])
            revision=digest([ref,scene.update_ts,scene.scene_name,scene.enable,sorted(scene.dids or [])])
            result[ref]=(scene,dict(id=ref,name=scene.scene_name,revision=revision,
                enabled=scene.enable,device_count=len(scene.dids or []),source='mijia',physically_confirmed=False))
        return result

    async def listing(self):
        return dict(connected=True,scenes=[row for _,row in (await self.inventory()).values()])

    async def run(self,ref,revision,event):
        if not all(re.fullmatch(r'[a-f0-9]{64}',v or '') for v in (ref,revision)) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',event or ''):
            raise ValueError('invalid_scene_request')
        fd=os.open(self.directory/'scene-commands.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            path=self.directory/'scene-commands.json';ledger=read_lease(path) if path.exists() else {}
            key=digest(event);request=dict(scene=ref,revision=revision)
            if key in ledger:
                if ledger[key]['request']!=request:raise ValueError('scene_request_conflict')
                return ledger[key]['result']
            inventory=await self.inventory();bound=inventory.get(ref)
            if not bound or bound[1]['revision']!=revision or bound[0].enable is False:
                raise ValueError('scene_changed')
            result=dict(status='unknown_outcome',acknowledged=False,physically_confirmed=False,scene=ref)
            ledger[key]=dict(request=request,result=result);atomic(path,dict(list(ledger.items())[-64:]))
            try:
                accepted=await asyncio.wait_for(self.api.run_manual_scene_async(bound[0]),15)
                if accepted is True:result.update(status='accepted',acknowledged=True)
                elif accepted is False:result.update(status='rejected')
            except Exception:pass # The claimed event is never automatically replayed.
            ledger[key]['result']=result;atomic(path,dict(list(ledger.items())[-64:]))
            return result
        finally:os.close(fd)


async def run():
    from miot.cloud import MIoTHttpClient
    request=json.loads(sys.stdin.read(2048))
    from .miot_session import MiSession
    session=await MiSession(PRIVATE).current()
    api=MIoTHttpClient(cloud_server=session['cloud_server'],access_token=session['access_token'])
    try:
        homes=await api.get_homes_async()
        membership=PRIVATE/'home-room-membership.json'
        if membership.exists():
            home=read_lease(membership)['home_id'];homes={key:value for key,value in homes.items() if value.home_id==home}
        if len(homes)!=1:raise ValueError('home_selection_required')
        homes={value.home_id:value for value in homes.values()}
        owner=SceneCommands(api,homes,PRIVATE,region=session['cloud_server'])
        if request['action']=='list':return await owner.listing()
        if request['action']!='run':raise ValueError('invalid_scene_request')
        return await owner.run(request['scene'],request['revision'],request['event_id'])
    finally:await api.deinit_async()


if __name__=='__main__':
    try:print(json.dumps(asyncio.run(asyncio.wait_for(run(),45)),ensure_ascii=False))
    except Exception:print(json.dumps(dict(connected=False,scenes=[],code='mijia_scenes_unavailable')))
