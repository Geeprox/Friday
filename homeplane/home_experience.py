"""Product-facing projection over existing home, camera and action owners.

Reported values remain separate from qualified measurements. This layer never
invents device authority, source freshness, room geometry or physical success.
"""
import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from .action_contracts import DevicePlanRequest
from .recordings import Recordings
from .preview_request import prepare_for_viewer
from . import home_presentation

HOME_NAME = '我的家'


def room_name(value):
    return (value or '').strip() or '其他'


def control_name(entity_name,device_name):
    if '  ' not in entity_name:return entity_name
    label=entity_name.split('  ',1)[1].strip()
    if label.endswith(' 开关'):label=label[:-3].strip()
    if label in {'开关','灯','灯光','空调','窗帘','风扇'}:return device_name
    return label if label.startswith(device_name) else f'{device_name} · {label}'


def household_function(entity, alias, functions):
    """Select the physical function for the product view, independently of authority."""
    if alias:
        return True
    name = entity.name.lower()
    if any(word in name for word in ('指示灯', 'indicator', '凌动', '延时', '防闪烁',
        '默认', '断电', '上电', '生物节律', '调光模式', '起夜', '初始化', '格式化',
        '恢复出厂', '电机反向', '屏幕', '显示屏', '禁麦', '睡眠模式', '弹窗', '音效')):
        return False
    if entity.kind in {'light', 'climate', 'cover', 'fan', 'lock','media_player','humidifier','water_heater'}:
        return True
    if entity.kind == 'switch':
        if ' * ' in entity.name or '门锁' in entity.name:
            return False
        if any(e.kind in {'light', 'climate', 'cover', 'fan', 'lock','humidifier','water_heater'}
               and household_function(e, {}, []) for e in functions):
            return False
        # MIoT generic properties on panels/locks are not physical power channels.
        parts = [p.strip() for p in entity.name.split('  ') if p.strip()]
        if len(parts) > 1:
            channel = parts[-1].split()
            return channel[-1] in {'开关', '左键', '右键', '中键'}
        return True
    return False


class Control(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: str = Field(min_length=1, max_length=256)
    operation: str = Field(min_length=1, max_length=48)
    desired: dict
    expected_state_version: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=128)


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    rooms: list[dict] | None = Field(default=None, max_length=16)
    recording_names: dict[str, str] | None = None
    automatic_running: bool | None = None


class LayoutInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    layout: dict
    expected_revision: int | None = Field(default=None,ge=0)
    accept_device_changes: bool = False

class DevicePosition(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=0)
    device_id:str=Field(min_length=1,max_length=128)
    room_id:str=Field(min_length=1,max_length=128)
    position:list[float]=Field(min_length=2,max_length=2)

class RecordingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    offset_seconds: float = Field(default=0, strict=True, ge=0, lt=600, allow_inf_nan=False)


class RecordingStopRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(pattern=r'^[a-f0-9]{48}$')


class PresentationEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    profile: home_presentation.Profile


class HomeExperience:
    def __init__(self, core, settings, embodied, archive, runtime):
        self.core, self.settings, self.embodied = core, settings, embodied
        self.runtime=runtime
        self.task=None
        self.closed=False
        self.path = settings.path.with_name('home-experience.json')
        self.recordings = Recordings(archive, directory=settings.path.parent / 'recording-previews')
        self.preferences = dict(revision=0, rooms=[], recording_names={},automatic_running=True)
        try:
            if self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size < 32768:
                self.preferences.update(json.loads(self.path.read_text()))
        except (OSError,ValueError,TypeError):pass

    def connection_health(self):
        from .simulator import Simulator
        if isinstance(self.core.adapter, Simulator):
            return {'transport': 'simulated', 'source': 'simulator', 'simulated': True,
                    'registry_available': False}
        return self.core.adapter.health()

    async def start(self):
        await self.runtime.set_execution(bool(self.preferences.get("automatic_running")))
        if self.task is None:self.task=asyncio.create_task(self._maintain())

    async def close(self):
        self.closed=True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
        await self.recordings.close()

    async def _maintain_once(self):
        import time
        remaining = self.runtime.mode_deadline - time.monotonic()
        modes = self.runtime.modes
        awake = set(modes) == {'home.sleep', 'home.manual'} and all(m.value is False for m in modes.values())
        current = self.runtime.mode_generation == self.runtime.manager.generation
        now = datetime.now(UTC)
        fresh = all(m.issued_at <= now < m.valid_until for m in modes.values())
        # Only extend a still-valid, explicitly awake window. An expired, unknown,
        # sleep or externally manual state is never interpreted as permission.
        if self.preferences.get("automatic_running") and awake and current and fresh and 0 < remaining <= 20:
            await self.runtime.renew_awake(modes)

    async def _maintain(self):
        while not self.closed:
            try:await self._maintain_once()
            except (ValueError,OSError):pass
            await asyncio.sleep(15)

    def save(self, edit, *, commit=True):
        if edit.expected_revision != self.preferences['revision']:
            raise ValueError('preferences_changed')
        updated = dict(self.preferences)
        if edit.rooms is not None:
            cleaned = []
            for row in edit.rooms:
                if set(row) != {'name', 'x', 'z', 'w', 'd'} or not isinstance(row['name'],str) or not 1 <= len(row['name']) <= 32:
                    raise ValueError('invalid_room')
                if any(type(row[k]) not in (int,float) or not -30 <= row[k] <= 30 for k in ('x','z')):
                    raise ValueError('invalid_room')
                if any(type(row[k]) not in (int,float) or not .5 <= row[k] <= 20 for k in ('w','d')):
                    raise ValueError('invalid_room')
                cleaned.append(row)
            updated['rooms'] = cleaned
        if edit.recording_names is not None:
            from .recordings import FOLDER
            if len(edit.recording_names) > 8 or any(not FOLDER.fullmatch(k) or not isinstance(v,str) or not 1 <= len(v) <= 64 for k,v in edit.recording_names.items()):
                raise ValueError('invalid_recording_name')
            updated['recording_names'] = edit.recording_names
        if edit.automatic_running is not None:updated['automatic_running']=edit.automatic_running
        updated['revision'] += 1
        if not commit:return updated
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix('.tmp')
        descriptor = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as f:
            json.dump(updated, f, ensure_ascii=False)
        os.replace(temp, self.path)
        self.preferences = updated
        return self.preferences

    async def overview(self):
        snapshot = await self.core.snapshot()
        entities = {e.id:e for e in snapshot.entities}
        states = {s.id:s for s in snapshot.states}
        observations = {(o.entity_id,o.capability):o for o in snapshot.observations}
        authorized = set(snapshot.action_targets)
        try:
            from .embodied.paths import PRIVATE
            aliases = json.loads((PRIVATE/'device-aliases.json').read_text())
        except (OSError,ValueError):
            aliases = {}
        from .embodied.paths import configuration
        try:camera_dids={str(c["device"]["did"]) for c in configuration()["cameras"]}
        except (OSError,ValueError,KeyError):camera_dids=set()
        devices=[]
        for device in snapshot.devices:
            if device.category == 'service' or any(b.source=='xiaomi-identity' and b.external_id.split('_')[-1] in camera_dids for b in device.bindings):
                continue
            functions=[entities[x] for x in device.function_ids if x in entities]
            # Internal per-property HA functions are not separate household items.
            primary = sorted(functions, key=lambda e: {'light':0,'climate':1,'humidifier':1,'water_heater':1,'cover':2,'fan':3,'camera':4,'media_player':5,'lock':6,'sensor':7,'binary_sensor':8,'switch':9}.get(e.kind,20))
            controls=[]
            triggers=[]
            readings=[]
            for e in functions:
                binding=next((b.external_id for b in e.bindings if b.source=='ha'),'')
                alias=aliases.get(binding,{})
                channel=getattr(self.core.adapter,'_switch_channels',{}).get(binding,{})
                if e.kind=='event' and getattr(self.core.adapter,'_control_attributes',{}).get(binding,{}).get('device_class')=='button' and not any(f.kind in {'switch','light','climate','fan','cover','humidifier','water_heater'} for f in functions):
                    triggers.append(dict(id=e.id,name=control_name(e.name,device.name),role='event',basis='ha_event_button',mode=None,mode_entity=None))
                if e.kind=='switch' and channel.get('role') in {'wireless','unknown'}:
                    triggers.append(dict(id=e.id,name=control_name(e.name,device.name),role=channel['role'],mode=channel.get('mode'),basis=channel.get('basis'),mode_entity=channel.get('mode_entity')))
                caps=[c.model_dump(mode='json') for c in e.capabilities if c.access=='write' and c.support=='supported' and c.name in {'humidifier.power','humidifier.humidity','humidifier.mode','water_heater.power','water_heater.mode','light.set','light.color_temperature','switch.set','fan.set','fan.percentage','climate.mode','climate.power','climate.temperature','climate.fan_mode','cover.position','cover.tilt','cover.state','lock.set','media_player.power','media_player.volume','media_player.mute','media_player.playback'}] if e.id in authorized else []
                if not household_function(e,alias,functions):caps=[]
                if caps:
                    attrs=getattr(self.core.adapter,'_control_attributes',{}).get(binding,{})
                    o=next((v for (id,_),v in observations.items() if id==e.id),None)
                    state=next((s for id,s in states.items() if id.startswith(e.id+':')),None)
                    controls.append(dict(id=e.id,name=alias.get('name') or control_name(e.name,device.name),
                        kind=alias.get('kind')or e.kind,capabilities=caps,state_version=e.state_version,
                        value=o.value if o else None,available=bool(o and o.transport_health=='online' and o.value not in (None,'unknown','unavailable')),
                        quality=state.quality if state else 'unknown',
                        attributes={k:attrs[k]for k in ('brightness','color_temp_kelvin','color_temp','temperature','current_temperature','current_position','current_tilt_position','percentage','hvac_modes','fan_modes','fan_mode','hvac_action','unit_of_measurement','volume_level','is_volume_muted','media_title','source','humidity','current_humidity','mode','operation_mode','operation_list')if k in attrs}))
                for cap in e.capabilities:
                    if cap.access!='read' or cap.name not in ('temperature','humidity','sensor.state','binary_sensor.state'):
                        continue
                    o=observations.get((e.id,cap.name))
                    if o:
                        attrs=getattr(self.core.adapter,'_control_attributes',{}).get(binding,{})
                        value=o.value
                        if attrs.get('device_class')=='enum':value={'Fully':'充足','Low':'偏低','Exhausted':'已耗尽','Keep-warming':'保温中','Heat':'加热中'}.get(value,value)
                        available=o.transport_health=='online' and value not in (None,'unknown','unavailable')
                        if not isinstance(value, (str, int, float, bool, type(None))):continue
                        if isinstance(value,str) and (len(value)>128 or value.lstrip().startswith(('[','{','http:','https:'))):continue
                        if any(word in e.name for word in ('ui支持控制的品类','搭配appconfig','接入设备列表','外部 IP')):continue
                        device_class=attrs.get('device_class')
                        if e.kind=='binary_sensor' and value in ('on','off'):
                            labels={'moisture':('未检测到漏水','检测到漏水'),'motion':('无人移动','检测到移动'),
                                    'occupancy':('无人','有人'),'door':('已关闭','已打开'),'opening':('已关闭','已打开'),
                                    'smoke':('正常','检测到烟雾'),'connectivity':('离线','在线')}
                            pair=labels.get(device_class,('未触发','已触发'))
                            value=pair[value=='on']
                        label=re.sub(r'[（(].*?[）)]','',e.name.rsplit(' ',1)[-1]).strip() or e.name
                        readings.append(dict(id=e.id,name=label,value=value if available else None,available=available,
                            unit=o.unit,device_class=device_class,reported_at=o.integration_written_at,
                            measured_at=o.measured_at,basis='reported',transport=o.transport_health))
            kind=controls[0]['kind'] if controls else ('lock' if '门锁' in device.name else primary[0].kind if primary else device.kind)
            if triggers:kind='switch' if controls else 'trigger'
            if not triggers and not controls and readings and kind in {'light','switch'}:
                kind='binary_sensor' if all(e.kind=='binary_sensor' for e in functions if e.kind in {'sensor','binary_sensor'}) else 'sensor'
            if kind=='camera':
                continue # real camera owner supplies a single preview-based item
            name=device.name
            if controls and not triggers and len(controls)==1 and aliases.get(next((b.external_id for b in entities[controls[0]['id']].bindings if b.source=='ha'),'')):
                name=controls[0]['name']
            if re.fullmatch(r'[a-z0-9]+(?:_[a-z0-9]+)+',name or '') and controls:
                name=controls[0]['name']
            known=[observations[(e.id,c.name)] for e in functions for c in e.capabilities
                   if c.access=='read' and (e.id,c.name) in observations]
            available=any(o.transport_health=='online' and o.value not in (None,'unknown','unavailable') for o in known)
            devices.append(dict(id=device.id,name=name,room=room_name(device.room),kind=kind,controls=controls,
                readings=readings,triggers=triggers,available=available,bindings=[b.model_dump(mode='json')for b in device.bindings]))
        devices.sort(key=lambda d: ({'light':0,'cover':1,'climate':2,'camera':3,'sensor':4,'binary_sensor':5,'media_player':6}.get(d['kind'],9),d['room'],d['name']))
        from .embodied.miot_spec import optional_cover
        cover = optional_cover() if self.embodied.configured() else None
        if cover:
            # One registered DID has one household card. The explicitly imported
            # binding owns its semantics, including unreliable slat feedback;
            # a later HA device sync must not introduce a second travel slider.
            identity='cn_'+str(cover['did'])
            devices=[d for d in devices if not any(b.get('source')=='xiaomi-identity' and b.get('external_id')==identity for b in d.get('bindings',[]))]
            from .embodied.miot_spec import cover_revision
            try:readback=await self.embodied.miot_cover('read',None)
            except (OSError,ValueError,TimeoutError):readback={}
            available=readback.get('fault')==0 and readback.get('status')not in {'unavailable','connecting'}
            unreliable=cover.get('feedback_quality')=='unreliable'
            devices.append(dict(id='friday:configured-cover',name=cover['name'],room=cover['room'],kind='cover',readings=[],available=available,
                bindings=[dict(source='xiaomi-identity',external_id='cn_'+str(cover['did']))],
                controls=[dict(id='friday:configured-cover',name=cover['name'],kind='cover',state_version=cover_revision(cover),
                    value='unconfirmed' if unreliable else 'closed' if readback.get('position')==0 else 'open',available=available,quality='unknown' if unreliable else 'reported',
                    attributes=dict(current_position=None if unreliable else readback.get('position'),position_semantics=cover.get('position_semantics','travel'),feedback_quality=cover.get('feedback_quality','reported')),
                    capabilities=[dict(name='cover.position',minimum=0,maximum=100,step=1,unit='%'),dict(name='cover.state',choices=['open','closed','stopped'])] if self.core.actions.policy_report().get('enabled') else [])]))
        cameras=self.embodied.cameras()
        camera_sessions={}
        if cameras and callable(getattr(self.embodied,'status',None)):
            try:
                report=await self.embodied.status()
                camera_sessions={r['camera']:r.get('state','unknown') for r in report.get('camera_sessions',[])}
                if not report.get('capture_enabled',True):
                    camera_sessions={c['id']:'paused' for c in cameras}
            except (OSError,ValueError,KeyError):pass
        for camera in cameras:
            devices.append(dict(id=camera['id'],name=camera['name'],room=room_name(camera['room']),kind='camera',controls=[],readings=[],camera=camera,camera_state=camera_sessions.get(camera['id'],'unknown'),bindings=[dict(source='xiaomi-identity',external_id='cn_'+str(next(c['device']['did']for c in configuration()['cameras']if c['id']==camera['id'])))]))
        try: profile=home_presentation.load()
        except (OSError,ValueError): profile=home_presentation.Profile()
        devices=[home_presentation.project(d,profile) for d in devices]
        from . import home_spatial
        from .home_layout import LayoutStore
        try:
            layout=LayoutStore(PRIVATE/'home-layout.json').current()
            membership=home_spatial.load(PRIVATE/'home-room-membership.json',layout)
        except (OSError,ValueError):
            layout,membership=None,None
        devices=home_spatial.project(devices,layout,membership)
        return dict(home_name=HOME_NAME,devices=devices,preferences=self.preferences,presentation=profile.model_dump(),
                    rooms=sorted({d['room']for d in devices if d['room']!='其他'} | {r.name for r in layout.rooms} if layout else {d['room']for d in devices if d['room']!='其他'}),
                    connected=self.connection_health().get('transport')=='online',
                    generated_at=snapshot.generated_at,simulated=snapshot.mode=='simulator',
                    control_count=len(authorized))


def create_router(core, lock, settings, embodied, archive, runtime):
    router=APIRouter(prefix='/api/home')
    owner=HomeExperience(core,settings,embodied,archive,runtime)
    router.home_owner=owner
    from .automation_actions import create_router as automation_actions_router
    if getattr(runtime,'manager',None) is not None:
        router.include_router(automation_actions_router(runtime.manager,runtime,lock))
    from .storage_preparation import create_router as storage_router
    router.include_router(storage_router(settings,core))

    from .embodied.interaction import Configuration, load as interactions_load, save as interactions_save, GESTURES
    from .embodied.household import DeviceCommands
    device_commands=DeviceCommands(core,lock,embodied)
    from .home_layout import LayoutStore,LayoutError,normalize_layout
    from .embodied.paths import PRIVATE
    layout_store=LayoutStore(PRIVATE/'home-layout.json')

    @router.get('/presentation')
    async def presentation_read():
        try:return home_presentation.load()
        except (OSError,ValueError):raise HTTPException(409,detail='presentation_profile_unavailable')from None

    @router.post('/presentation')
    async def presentation_edit(edit:PresentationEdit):
        devices=(await owner.overview())['devices']
        kinds={}
        for d in devices:
            for identity in [d['id'],*[b['external_id']for b in d.get('bindings',[])]]:
                kinds[identity]=d['kind']
            kinds.update({c['id']:c['kind']for c in d['controls']})
        for identity,style in edit.profile.overrides.items():
            kind=kinds.get(identity)
            allowed=({'ceiling_vent','wall_unit','generic'}if kind=='climate'else
                     {'main_light','pendant','led_strip','downlight','spotlight','accent_light','power_supply','switch_panel','generic'}if kind in {'light','switch'}else
                     {'generic', {'cover':'curtain','camera':'camera','lock':'door_lock','sensor':'sensor',
                                  'binary_sensor':'sensor','media_player':'speaker'}.get(kind,'generic')})
            if identity not in kinds or style.fixture not in allowed:
                raise HTTPException(422,detail='presentation_target_invalid')
        try:return home_presentation.save(edit.profile,edit.expected_revision)
        except (OSError,ValueError)as exc:raise HTTPException(409,detail='presentation_profile_changed')from None

    @router.get('/layout')
    async def layout_read():
        try:return layout_store.current()
        except (OSError,ValueError):raise HTTPException(409,detail='layout_unavailable')from None

    @router.post('/layout/preview')
    async def layout_preview(request:LayoutInput):
        try:return layout_store.preview(request.layout)
        except (OSError,ValueError):raise HTTPException(422,detail='layout_invalid_import')from None

    @router.post('/layout/import')
    async def layout_import(request:LayoutInput):
        if request.expected_revision is None:raise HTTPException(422)
        try:return layout_store.apply(request.layout,request.expected_revision,accept_device_changes=request.accept_device_changes)
        except LayoutError as exc:raise HTTPException(409,detail=exc.code)from None
        except (OSError,ValueError):raise HTTPException(422,detail='layout_invalid_import')from None

    @router.post('/layout/device-position')
    async def device_position(request:DevicePosition):
        current=layout_store.current()
        if current is None:raise HTTPException(409,detail='layout_not_imported')
        snapshot=await owner.overview()
        known={d['id'] for d in snapshot['devices']}
        known.update(b['external_id']for d in snapshot['devices']for b in d.get('bindings',[])if b['source']=='xiaomi-identity')
        if request.device_id not in known:raise HTTPException(422,detail='layout_device_unknown')
        devices=[d.model_dump()for d in current.devices if d.id!=request.device_id]
        devices.append(dict(id=request.device_id,room_id=request.room_id,position=request.position))
        try:return layout_store.replace_devices(devices,request.expected_revision)
        except LayoutError as exc:raise HTTPException(409,detail=exc.code)from None
        except (OSError,ValueError):raise HTTPException(422,detail='layout_invalid_position')from None

    @router.post('/layout/restore')
    async def restore_layout(request:Preferences):
        try:return layout_store.restore(request.expected_revision)
        except (OSError,ValueError):raise HTTPException(409,detail='layout_restore_unavailable')from None


    @router.get('/interactions')
    async def interactions():
        config=interactions_load()
        return dict(config=config.model_dump(),gestures=GESTURES)

    @router.post('/interactions')
    async def interactions_edit(edit:Configuration):
        inventory={i['id']:i for i in await device_commands.inventory() if i['controllable']}
        try:
            return interactions_save(edit,inventory,{c['id'] for c in embodied.cameras()}).model_dump()
        except ValueError as exc:
            raise HTTPException(409,detail=str(exc))from None

    @router.get('/overview')
    async def overview():
        return await owner.overview()

    @router.post('/preferences')
    async def preferences(edit:Preferences):
        try:
            owner.save(edit,commit=False)
            if edit.automatic_running is not None:
                await runtime.set_execution(edit.automatic_running)
            return owner.save(edit)
        except ValueError as exc:
            raise HTTPException(409,detail=str(exc)) from None

    @router.post('/control')
    async def control(request:Control):
        if owner.preferences.get('automatic_running'):
            await runtime.set_execution(True, manual_seconds=60)
        if request.target=='friday:configured-cover':
            from .embodied.miot_spec import optional_cover,cover_revision
            cover=optional_cover()
            if cover is None or request.expected_state_version!=cover_revision(cover):raise HTTPException(409,detail='cover_binding_changed')
            if request.operation=='cover.position' and set(request.desired)=={'position'} and type(request.desired['position'])is int and 0<=request.desired['position']<=100:action='position:'+str(request.desired['position'])
            elif request.operation=='cover.state' and request.desired in ({'state':'open'},{'state':'closed'},{'state':'stopped'}):action={'open':'open','closed':'close','stopped':'stop'}[request.desired['state']]
            else:raise HTTPException(422,detail='unsupported_curtain_parameters')
            result=await device_commands.command(None,action,request.event_id,target=request.target)
            return dict(status=result.get('status','unknown_outcome'),code=result.get('code','curtain_readback'),feedback_quality=cover.get('feedback_quality','reported'),result=result)
        async with lock:
            try:
                existing=core.store.light_action(key=request.event_id)
                if existing is not None:
                    previous=existing.request
                    if previous.target!=request.target or getattr(previous,'operation','light.set')!=request.operation or previous.desired!=request.desired or previous.expected_state_version!=request.expected_state_version:
                        raise ValueError('source_conflict')
                    return await core.actions.execute(existing.action_id)if existing.status=='planned'else existing
                plan=DevicePlanRequest(target=request.target,operation=request.operation,desired=request.desired,
                    expected_state_version=request.expected_state_version,expires_at=datetime.now(UTC)+timedelta(seconds=15),
                    idempotency_key=request.event_id)
                trace=await core.actions.plan(plan)
                if trace.status=='planned':
                    trace=await core.actions.execute(trace.action_id)
                return trace
            except ValueError:
                raise HTTPException(409,detail='device_control_unavailable') from None

    @router.get('/connections')
    async def connections():
        nas=dict(connected=False,configured=bool(settings.config.nas_path or settings.config.nas_url))
        try:
            root=await archive._root()
            # Bounded metadata only; no media read on the connections screen.
            from .settings import scan_directory
            report=await asyncio.wait_for(asyncio.to_thread(scan_directory,root),3)
            nas.update(connected=report.get('readable')is True,status=report.get('status'))
        except (ValueError,OSError,TimeoutError):
            pass
        return dict(home=owner.connection_health(),cameras=dict(configured=embodied.configured(),count=len(embodied.cameras())),nas=nas)

    @router.get('/automatic-status')
    async def automatic_status():
        return dict(enabled=owner.preferences.get('automatic_running',False),runtime=runtime.status())

    @router.get('/recordings')
    async def recordings(folder:str|None=None,date:str|None=None):
        try:
            result=await owner.recordings.browse(folder,date)
            return dict(**result,names=owner.preferences['recording_names'])
        except (ValueError,OSError,TimeoutError)as exc:
            code='recordings_mount_required'if isinstance(exc,ValueError)and str(exc)in {'archive_mount_required','archive_not_configured'}else 'recordings_temporarily_unavailable'
            return dict(connected=False,files=[],folders=[],code=code,names=owner.preferences['recording_names'])

    @router.get('/recordings/privacy')
    async def recording_privacy():
        import importlib.util
        return {**owner.recordings.privacy.configuration_report(),
                'runtime_available':all(importlib.util.find_spec(name) is not None
                                        for name in ('av','cv2','numpy')),
                'detector_runtime_available':all(importlib.util.find_spec(name) is not None
                                                 for name in ('onnxruntime','PIL')),
                'model_inference_attempted':False}

    @router.post('/recordings/play')
    async def play(request:RecordingRequest,http_request:Request):
        try:
            return await prepare_for_viewer(http_request,lambda:owner.recordings.prepare(request.id,request.offset_seconds),
                                            discard=lambda plan:owner.recordings.revoke(plan['token']))
        except ValueError as exc:
            allowed={'recording_refresh_required','recording_source_changed','recording_expired',
                     'archive_privacy_busy','archive_privacy_budget','archive_privacy_unavailable',
                     'archive_preview_cancelled'}
            allowed.add('recording_invalid_offset')
            code=str(exc) if str(exc) in allowed else 'recording_refresh_required'
            raise HTTPException(409,detail=code)from None
        except (OSError,TimeoutError):
            raise HTTPException(503,detail='archive_privacy_unavailable')from None

    @router.post('/recordings/stop')
    async def stop_recording(request:RecordingStopRequest):
        return owner.recordings.revoke(request.token)

    @router.get('/recordings/media/{token}')
    async def media(token:str,request:Request):
        if request.headers.get('sec-fetch-site')not in (None,'same-origin','none'):
            raise HTTPException(403)
        try:
            reader=await owner.recordings.media(token,request.headers.get('range'))
            headers={'Content-Length':str(reader.length),'Accept-Ranges':'bytes','Cache-Control':'no-store',
                     'Cross-Origin-Resource-Policy':'same-origin','Referrer-Policy':'no-referrer',
                     'Content-Disposition':'inline; filename="protected-preview.mp4"'}
            if reader.partial:
                headers['Content-Range']=f'bytes {reader.start}-{reader.start+reader.length-1}/{reader.lease.binding["size_bytes"]}'
            return StreamingResponse(reader.chunks(),media_type='video/mp4',headers=headers,
                                     status_code=206 if reader.partial else 200,
                                     background=BackgroundTask(reader.close))
        except (ValueError,OSError,TimeoutError):
            raise HTTPException(416,detail='recording_unavailable')from None

    return router
