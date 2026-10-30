"""Production-function regressions for reviewed races; no SDK or real devices."""
import ast
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from homeplane.embodied.ptz_control import CameraLease, SearchPlanner, FLAGS


def functions(names,scope):
    path=Path('homeplane/embodied/worker.py')
    nodes=[n for n in ast.parse(path.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),scope)


@pytest.mark.asyncio
async def test_exited_capture_race_cannot_skip_native_restore_or_retain_lock(tmp_path):
    values=dict(camera_power=True,cruise=True,human_tracking=True,pet_tracking=True,
                human_close_up=False,active_preset=1,presets=[])
    original=dict(values)
    async def write(name,value):values[name]=value
    io=SimpleNamespace(read=AsyncMock(side_effect=lambda:dict(values)),write=write,close=AsyncMock())
    lease=CameraLease(io,tmp_path/'lease.json')
    def exited():raise ProcessLookupError('synthetic_capture_exited')
    capture=SimpleNamespace(returncode=None,terminate=exited,wait=AsyncMock())
    scope=dict(asyncio=asyncio,time=time,os=os,Transport=lambda:io,CameraLease=lambda *_,**__:lease,
               SearchPlanner=SearchPlanner,CameraDirector=lambda:None,camera_is_open=AsyncMock(return_value=False),
               SELECTED={'id':'synthetic'},close_primary_view=lambda:None,capture=capture,state={},
               camera_lease=None,camera_planner=None)
    functions({'camera_search','reap'},scope)
    await scope['camera_search']()
    assert values==original and lease.lock_fd is None
    assert json.loads(lease.path.read_text())['status']=='restored'


@pytest.mark.asyncio
async def test_manual_close_during_room_wait_blocks_motor_send():
    power=True;sent=[];reads=0
    async def is_open(_):
        nonlocal reads
        reads+=1;return power
    class RoomLock:
        async def __aenter__(self):
            nonlocal power
            await asyncio.sleep(0);power=False
        async def __aexit__(self,*_):pass
    class Stdin:
        def write(self,data):sent.append(data)
        async def drain(self):raise AssertionError('closed camera cannot send')
    noop=lambda *_,**__:None
    scope=dict(asyncio=asyncio,time=time,secrets=secrets,json=json,capture=SimpleNamespace(stdin=Stdin()),
        camera_lease=SimpleNamespace(snapshot={'status':'owned'}),camera_planner=None,state={'status':'live'},
        SELECTED={'id':'synthetic'},camera_is_open=is_open,close_primary_view=noop,motor_sequence=0,
        motion_generation=0,motor_waiters={},identities=SimpleNamespace(discard=noop),fleet=SimpleNamespace(clear=noop),
        home=SimpleNamespace(selected=True,epoch=0,vad=[],pending_audio=None,runtime_log=SimpleNamespace(record=noop)),
        room_lock=RoomLock(),room=SimpleNamespace(expect_motion=noop))
    functions({'motor_step'},scope)
    with pytest.raises(RuntimeError,match='privacy_shutter_closed'):await scope['motor_step']('left',time.monotonic())
    assert reads==2 and not sent


@pytest.mark.asyncio
async def test_preset_ack_without_readback_remains_pending(tmp_path):
    values=dict(camera_power=True,cruise=True,human_tracking=True,pet_tracking=True,
                human_close_up=False,active_preset=1,presets=[])
    async def write(name,value):
        if name!='active_preset':values[name]=value
    io=SimpleNamespace(read=AsyncMock(side_effect=lambda:dict(values)),write=write)
    lease=CameraLease(io,tmp_path/'lease.json')
    await lease.acquire();values['active_preset']=2
    original=lease.read_until
    async def bounded(expected,seconds=5):return await original(expected,seconds=0)
    lease.read_until=bounded
    assert not await lease.release()
    assert json.loads(lease.path.read_text())['status']=='restore_pending'
    assert all(values[name] is lease.snapshot['before'][name] for name in FLAGS)


def test_capture_reparented_during_imports_never_enters_sdk_main(tmp_path):
    entry=Path('homeplane/embodied/capture_worker.py').resolve()
    child='''
import ast,asyncio,os,sys,time
from pathlib import Path
time.sleep(.2)
async def main():Path(sys.argv[1]).touch()
node=ast.parse(Path(sys.argv[2]).read_text()).body[-1]
exec(compile(ast.Module(body=[node],type_ignores=[]),sys.argv[2],'exec'),globals())
'''
    parent='''
import os,subprocess,sys
subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2],sys.argv[3]],
                 env=dict(os.environ,FRIDAY_CAPTURE_OWNER_PID=str(os.getpid())))
'''
    marker=tmp_path/'sdk-main-entered'
    result=subprocess.run([sys.executable,'-B','-c',parent,child,str(marker),str(entry)],
                          env=dict(os.environ,PYTHONPATH=str(Path.cwd())),capture_output=True,timeout=3)
    assert result.returncode==0 and not marker.exists()


@pytest.mark.asyncio
async def test_secondary_cleanup_exception_cannot_leave_sdk_child_alive(tmp_path):
    from homeplane.embodied import previews, ptz_control
    class Camera:
        def __init__(self):
            self.values=dict(camera_power=True,cruise=True,human_tracking=True,pet_tracking=True,
                             human_close_up=False,active_preset=1,presets=[])
        async def read(self):return dict(self.values)
        async def write(self,name,value):self.values[name]=value
        async def close(self):pass
    io=Camera();before=dict(io.values);leases=[]
    def lease_factory(*args,**kwargs):
        lease=CameraLease(*args,**kwargs);leases.append(lease);return lease
    class Recognizer:
        async def start(self):pass
        async def close(self):raise ProcessLookupError('synthetic vision exit race')
    def rec_factory(*args):return Recognizer()
    process=SimpleNamespace(returncode=None,stdin=SimpleNamespace(write=lambda _:None,drain=AsyncMock()),
                            stdout=SimpleNamespace(readline=AsyncMock(return_value=b'')),
                            terminate=AsyncMock(),wait=AsyncMock(return_value=0))
    terminated=[]
    def terminate():
        terminated.append(True)
        process.returncode=0
    process.terminate=terminate
    process.kill=terminate
    with patch.object(previews,'DATA',tmp_path),patch.object(previews,'camera_is_open',AsyncMock(return_value=True)),\
         patch.object(previews,'camera_storage',lambda _:tmp_path),patch.object(previews,'capture_environment',lambda:{}),\
         patch.object(ptz_control,'Transport',lambda _:io),patch.object(ptz_control,'CameraLease',lease_factory),\
         patch.dict(sys.modules,{'homeplane.embodied.camera_recognition':SimpleNamespace(CameraRecognition=rec_factory)}),\
         patch.object(asyncio,'create_subprocess_exec',AsyncMock(return_value=process)):
        pool=previews.PreviewPool(continuous=False,recognize=True)
        item=dict(state='connecting',recognizer=None,process=None,shutter_changed=False,
                  expires=time.monotonic()+60,frames=0)
        try:await pool._run({'id':'cam_1111111111'},item)
        except ProcessLookupError:pass
    assert io.values==before and leases[0].lock_fd is None
    assert json.loads(leases[0].path.read_text())['status']=='restored'
    assert terminated and process.returncode is not None, 'SDK child survived failed vision cleanup while lease was released'
