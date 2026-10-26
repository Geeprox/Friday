import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from homeplane.embodied import service as module

@pytest.mark.asyncio
@pytest.mark.parametrize('restore_success',[True,False])
async def test_stop_compensates_every_owned_camera_without_touching_another_process(tmp_path,monkeypatch,restore_success):
    cameras=['cam_1111111111','cam_2222222222','cam_3333333333']
    monkeypatch.setattr(module,'PRIVATE',tmp_path)
    monkeypatch.setattr(module,'configuration',lambda:{'cameras':[{'id':c} for c in cameras]})
    for i,camera in enumerate(cameras):
        path=tmp_path/'cameras'/camera/'ptz-lease.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'pid':1234 if i!=1 else 9999,'status':'owned'}))
    attempts=[]
    async def repair(*args,**kwargs):
        camera=kwargs['env']['FRIDAY_CAMERA_ID'];attempts.append(camera)
        path=tmp_path/'cameras'/camera/'ptz-lease.json'
        row=json.loads(path.read_text());row['status']='restored' if restore_success or camera==cameras[2] else 'restore_pending';path.write_text(json.dumps(row))
        return SimpleNamespace(wait=AsyncMock(return_value=0),kill=lambda:None)
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',repair)
    owner=module.EmbodiedService();owner.process=SimpleNamespace(pid=1234,returncode=1)
    owner.selected=cameras[0]
    try:
        if restore_success:assert (await owner.stop())['restored']
        else:
            with pytest.raises(ValueError,match='restore_pending'):await owner.stop()
        assert attempts==[cameras[0],cameras[2]]
        assert json.loads((tmp_path/'cameras'/cameras[1]/'ptz-lease.json').read_text())['status']=='owned'
    finally:await owner.client.aclose()
