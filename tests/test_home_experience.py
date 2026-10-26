"""Product behavior at the real action boundary, using synthetic upstream IO."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from homeplane.home_experience import HomeExperience,create_router,room_name
from homeplane.recordings import Recordings,listing
from homeplane.embodied.interaction import Configuration,Binding,InteractionEngine,save
from fastapi import FastAPI
from test_homeplane_device_controls import rig

class Embodied:
    def configured(self):return False
    def cameras(self):return []

class Runtime:
    mode_deadline=0
    async def set_mode(self,*a):return {}
    async def set_execution(self,*a,**kw):return {}
    def status(self):return {}

@pytest.mark.asyncio
async def test_product_controls_execute_once_without_second_approval(tmp_path):
    core,rows,calls,fault,path,authority=rig(tmp_path)
    settings=SimpleNamespace(path=tmp_path/'settings.json',config=SimpleNamespace(nas_url=None,nas_path=None))
    app=FastAPI();app.include_router(create_router(core,asyncio.Lock(),settings,Embodied(),None,Runtime()))
    try:
        snapshot=await core.snapshot();target=next(e for e in snapshot.entities if e.kind=='light')
        payload=dict(target=target.id,operation='light.set',desired={'power':'on'},expected_state_version=target.state_version,event_id='synthetic-product-toggle')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test')as client:
            first=await client.post('/api/home/control',json=payload)
            second=await client.post('/api/home/control',json=payload)
            assert first.status_code==second.status_code==200
            assert first.json()['status']=='reported_match'
            assert first.json()['action_id']==second.json()['action_id']
            assert len(calls)==1
            bad=await client.post('/api/home/control',json={**payload,'target':'unregistered','event_id':'synthetic-bad-target'})
            assert bad.json()['status']=='rejected'and len(calls)==1
    finally:core.actions.close();await core.adapter.close();core.store.close()

@pytest.mark.asyncio
async def test_overview_reports_latched_values_and_normalized_rooms(tmp_path):
    core,*_=rig(tmp_path);settings=SimpleNamespace(path=tmp_path/'settings.json')
    owner=HomeExperience(core,settings,Embodied(),None,Runtime())
    try:
        result=await owner.overview()
        assert result['devices']and result['control_count']>0
        assert all(isinstance(d['room'], str) for d in result['devices'])
        assert room_name('Imported area name')=='Imported area name'
        assert any(c['available']for d in result['devices']for c in d['controls'])
    finally:core.actions.close();await core.adapter.close();core.store.close()

@pytest.mark.asyncio
async def test_original_recording_range_revision_and_symlink_fence(tmp_path):
    root=tmp_path/'nas';folder=root/'XiaomiCamera_00_123456ABCDEF';folder.mkdir(parents=True)
    original=folder/'00_20260101000000_20260101001000.mp4';original.write_bytes(b'original-synthetic-video')
    outside=tmp_path/'outside.mp4';outside.write_bytes(b'secret-not-a-recording')
    (folder/'00_20260101001000_20260101002000.mp4').symlink_to(outside)
    report=listing(str(root));assert len(report['files'])==1
    archive=SimpleNamespace(_root=AsyncMock(return_value=str(root)))
    owner=Recordings(archive);owner.files['a'*32]={**report['files'][0],'root':str(root)}
    plan=await owner.prepare('a'*32);token=plan['url'].rsplit('/',1)[1]
    chunks,headers,status=owner.media(token,'bytes=2-6');assert b''.join(chunks)==b'igina';assert status==206
    original.write_bytes(b'changed')
    with pytest.raises(ValueError,match='source_changed'):owner.media(token,'bytes=0-1')


def test_binding_scope_rejects_unknown_targets_and_requires_point_calibration(tmp_path,monkeypatch):
    import homeplane.embodied.interaction as module
    monkeypatch.setattr(module,'PRIVATE',tmp_path)
    b=dict(id='a'*32,camera='cam_1234567890',gesture='hand.point',target='lamp',action='on',enabled=True)
    with pytest.raises(ValueError,match='calibration'):save(Configuration(revision=0,bindings=[Binding(**b)]),{'lamp':{'kind':'light','capabilities':['light.set']}},{'cam_1234567890'})
    b['anchor']=[.8,.5]
    with pytest.raises(ValueError,match='invalid_interaction'):save(Configuration(revision=0,bindings=[Binding(**b)]),{}, {'cam_1234567890'})
    saved=save(Configuration(revision=0,enabled=True,bindings=[Binding(**b)]),{'lamp':{'kind':'light','capabilities':['light.set']}},{'cam_1234567890'})
    engine=InteractionEngine('cam_1234567890','test-session')
    people=[{'id':7,'joints':{}}];point={'person_id':7,'gesture':'pointing','ray':{'origin':[.1,.5],'direction':[1,0]}}
    assert not engine.update(people,[point],7,{'status':'locked'},True,10)
    assert not engine.update(people,[point],7,{'status':'locked'},True,10.9)
    assert engine.public['selection']=='lamp'
    event=engine.update(people,[{'person_id':7,'gesture':'ok'}],7,{'status':'locked'},True,11)
    assert len(event)==1 and event[0]['target']=='lamp'
    assert not engine.update(people,[{'person_id':7,'gesture':'ok'}],7,{'status':'locked'},True,11.1)
    assert not engine.update([{'id':8,'joints':{}}],[{'person_id':8,'gesture':'ok'}],8,{'status':'locked'},True,11.2)


def test_product_selects_physical_channels_instead_of_configuration_switches():
    from homeplane.home_experience import household_function
    def e(kind,name):return SimpleNamespace(kind=kind,name=name)
    lamp=e('light','餐吊灯 灯光')
    for name in ('餐吊灯 灯光 凌动开关','餐吊灯 * 通用开关设置 上电默认开灯状态',
                 '门锁 * WiFi自定义服务 包裹检测','面板 * 中控屏服务 显示按键名称；true为显示'):
        assert not household_function(e('switch',name),{},[lamp])
    assert household_function(lamp,{},[lamp])
    assert household_function(e('switch','客厅面板  电视背景墙射灯开关 开关'),{'kind':'light'},[])
    assert household_function(e('switch','厨房开关  开关 开关'),{},[])


def test_nas_worker_imports_from_owned_source_outside_repository(tmp_path):
    import subprocess,os
    from homeplane.worker_runtime import module_command
    root=tmp_path/'nas';root.mkdir()
    command=module_command('homeplane.settings','--scan-nas',str(root))
    result=subprocess.run(command,cwd=tmp_path,env={'PATH':os.defpath},capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and json.loads(result.stdout)['readable']is True
    result=subprocess.run(module_command('homeplane.recordings'),input=json.dumps({'root':str(root)}),cwd=tmp_path,env={'PATH':os.defpath},capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and json.loads(result.stdout)['files']==[]


def test_household_presentation_maps_central_air_without_changing_device_contract(tmp_path,monkeypatch):
    from homeplane import home_presentation as p
    monkeypatch.setattr(p,'PRIVATE',tmp_path)
    raw={'id':'ac-owner','kind':'climate','name':'米家壁挂机模型','room':'客厅',
         'bindings':[{'source':'xiaomi-identity','external_id':'cn_123'}],
         'controls':[{'id':'ac-control','kind':'climate','name':'空调','capabilities':[{'name':'climate.mode'}],'value':'cool'}]}
    profile=p.save(p.Profile(climate_style='ceiling_vent'),0)
    result=p.project(raw,profile)
    assert result['presentation']['fixture']=='ceiling_vent'
    assert result['controls'][0]['presentation']['label']=='中央空调 · 吊顶出风口'
    assert result['id']==raw['id']and result['bindings']==raw['bindings']
    assert raw.get('presentation')is None
    assert result['controls'][0]['capabilities']==raw['controls'][0]['capabilities']
    with pytest.raises(ValueError,match='changed'):p.save(profile,0)
    profile.overrides['ac-control']=p.Style(fixture='wall_unit')
    saved=p.save(profile,1)
    assert saved.revision==2 and p.project(raw,p.load())['presentation']['fixture']=='wall_unit'
    assert (tmp_path/'home-presentation.json').stat().st_mode&0o777==0o600


def test_presentation_separates_power_supply_from_emitting_light_and_panel_circuits():
    from homeplane import home_presentation as p
    profile=p.Profile()
    assert not p.appearance('light','主灯电源',profile)['emits_light']
    assert p.appearance('light','吊顶灯带',profile)['lighting_role']=='strip'
    assert p.appearance('light','餐吊灯',profile)['lighting_role']=='main'
    assert p.appearance('light','走廊筒灯',profile)['lighting_role']=='downlight'
    assert p.appearance('light','背景墙射灯',profile)['lighting_role']=='spotlight'
    profile.overrides['strip']=p.Style(fixture='led_strip')
    d={'id':'panel','name':'多路面板','kind':'light','controls':[
       {'id':'strip','name':'一路','kind':'light'},{'id':'down','name':'筒灯','kind':'light'}]}
    projected=p.project(d,profile)
    assert projected['presentation']['fixture']=='generic'
    assert [c['presentation']['lighting_role']for c in projected['controls']]==['strip','downlight']
