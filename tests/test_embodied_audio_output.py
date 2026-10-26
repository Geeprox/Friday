import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from homeplane.embodied import airplay, audio_output


@pytest.fixture
def endpoint(tmp_path,monkeypatch):
    config={'speech_output':'panel','version':1,'cameras':['SYNTHETIC']}
    monkeypatch.setattr(audio_output,'PRIVATE',tmp_path)
    monkeypatch.setattr(airplay,'PRIVATE',tmp_path)
    monkeypatch.setattr(audio_output,'configuration',lambda:config)
    router=APIRouter();audio_output.install(router);app=FastAPI();app.include_router(router)
    return app,config,tmp_path


@pytest.mark.asyncio
async def test_reading_settings_is_silent_and_never_discovers_or_opens_receiver(endpoint,monkeypatch):
    app,_,_=endpoint
    discover=AsyncMock(side_effect=AssertionError('status must stay local'))
    monkeypatch.setattr(airplay,'discover',discover)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        result=(await client.get('/api/embodied/speech-output')).json()
    assert result['output']=='panel' and not result['homepod']['configured']
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_picker_excludes_mac_tv_and_other_speakers(endpoint,monkeypatch):
    app,_,_=endpoint
    discover=AsyncMock(return_value=[{'homepod':False,'name':'Mac'},{'homepod':True,'name':'HomePod'}])
    monkeypatch.setattr(airplay,'discover',discover)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        response=await client.get('/api/embodied/speech-output/devices?host=192.168.50.123')
    assert response.json()['devices']==[{'homepod':True,'name':'HomePod'}]
    discover.assert_awaited_once_with(8,host='192.168.50.123')


@pytest.mark.asyncio
async def test_selection_requires_exact_identity_and_known_output_schema(endpoint,monkeypatch):
    app,_,_=endpoint;select=AsyncMock();monkeypatch.setattr(airplay,'select',select)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        for body in [{'output':'homepod'},{'output':'homepod','identifier':'pod','host':'outside.invalid'},
                     {'output':'anything'},{'output':'panel','identifier':'pod'},
                     {'output':'homepod','identifier':'pod','credentials':'secret'}]:
            assert (await client.post('/api/embodied/speech-output',json=body)).status_code==422
        assert (await client.post('/api/embodied/speech-output',json={'output':'homepod','identifier':'pod','host':'192.168.50.123'})).status_code==200
    select.assert_awaited_once_with('pod',host='192.168.50.123')


@pytest.mark.asyncio
async def test_return_to_existing_panel_preserves_cameras_and_quiet(endpoint):
    app,_,private=endpoint;(private/'voice-paused').touch()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        assert (await client.post('/api/embodied/speech-output',json={'output':'panel'})).status_code==200
    assert (private/'voice-paused').exists()
    assert json.loads((private/'config.json').read_text())['cameras']==['SYNTHETIC']


@pytest.mark.asyncio
async def test_homepod_test_respects_quiet_before_any_network_or_synthesis(endpoint,monkeypatch):
    app,config,private=endpoint;config['speech_output']='homepod';(private/'voice-paused').touch()
    monkeypatch.setattr(airplay,'status',lambda:pytest.fail('quiet test must not inspect receiver'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        result=await client.post('/api/embodied/speech-output/test',json={})
    assert result.json()=={'status':'paused'}


@pytest.mark.asyncio
async def test_homepod_test_does_not_play_on_a_different_selected_output(endpoint,monkeypatch):
    app,_,_=endpoint;play=AsyncMock();monkeypatch.setattr(airplay,'play',play)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        assert (await client.post('/api/embodied/speech-output/test',json={})).status_code==409
    play.assert_not_awaited()
