from types import SimpleNamespace

import pytest

from homeplane.embodied.scenes import SceneCommands


class API:
    def __init__(self):self.runs=[];self.fail=False;self.version=1
    async def get_manual_scenes_async(self,homes):
        return {'scene':SimpleNamespace(home_id='home',scene_id='stable-id',scene_name='测试场景',
                    update_ts=self.version,enable=True,dids=['registered-device']),
                'foreign':SimpleNamespace(home_id='other',scene_id='foreign',scene_name='不在此家庭',update_ts=1,enable=True,dids=[])}
    async def run_manual_scene_async(self,scene):
        self.runs.append(scene.scene_id)
        if self.fail:raise TimeoutError()
        return True


@pytest.mark.asyncio
async def test_live_stable_ids_scope_scene_and_ack_never_claims_physical_effect(tmp_path):
    api=API();owner=SceneCommands(api,{'home':object()},tmp_path,region='synthetic')
    listing=await owner.listing();assert len(listing['scenes'])==1 and not api.runs
    scene=listing['scenes'][0];result=await owner.run(scene['id'],scene['revision'],'click')
    assert result['status']=='accepted' and result['physically_confirmed'] is False
    assert await owner.run(scene['id'],scene['revision'],'click')==result
    assert api.runs==['stable-id']


@pytest.mark.asyncio
async def test_changed_or_unknown_scene_cannot_run_and_timeout_is_not_replayed(tmp_path):
    api=API();owner=SceneCommands(api,{'home':object()},tmp_path,region='synthetic')
    scene=(await owner.listing())['scenes'][0];api.version=2
    with pytest.raises(ValueError):await owner.run(scene['id'],scene['revision'],'stale')
    with pytest.raises(ValueError):await owner.run('0'*64,scene['revision'],'foreign')
    assert not api.runs
    api.version=1;api.fail=True
    result=await owner.run(scene['id'],scene['revision'],'uncertain');assert result['status']=='unknown_outcome'
    assert await owner.run(scene['id'],scene['revision'],'uncertain')==result
    assert len(api.runs)==1


@pytest.mark.asyncio
async def test_console_scene_schema_and_validated_request_share_one_dispatch(tmp_path):
    import asyncio
    import warnings

    import httpx
    from fastapi import FastAPI

    from homeplane.embodied.api import create_router

    api=API();owner=SceneCommands(api,{'home':object()},tmp_path,region='synthetic')
    async def scenes(**request):
        return await owner.run(request['scene'],request['revision'],request['event_id']) if request else await owner.listing()
    app=FastAPI();app.include_router(create_router(SimpleNamespace(scenes=scenes),SimpleNamespace(),asyncio.Lock()))
    with warnings.catch_warnings():
        warnings.filterwarnings('error',message='Duplicate Operation ID')
        schema=app.openapi()
    assert schema['paths']['/api/embodied/scenes'].keys()=={'get'}
    assert schema['paths']['/api/embodied/scenes/run'].keys()=={'post'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        listing=(await client.get('/api/embodied/scenes')).json()
        scene=listing['scenes'][0]
        request=dict(scene=scene['id'],revision=scene['revision'],event_id='synthetic-click')
        assert (await client.post('/api/embodied/scenes/run',json={**request,'unreviewed':True})).status_code==422
        assert not api.runs
        first=await client.post('/api/embodied/scenes/run',json=request)
        assert first.status_code==200 and first.json()['physically_confirmed'] is False
        assert (await client.post('/api/embodied/scenes/run',json=request)).json()==first.json()
        assert api.runs==['stable-id']
