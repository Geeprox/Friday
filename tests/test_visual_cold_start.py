"""Cold process preparation must not age the selected evidence or bypass cadence."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock,AsyncMock

import pytest

from homeplane.embodied.temporal_model import VisualResult
from tests.test_visual_followup import Home
from tests.embodied.test_review_motion import Patrol


class ColdVisual:
    def __init__(self, clock, *, warmup=8, inference=10):
        self.clock,self.warmup,self.inference=clock,warmup,inference
        self.ready=False;self.pending=None
        self.ready_at=[];self.inputs=[];self.active=self.maximum=0

    async def wait(self,seconds,stage):
        future=asyncio.get_running_loop().create_future();self.pending=(self.clock()+seconds,future,stage)
        try:
            await future
        finally:
            if self.pending and self.pending[1]is future:self.pending=None

    async def call(self,window):
        self.active+=1;self.maximum=max(self.maximum,self.active)
        try:
            if not self.ready:
                await self.wait(self.warmup,'warmup');self.ready=True;self.ready_at.append(self.clock())
            frames=window();self.inputs.append((self.clock(),tuple(frames)))
            await self.wait(self.inference,'inference')
            return VisualResult(event='face_obscured',observation='合成可见变化')
        except asyncio.CancelledError:
            await self.close();raise
        finally:
            self.active-=1

    async def analyze(self,frames):
        return await self.call(lambda:frames)

    async def analyze_latest(self,window):
        return await self.call(window)

    def release_due(self):
        if self.pending and self.clock()>=self.pending[0]and not self.pending[1].done():
            _,future,stage=self.pending;future.set_result(None)
            # The existing replay clock may advance through preparation while
            # the same job remains alive; only inference completes that job.
            return stage=='inference'
        return False

    async def close(self):
        self.ready=False
        if self.pending and not self.pending[1].done():self.pending[1].cancel()


def cold_home(tmp_path,monkeypatch,**options):
    home=Home(tmp_path,monkeypatch)
    engine=ColdVisual(lambda:home.clock,**options)
    home.engine=home.runtime.visual.engine=engine
    return home,engine


@pytest.mark.asyncio
@pytest.mark.parametrize('owner',['primary','secondary'])
async def test_cold_start_uses_recent_frames_and_still_corroborates_in_original_patrol(tmp_path,monkeypatch,owner):
    home,engine=cold_home(tmp_path,monkeypatch);patrol=Patrol(home,owner)
    try:
        await patrol.run()
        events=home.store.events()
        assert events, (owner,'cold startup consumed evidence freshness',engine.ready_at,
                        [(at,frames[-1][0])for at,frames in engine.inputs],patrol.moves)
        submitted,frames=engine.inputs[0]
        assert 0<=submitted-frames[-1][0]<=1.5
        assert frames[-1][0]>=engine.ready_at[0]-1.5
        assert all(len(event['frames'])==8 for event in events)and engine.maximum==1
        assert patrol.moves
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_minimum_inference_gap_is_measured_after_startup_at_actual_frame_submission(tmp_path,monkeypatch):
    home,engine=cold_home(tmp_path,monkeypatch,inference=.1);home.feeds=home.feeds[:1]
    try:
        for index in range(1,81):await home.tick(index)
        starts=[at for at,_ in engine.inputs]
        assert len(starts)>=3
        assert all(b-a>=6 for a,b in zip(starts,starts[1:])),starts
        assert engine.maximum==1
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',[False,True])
async def test_actual_process_adapter_defers_the_window_until_ready_and_closes_revoked_input(tmp_path,monkeypatch,revoke):
    from homeplane.embodied import temporal_model as model
    from tests.test_temporal_review import FakeProcess
    entered,ready=asyncio.Event(),asyncio.Event();created=[];calls=[]
    class Waiting(FakeProcess):
        def __init__(self):super().__init__();self.writes=[]
        async def readline(self):
            if self.reads==0:entered.set();await ready.wait()
            return await super().readline()
        def write(self,data):self.writes.append(data)
    async def spawn(*_,**__):
        child=Waiting();created.append(child);return child
    monkeypatch.setattr(model,'model_readiness',lambda _:dict(available=True))
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    owner=model.MlxWindowProcess(tmp_path,tmp_path,budget=lambda:model.HostBudget(16*model.GIB,50))
    frames=[(float(i),b'old')for i in range(4)]
    def selected():
        calls.append(True)
        if revoke:raise model.VisualSourceChanged('visual_source_changed')
        return frames
    task=asyncio.create_task(owner.analyze_latest(selected))
    try:
        await asyncio.wait_for(entered.wait(),1)
        assert not calls and not created[0].writes
        frames=[(10.+i,('fresh-'+str(i)).encode())for i in range(4)];ready.set()
        if revoke:
            with pytest.raises(model.VisualSourceChanged):await task
            assert not created[0].writes and owner.process is owner.lease is None
            assert created[0].returncode==0
        else:
            assert (await task).event=='none'
            payload=json.loads(created[0].writes[0])
            assert [base64.b64decode(f['jpeg'])for f in payload['frames']]==[f[1]for f in frames]
            assert [f['t']for f in payload['frames']]==[0,1,2,3]
        assert calls==[True]
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reply',[b'',b'{}\n',b'{"version":1,"ready":1}\n',b'{"version":true,"ready":true}\n',
                                  b'{"version":1,"ready":true,"extra":0}\n',b'x'*129+b'\n'])
async def test_process_start_or_invalid_ready_cannot_authorize_an_image_request(tmp_path,monkeypatch,reply):
    from homeplane.embodied import temporal_model as model
    from tests.test_temporal_review import FakeProcess
    child=FakeProcess();child.readline=AsyncMock(return_value=reply)
    child.write=Mock()
    async def spawn(*_,**__):return child
    monkeypatch.setattr(model,'model_readiness',lambda _:dict(available=True))
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    owner=model.MlxWindowProcess(tmp_path,tmp_path,budget=lambda:model.HostBudget(16*model.GIB,50));select=Mock()
    try:
        with pytest.raises(ValueError):await owner.analyze_latest(select)
        select.assert_not_called();child.write.assert_not_called()
        assert owner.process is owner.lease is None and child.returncode==0
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['clear','policy','stale'])
async def test_source_or_policy_changes_during_cold_start_never_reach_inference(tmp_path,monkeypatch,change):
    home,engine=cold_home(tmp_path,monkeypatch);home.feeds=home.feeds[:1];camera=home.feeds[0][1]
    try:
        for index in range(1,9):await home.tick(index)
        assert engine.pending and engine.pending[2]=='warmup' and not engine.inputs
        if change=='clear':home.runtime.clear(camera)
        elif change=='policy':
            home.store.configure(home.store.policy().model_copy(update={'visual_review':False}))
            await home.runtime.sync_policy()
        home.excluded.add(camera)
        for index in range(9,34):await home.tick(index)
        assert not engine.inputs and not home.store.events()
        assert home.runtime.visual.failures==0
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


def test_real_worker_emits_ready_only_after_qualified_load_and_keeps_diagnostics_off_wire(tmp_path,monkeypatch):
    import hashlib
    import sys
    from types import ModuleType
    from homeplane.embodied import temporal_worker as worker,camera_guardian
    data=b'SYNTHETIC-NOT-MODEL-WEIGHTS';(tmp_path/'model.safetensors').write_bytes(data)
    calls=[]
    monkeypatch.setattr(worker,'model_readiness',lambda _:dict(available=True))
    monkeypatch.setattr(worker,'WEIGHT_SHA256',hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(camera_guardian,'exit_with_owner',lambda _:calls.append('owner'))
    for name in ('mlx','mlx.core','mlx_vlm','mlx_vlm.generate','mlx_vlm.generate.video'):
        module=ModuleType(name);module.__path__=[];monkeypatch.setitem(sys.modules,name,module)
    core=sys.modules['mlx.core'];core.set_memory_limit=core.set_cache_limit=lambda _:None
    sys.modules['mlx'].core=core
    def load(*_,**kwargs):
        assert kwargs==dict(trust_remote_code=False,local_files_only=True)
        calls.append('load');print('SYNTHETIC LOAD PROGRESS');return object(),object()
    sys.modules['mlx_vlm'].load=load
    sys.modules['mlx_vlm'].generate=Mock(side_effect=AssertionError('no inference before a window'))
    sys.modules['mlx_vlm.generate.video'].timestamped_frame_messages=Mock()
    output,error=io.StringIO(),io.StringIO()
    monkeypatch.setattr(sys,'stdout',output);monkeypatch.setattr(sys,'stderr',error)
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO()))
    worker.main(tmp_path)
    assert calls==['owner','load']
    assert output.getvalue()=='{"version": 1, "ready": true}\n'
    assert 'SYNTHETIC LOAD PROGRESS' in error.getvalue()


@pytest.mark.asyncio
async def test_eight_patrolling_cameras_still_corroborate_with_repeated_cold_model_loads(tmp_path,monkeypatch):
    from tests.embodied.test_review_motion import patrol_fleet
    home,engine=cold_home(tmp_path,monkeypatch)
    try:
        directors=await patrol_fleet(home,steps=720)
        events=home.store.events()
        assert {event['camera']for event in events}==set(directors)
        assert all(len(event['frames'])==8 for event in events)and engine.maximum==1
        starts=[at for at,_ in engine.inputs]
        assert all(b-a>=6 for a,b in zip(starts,starts[1:]))
        assert len(engine.ready_at)>1
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()
