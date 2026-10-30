import pytest

from homeplane.embodied.camera_power import PowerController

SELECTED={'id':'cam_aaaaaaaaaa','device':{'did':'synthetic-registered-device'}}


class IO:
    def __init__(self):self.enabled=False;self.writes=[];self.fail=False
    async def read(self):return self.enabled
    async def write(self,value):
        self.writes.append(value)
        if self.fail:raise TimeoutError()
        self.enabled=value


@pytest.mark.asyncio
async def test_read_is_passive_and_open_close_are_absolute_scoped_and_idempotent(tmp_path):
    io=IO();owner=PowerController(SELECTED,io,tmp_path)
    before=await owner.status();assert before['enabled'] is False and io.writes==[]
    opened=await owner.set(True,before['revision'],'explicit-open')
    assert opened['status']=='reported_match' and opened['physically_confirmed'] is False
    assert await owner.set(True,before['revision'],'explicit-open')==opened
    assert io.writes==[True]
    await owner.set(False,opened['revision'],'explicit-close');assert io.writes==[True,False]


@pytest.mark.asyncio
async def test_stale_state_foreign_camera_and_event_reuse_do_not_send(tmp_path):
    io=IO();owner=PowerController(SELECTED,io,tmp_path);before=await owner.status()
    other=PowerController(dict(SELECTED,id='cam_bbbbbbbbbb'),io,tmp_path)
    with pytest.raises(ValueError):await other.set(True,before['revision'],'foreign-camera')
    await owner.set(True,before['revision'],'event')
    with pytest.raises(ValueError):await owner.set(False,before['revision'],'event')
    with pytest.raises(ValueError):await owner.set(False,before['revision'],'stale-close')
    assert io.writes==[True]


@pytest.mark.asyncio
async def test_lost_ack_is_durable_unknown_and_never_retried(tmp_path):
    io=IO();io.fail=True;owner=PowerController(SELECTED,io,tmp_path);before=await owner.status()
    result=await owner.set(True,before['revision'],'uncertain')
    assert result['status']=='unknown_outcome' and io.writes==[True]
    reopened=PowerController(SELECTED,io,tmp_path)
    assert await reopened.set(True,before['revision'],'uncertain')==result
    assert io.writes==[True]


@pytest.mark.asyncio
async def test_unknown_readback_or_untyped_power_never_opens(tmp_path):
    io=IO();owner=PowerController(SELECTED,io,tmp_path);before=await owner.status()
    with pytest.raises(ValueError):await owner.set(1,before['revision'],'wrong-type')
    io.enabled=None
    with pytest.raises(ValueError):await owner.set(True,before['revision'],'unknown-state')
    assert io.writes==[]


@pytest.mark.asyncio
async def test_manual_preview_selects_one_camera_without_recognition_or_audio(monkeypatch,capsys):
    import io
    from homeplane.embodied import manual_preview
    calls=[]
    class Pool:
        def __init__(self,**options):calls.append(options)
        async def start(self,camera):calls.append(camera);return {'state':'connecting'}
        async def close(self):calls.append('closed')
    monkeypatch.setattr(manual_preview,'camera',lambda value:dict(id=value))
    monkeypatch.setattr(manual_preview.sys,'stdin',type('Input',(),{'buffer':io.BytesIO(b'{"action":"start","camera":"cam_aaaaaaaaaa"}\n')})())
    await manual_preview.worker(Pool)
    assert calls==[{'recognize':False,'control_camera':True},'cam_aaaaaaaaaa','closed']
    assert 'connecting' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_only_explicit_view_may_use_manual_preview_while_background_is_paused(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from homeplane.embodied import service
    owner=service.EmbodiedService();owner.capture_enabled=False
    command=AsyncMock(return_value={'state':'connecting'})
    owner.manual_previews=SimpleNamespace(has=lambda _:False,command=command,close=AsyncMock())
    monkeypatch.setattr(service,'camera',lambda _:SELECTED)
    monkeypatch.setattr(owner,'configured',lambda:True)
    try:
        with pytest.raises(ValueError,match='camera_capture_paused'):
            await owner.preview(SELECTED['id'],start=True)
        assert not command.called
        result=await owner.preview(SELECTED['id'],start=True,explicit=True)
        assert result['recognition_enabled'] is False and owner.capture_enabled is False
        command.assert_awaited_once_with('start',SELECTED['id'])
    finally:await owner.close()
