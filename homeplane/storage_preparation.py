"""Restore the saved household SMB connection through macOS, without login UI.

No credential values cross this boundary. NetFS may reuse the current user's
saved session/keychain. A new mount is read-only. Existing mounts are reused.
"""
import asyncio
import json
import sys
import time

from fastapi import APIRouter

from .settings import SettingsConfig
from .worker_runtime import module_command


async def mount_worker(command,*,input,timeout):
    process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        output,_=await asyncio.wait_for(process.communicate(input),timeout)
        if len(output)>2048:raise ValueError('storage_output_budget')
        return process.returncode,output
    finally:
        if process.returncode is None:process.kill();await process.wait()


class StoragePreparation:
    def __init__(self,settings,*,runner=mount_worker,platform=sys.platform,simulated=lambda:False):
        self.settings,self.runner,self.platform,self.simulated=settings,runner,platform,simulated
        self.lock=asyncio.Lock();self.last=None;self.last_at=0;self.last_source=None

    async def prepare(self):
        async with self.lock:
            source=(self.settings.config.nas_url,self.settings.config.nas_path)
            report=await self.settings.probe_nas()
            if report.get('readable') is True:return dict(state='online',readable=True,automatically_prepared=False)
            if not any(source):return dict(state='not_configured',readable=False,code='storage_not_configured')
            if self.simulated():return dict(state='unavailable',readable=False,code='simulated_storage_not_connected')
            if self.last_source==source and time.monotonic()-self.last_at<2 and self.last:return self.last
            result=dict(state='unavailable',readable=False,code='storage_unavailable')
            if source[0] and report.get('status')=='mount_required' and self.platform=='darwin':
                if source!=(self.settings.config.nas_url,self.settings.config.nas_path):return dict(state='changed',readable=False)
                try:
                    code,data=await self.runner(module_command('homeplane.storage_preparation'),
                        input=json.dumps(dict(url=source[0])).encode(),timeout=25)
                    mounted=json.loads(data) if code==0 and len(data)<=2048 else {}
                    if source!=(self.settings.config.nas_url,self.settings.config.nas_path):return dict(state='changed',readable=False)
                    check=await self.settings.probe_nas()
                    if check.get('readable') is True:result=dict(state='online',readable=True,automatically_prepared=True)
                    elif mounted.get('authentication_required'):result=dict(state='needs_authentication',readable=False,code='storage_authentication_required')
                except (ValueError,OSError,TimeoutError):pass
            self.last,self.last_at,self.last_source=result,time.monotonic(),source
            return result


def create_router(settings,core):
    router=APIRouter(prefix='/storage')
    owner=StoragePreparation(settings,simulated=lambda:getattr(core.adapter,'mode',None)=='simulator')
    @router.post('/prepare')
    async def prepare():return await owner.prepare()
    return router


def mount_saved_url(url):
    if sys.platform!='darwin':return dict(mounted=False)
    SettingsConfig(nas_url=url)
    import ctypes as c
    import plistlib
    cf=c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    net=c.CDLL('/System/Library/Frameworks/NetFS.framework/NetFS')
    cf.CFDataCreate.argtypes=[c.c_void_p,c.c_void_p,c.c_long];cf.CFDataCreate.restype=c.c_void_p
    cf.CFPropertyListCreateWithData.argtypes=[c.c_void_p,c.c_void_p,c.c_ulong,c.c_void_p,c.c_void_p];cf.CFPropertyListCreateWithData.restype=c.c_void_p
    cf.CFURLCreateWithBytes.argtypes=[c.c_void_p,c.c_void_p,c.c_long,c.c_uint,c.c_void_p];cf.CFURLCreateWithBytes.restype=c.c_void_p
    cf.CFRelease.argtypes=[c.c_void_p]
    net.NetFSMountURLSync.argtypes=[c.c_void_p]*7;net.NetFSMountURLSync.restype=c.c_int
    allocated=[]
    def options(value):
        raw=plistlib.dumps(value);data=cf.CFDataCreate(None,raw,len(raw));allocated.append(data)
        result=cf.CFPropertyListCreateWithData(None,data,1,None,None);allocated.append(result)
        if not result:raise ValueError('storage_options_unavailable')
        return result
    try:
        raw=url.encode();ref=cf.CFURLCreateWithBytes(None,raw,len(raw),0x08000100,None);allocated.append(ref)
        if not ref:raise ValueError('invalid_storage_url')
        opened=options({'UIOption':'NoUI','Guest':False})
        mounted=options({'MountFlags':1,'SoftMount':True,'AllowSubMounts':True})
        points=c.c_void_p();code=net.NetFSMountURLSync(ref,None,None,None,opened,mounted,c.byref(points))
        if points.value:allocated.append(points.value)
        return dict(mounted=code==0,authentication_required=code in {13,80,-128,-25293},code=code)
    finally:
        for value in reversed(allocated):
            if value:cf.CFRelease(value)


if __name__=='__main__':
    try:print(json.dumps(mount_saved_url(json.loads(sys.stdin.read(2048))['url'])))
    except Exception:print(json.dumps(dict(mounted=False)))
