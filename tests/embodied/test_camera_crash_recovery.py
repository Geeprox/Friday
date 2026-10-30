"""Real process death and OS locks; all camera I/O is an isolated JSON fixture."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import service as services
from homeplane.embodied.ptz_control import CameraLease, FLAGS, restore_camera
from homeplane.embodied.camera_guardian import check_guardian, monitor_owner, watch_owner


FAKE_IO = '''
import asyncio, json, os, sys
from pathlib import Path
from homeplane.embodied.ptz_control import CameraLease, restore_camera
root=Path(sys.argv[1]); camera_id='cam_1111111111'
path=root/'cameras'/camera_id/'ptz-lease.json'
class Camera:
    def __init__(self,*_):pass
    async def read(self):
        if (root/'offline').exists():raise OSError('synthetic_offline')
        return json.loads((root/'device.json').read_text())
    async def write(self,name,value):
        state=await self.read();state[name]=value
        (root/'device.json').write_text(json.dumps(state))
        with (root/'writes.jsonl').open('a') as stream:stream.write(json.dumps([name,value])+'\\n')
    async def close(self):pass
'''
OWNER = FAKE_IO + '''
async def main():
    lease=CameraLease(Camera(),path)
    await lease.acquire()
    print('owned',flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
'''
GUARDIAN = FAKE_IO + '''
from homeplane.embodied.camera_guardian import monitor_owner
async def repair(camera,**kwargs):
    (root/'guardian-repair-started').touch()
    return await restore_camera(camera,transport_factory=Camera,**kwargs)
async def main():
    print('ready',flush=True)
    await monitor_owner(int(sys.argv[2]),sys.argv[3] if len(sys.argv)>3 else '1'*32,
                        [camera_id],root=root,repair=repair,interval=.02)
    (root/'guardian-done').touch()
asyncio.run(main())
'''


async def eventually(predicate):
    for _ in range(300):
        if predicate():return
        await asyncio.sleep(.01)
    raise AssertionError('synthetic recovery did not complete')


@pytest.mark.asyncio
@pytest.mark.parametrize('closed,offline',[(False,False),(True,False),(True,True)])
async def test_sigkill_restores_exact_baseline_without_opening_shutter(tmp_path,closed,offline):
    folder=tmp_path/'cameras'/'cam_1111111111';folder.mkdir(parents=True)
    path=folder/'ptz-lease.json'
    original=dict(camera_power=True,cruise=True,human_tracking=False,pet_tracking=True,
                  human_close_up=True,active_preset=1,presets=[])
    (tmp_path/'device.json').write_text(json.dumps(original))
    read_fd,write_fd=os.pipe();owner=guardian=None
    try:
        guardian=subprocess.Popen([sys.executable,'-B','-c',GUARDIAN,str(tmp_path),str(read_fd)],
            pass_fds=(read_fd,),stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        assert await asyncio.to_thread(guardian.stdout.readline)==b'ready\n'
        owner=subprocess.Popen([sys.executable,'-B','-c',OWNER,str(tmp_path)],
            env=dict(os.environ,FRIDAY_CAMERA_OWNER='1'*32),pass_fds=(write_fd,),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        os.close(read_fd);read_fd=None;os.close(write_fd);write_fd=None
        assert await asyncio.to_thread(owner.stdout.readline)==b'owned\n'
        assert not (tmp_path/'guardian-repair-started').exists(), 'No guardian device access while owner lives'
        changed=json.loads((tmp_path/'device.json').read_text())
        assert all(changed[name] is False for name in FLAGS)
        if closed:changed['camera_power']=False
        changed['active_preset']=2
        (tmp_path/'device.json').write_text(json.dumps(changed))
        if offline:(tmp_path/'offline').touch()
        owner.kill();await asyncio.to_thread(owner.wait,5)
        if offline:
            await eventually(lambda:json.loads(path.read_text())['status']=='restore_pending')
            assert json.loads(path.read_text())['before']['pet_tracking'] is True
            assert guardian.poll() is None
            (tmp_path/'offline').unlink()
        stdout,stderr=await asyncio.to_thread(guardian.communicate,timeout=5)
        assert guardian.returncode==0,stderr.decode()
        result=json.loads((tmp_path/'device.json').read_text())
        assert {k:result[k] for k in FLAGS}=={k:original[k] for k in FLAGS}
        assert result['camera_power'] is not closed
        assert result['active_preset']==(2 if closed else 1)
        assert json.loads(path.read_text())['status']=='restored'
        writes=[json.loads(row) for row in (tmp_path/'writes.jsonl').read_text().splitlines()]
        assert ['camera_power',True] not in writes
    finally:
        for process in (owner,guardian):
            if process is not None:
                if process.poll() is None:process.kill()
                await asyncio.to_thread(process.communicate,timeout=5)
        for fd in (read_fd,write_fd):
            if fd is not None:os.close(fd)


@pytest.mark.asyncio
async def test_lock_fences_same_pid_and_stale_guardian(tmp_path):
    state=dict(camera_power=True,cruise=True,human_tracking=False,pet_tracking=True,
               human_close_up=False,active_preset=1,presets=[])
    writes=[]
    async def write(name,value):writes.append((name,value));state[name]=value
    io=SimpleNamespace(read=AsyncMock(side_effect=lambda:dict(state)),write=write,close=AsyncMock())
    path=tmp_path/'lease.json';first=CameraLease(io,path);second=CameraLease(io,path)
    try:
        await first.acquire();before=list(writes)
        with pytest.raises(RuntimeError,match='owned_by_live_process'):await second.acquire()
        with pytest.raises(RuntimeError,match='owned_by_live_process'):
            await restore_camera(path=path,owner_token='stale',transport_factory=lambda _:io)
        assert writes==before
        await first.release();writes.clear()
        record=json.loads(path.read_text());record.update(status='owned',owner_token='new_owner')
        path.write_text(json.dumps(record))
        assert await restore_camera(path=path,owner_token='stale',transport_factory=lambda _:io)
        assert not writes
    finally:first.unclaim();second.unclaim()


@pytest.mark.asyncio
async def test_legacy_pending_lease_restores_flags_despite_old_disabled_policy(tmp_path):
    state=dict(camera_power=False,cruise=False,human_tracking=False,pet_tracking=False,human_close_up=False)
    before=dict(state,cruise=True,pet_tracking=True)
    path=tmp_path/'lease.json'
    path.write_text(json.dumps(dict(version=1,pid=-1,status='owned',restore_native=False,
                                   before=before,return_preset=1)))
    writes=[]
    async def write(name,value):writes.append((name,value));state[name]=value
    io=SimpleNamespace(read=AsyncMock(side_effect=lambda:dict(state)),write=write,close=AsyncMock())
    assert await restore_camera(path=path,transport_factory=lambda _:io)
    assert state['cruise'] and state['pet_tracking']
    assert not state['camera_power'] and not any(name=='active_preset' for name,_ in writes)


@pytest.mark.asyncio
async def test_worker_starts_only_after_guardian_ready_with_exclusive_lifetime_fd(monkeypatch):
    calls=[]
    guard=SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(return_value=b'ready\n')))
    worker=SimpleNamespace(returncode=None)
    async def spawn(*args,**kwargs):
        for fd in kwargs['pass_fds']:os.fstat(fd)
        calls.append((args,kwargs));return guard if len(calls)==1 else worker
    monkeypatch.setattr(services.asyncio,'create_subprocess_exec',spawn)
    service=services.EmbodiedService()
    try:
        await service._spawn_guarded_worker({'SYNTHETIC':'1'})
        assert service.process is worker and service.guardian is guard
        assert calls[0][0][3]=='homeplane.embodied.camera_guardian'
        assert calls[1][0][3]=='homeplane.embodied.worker'
        assert all(call[1]['start_new_session'] for call in calls)
        assert len(calls[1][1]['env']['FRIDAY_CAMERA_OWNER'])==32
        for _,kwargs in calls:
            for fd in kwargs['pass_fds']:
                with pytest.raises(OSError):os.fstat(fd)
    finally:await service.client.aclose()


@pytest.mark.asyncio
async def test_entire_service_crash_still_releases_worker_and_restores_camera(tmp_path):
    """The guardian outlives its service parent; no service finally is available."""
    folder=tmp_path/'cameras'/'cam_1111111111';folder.mkdir(parents=True)
    original=dict(camera_power=True,cruise=True,human_tracking=False,pet_tracking=True,
                  human_close_up=True,active_preset=1,presets=[])
    (tmp_path/'device.json').write_text(json.dumps(original))
    worker_code=FAKE_IO+'''
from homeplane.embodied.camera_guardian import watch_owner
async def main():
    lease=CameraLease(Camera(),path)
    await lease.acquire()
    (root/'worker-owned').touch()
    done=asyncio.Event()
    task=asyncio.create_task(watch_owner(os.getppid(),done.set,interval=.01))
    await done.wait()
    await task
    # Intentionally omit release: independent recovery must handle this too.
    (root/'worker-exited').touch()
asyncio.run(main())
'''
    service_code=FAKE_IO+f'''
from homeplane.embodied.service import EmbodiedService
real_spawn=asyncio.create_subprocess_exec
async def spawn(*args,**kwargs):
    if args[3]=='homeplane.embodied.camera_guardian':
        args=(sys.executable,'-B','-c',{GUARDIAN!r},str(root),args[5],args[7])
    else:
        assert args[3]=='homeplane.embodied.worker'
        args=(sys.executable,'-B','-c',{worker_code!r},str(root))
    return await real_spawn(*args,**kwargs)
asyncio.create_subprocess_exec=spawn
async def main():
    service=EmbodiedService()
    await service._spawn_guarded_worker(dict(os.environ))
    print('service-ready',flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
'''
    process=subprocess.Popen([sys.executable,'-B','-c',service_code,str(tmp_path)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        assert await asyncio.to_thread(process.stdout.readline)==b'service-ready\n'
        await eventually(lambda:(tmp_path/'worker-owned').exists())
        process.kill();await asyncio.to_thread(process.wait,5)
        await eventually(lambda:(tmp_path/'guardian-done').exists())
        assert (tmp_path/'worker-exited').exists()
        assert json.loads((tmp_path/'device.json').read_text())==original
        assert json.loads((folder/'ptz-lease.json').read_text())['status']=='restored'
    finally:
        if process.poll() is None:process.kill()
        await asyncio.to_thread(process.communicate,timeout=5)


@pytest.mark.asyncio
async def test_new_service_recovers_pending_lease_without_old_process_object(tmp_path,monkeypatch):
    camera='cam_1111111111';folder=tmp_path/'cameras'/camera;folder.mkdir(parents=True)
    path=folder/'ptz-lease.json';path.write_text(json.dumps({'pid':99999,'status':'owned'}))
    monkeypatch.setattr(services,'PRIVATE',tmp_path)
    monkeypatch.setattr(services,'configuration',lambda:{'cameras':[{'id':camera}]})
    async def repair(*_,**kwargs):
        path.write_text(json.dumps({'status':'restored'}))
        return SimpleNamespace(wait=AsyncMock(return_value=0))
    spawn=AsyncMock(side_effect=repair);monkeypatch.setattr(services.asyncio,'create_subprocess_exec',spawn)
    service=services.EmbodiedService()
    try:
        service.recovery_enabled=True
        assert service.process is None
        assert (await service.stop())['restored']
        spawn.assert_awaited_once()
        assert not service.recovery_pending
    finally:await service.client.aclose()


@pytest.mark.asyncio
async def test_failed_restore_keeps_baseline_and_retries_while_owner_is_still_alive(tmp_path):
    folder=tmp_path/'cameras'/'cam_1111111111';folder.mkdir(parents=True)
    path=folder/'ptz-lease.json'
    original=dict(camera_power=True,cruise=True,human_tracking=False,pet_tracking=True,
                  human_close_up=False,active_preset=1,presets=[])
    values=dict(original);offline=False;writes=[]
    async def read():
        if offline:raise OSError('synthetic_offline')
        return dict(values)
    async def write(name,value):writes.append((name,value));values[name]=value
    io=SimpleNamespace(read=read,write=write,close=AsyncMock())
    first=CameraLease(io,path);second=CameraLease(io,path)
    read_fd,write_fd=os.pipe();task=None
    try:
        await first.acquire()
        first.snapshot['owner_token']='1'*32
        offline=True
        assert not await first.release()
        baseline=json.loads(path.read_text())['before'];writes.clear()
        with pytest.raises(RuntimeError,match='previous_restore_pending'):await second.acquire()
        assert json.loads(path.read_text())['before']==baseline and not writes
        offline=False
        async def repair(camera,**kwargs):return await restore_camera(camera,transport_factory=lambda _:io,**kwargs)
        task=asyncio.create_task(monitor_owner(read_fd,'1'*32,['cam_1111111111'],root=tmp_path,repair=repair,interval=.01))
        read_fd=None
        await eventually(lambda:json.loads(path.read_text())['status']=='restored')
        assert not task.done(), 'Released-lease recovery must not wait for worker exit'
        assert values==original
        os.close(write_fd);write_fd=None
        await asyncio.wait_for(task,2)
    finally:
        first.unclaim();second.unclaim()
        if task is not None and not task.done():
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        for fd in (read_fd,write_fd):
            if fd is not None:os.close(fd)


@pytest.mark.asyncio
async def test_dead_guardian_revokes_worker_and_unadmitted_stop_has_no_io(monkeypatch):
    read_fd,write_fd=os.pipe()
    try:
        monkeypatch.setenv('FRIDAY_CAMERA_LIFETIME_FD',str(write_fd))
        check_guardian();assert os.read(read_fd,1)==b'.'
        os.close(read_fd);read_fd=None
        with pytest.raises(BrokenPipeError):check_guardian()
        exits=[];await watch_owner(os.getppid(),lambda:exits.append(True))
        assert exits==[True]
    finally:
        if read_fd is not None:os.close(read_fd)
        os.close(write_fd)
    monkeypatch.setattr(services,'configuration',lambda:(_ for _ in ()).throw(AssertionError('Simulator must not inspect household')))
    service=services.EmbodiedService()
    try:assert (await service.stop())['restored']
    finally:await service.client.aclose()


@pytest.mark.asyncio
async def test_orphaned_capture_exits_even_with_a_blocked_executor_thread():
    child='''
import os, threading
from homeplane.embodied.camera_guardian import exit_with_owner
exit_with_owner(os.getppid())
threading.Thread(target=lambda:threading.Event().wait(),daemon=False).start()
print('capture-ready',flush=True)
threading.Event().wait()
'''
    parent=f'''
import subprocess,sys,threading
subprocess.Popen([sys.executable,'-B','-c',{child!r}])
threading.Event().wait()
'''
    process=subprocess.Popen([sys.executable,'-B','-c',parent],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        assert await asyncio.to_thread(process.stdout.readline)==b'capture-ready\n'
        process.kill()
        # The inherited output pipe closes only when the orphan exits. A hung
        # non-daemon SDK/executor thread would otherwise retain it indefinitely.
        await asyncio.to_thread(process.communicate,timeout=3)
    finally:
        if process.poll() is None:process.kill()
        await asyncio.to_thread(process.communicate,timeout=3)
