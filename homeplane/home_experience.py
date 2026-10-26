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

from .action_contracts import DevicePlanRequest
from .recordings import Recordings
from . import home_presentation

HOME_NAME = '我的家'


def room_name(value):
    return (value or '').strip() or '其他'


def household_function(entity, alias, functions):
    """Select the physical function for the product view, independently of authority."""
    if alias:
        return True
    name = entity.name.lower()
    if any(word in name for word in ('指示灯', 'indicator', '凌动', '延时', '防闪烁',
        '默认', '断电', '上电', '生物节律', '调光模式', '起夜', '初始化', '格式化',
        '恢复出厂', '电机反向', '屏幕', '显示屏', '禁麦', '睡眠模式', '弹窗', '音效')):
        return False
    if entity.kind in {'light', 'climate', 'cover', 'fan', 'lock'}:
        return True
    if entity.kind == 'switch':
        if ' * ' in entity.name or '门锁' in entity.name:
            return False
        if any(e.kind in {'light', 'climate', 'cover', 'fan', 'lock'} for e in functions):
            return False
        # MIoT generic properties on panels/locks are not physical power channels.
        parts = [p.strip() for p in entity.name.split('  ') if p.strip()]
        if len(parts) > 1:
            channel = parts[-1].split()
            return channel[-1] in {'开关', '左键', '右键', '中键'} and any('开关' in p or '插座' in p for p in channel[:-1] + parts[:-1])
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
        self.recordings = Recordings(archive)
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
            primary = sorted(functions, key=lambda e: {'light':0,'climate':1,'cover':2,'fan':3,'camera':4,'media_player':5,'lock':6,'sensor':7,'binary_sensor':8,'switch':9}.get(e.kind,20))
            controls=[]
            readings=[]
            for e in functions:
                binding=next((b.external_id for b in e.bindings if b.source=='ha'),'')
                alias=aliases.get(binding,{})
                caps=[c.model_dump(mode='json') for c in e.capabilities if c.access=='write' and c.support=='supported' and c.name in {'light.set','switch.set','fan.set','fan.percentage','climate.mode','climate.temperature','cover.position','cover.state','lock.set'}] if e.id in authorized else []
                if not household_function(e,alias,functions):caps=[]
                if caps:
                    attrs=getattr(self.core.adapter,'_control_attributes',{}).get(binding,{})
                    o=next((v for (id,_),v in observations.items() if id==e.id),None)
                    state=next((s for id,s in states.items() if id.startswith(e.id+':')),None)
                    controls.append(dict(id=e.id,name=alias.get('name') or e.name.split('  ')[0],
                        kind=alias.get('kind')or e.kind,capabilities=caps,state_version=e.state_version,
                        value=o.value if o else None,available=bool(o and o.transport_health=='online' and o.value not in (None,'unknown','unavailable')),
                        quality=state.quality if state else 'unknown',
                        attributes={k:attrs[k]for k in ('brightness','color_temp_kelvin','color_temp','temperature','current_temperature','current_position','percentage','hvac_modes','fan_modes','unit_of_measurement')if k in attrs}))
                for cap in e.capabilities:
                    if cap.access!='read' or cap.name not in ('temperature','humidity','sensor.state','binary_sensor.state'):
                        continue
                    o=observations.get((e.id,cap.name))
                    if o and '电量' not in e.name and '电池' not in e.name and 'battery' not in e.name.lower() and o.value not in (None,'unknown','unavailable') and (o.unit in ('°C','%','lx') or e.kind=='binary_sensor'):
                        attrs=getattr(self.core.adapter,'_control_attributes',{}).get(binding,{})
                        value=o.value
                        device_class=attrs.get('device_class')
                        if e.kind=='binary_sensor' and value in ('on','off'):
                            labels={'moisture':('未检测到漏水','检测到漏水'),'motion':('无人移动','检测到移动'),
                                    'occupancy':('无人','有人'),'door':('已关闭','已打开'),'opening':('已关闭','已打开'),
                                    'smoke':('正常','检测到烟雾'),'connectivity':('离线','在线')}
                            pair=labels.get(device_class,('未触发','已触发'))
                            value=pair[value=='on']
                        readings.append(dict(name=e.name,value=value,unit=o.unit,reported_at=o.integration_written_at,
                            measured_at=o.measured_at,basis='reported',transport=o.transport_health))
            kind=controls[0]['kind'] if controls else ('lock' if '门锁' in device.name else primary[0].kind if primary else device.kind)
            if kind=='camera':
                continue # real camera owner supplies a single preview-based item
            if not controls and not readings and kind not in ('vacuum','media_player','lock'):
                continue
            name=device.name
            if controls and len(controls)==1 and aliases.get(next((b.external_id for b in entities[controls[0]['id']].bindings if b.source=='ha'),'')):
                name=controls[0]['name']
            if re.fullmatch(r'[a-z0-9]+(?:_[a-z0-9]+)+',name or '') and controls:
                name=controls[0]['name']
            devices.append(dict(id=device.id,name=name,room=room_name(device.room),kind=kind,controls=controls,readings=readings[:6],bindings=[b.model_dump(mode='json')for b in device.bindings]))
        devices.sort(key=lambda d: ({'light':0,'cover':1,'climate':2,'camera':3,'sensor':4,'binary_sensor':5,'media_player':6}.get(d['kind'],9),d['room'],d['name']))
        from .embodied.miot_spec import optional_cover
        cover = optional_cover() if self.embodied.configured() else None
        if cover and self.core.actions.policy_report().get('enabled'):
            devices.append(dict(id='friday:configured-cover',name=cover['name'],room=cover['room'],kind='cover',readings=[],
                controls=[dict(id='friday:configured-cover',name=cover['name'],kind='cover',state_version='miot-selected-cover-v1',
                value='connected',available=True,quality='reported',attributes={},capabilities=[dict(name='cover.position',minimum=0,maximum=100,step=1,unit='%')])]))
        cameras=self.embodied.cameras()
        for camera in cameras:
            devices.append(dict(id=camera['id'],name=camera['name'],room=room_name(camera['room']),kind='camera',controls=[],readings=[],camera=camera,bindings=[dict(source='xiaomi-identity',external_id='cn_'+str(next(c['device']['did']for c in configuration()['cameras']if c['id']==camera['id'])))]))
        try: profile=home_presentation.load()
        except (OSError,ValueError): profile=home_presentation.Profile()
        devices=[home_presentation.project(d,profile) for d in devices]
        return dict(home_name=HOME_NAME,devices=devices,preferences=self.preferences,presentation=profile.model_dump(),
                    rooms=sorted({d['room']for d in devices if d['room']!='其他'}),
                    connected=self.connection_health().get('transport')=='online',
                    generated_at=snapshot.generated_at,simulated=snapshot.mode=='simulator',
                    control_count=len(authorized))


def create_router(core, lock, settings, embodied, archive, runtime):
    router=APIRouter(prefix='/api/home')
    owner=HomeExperience(core,settings,embodied,archive,runtime)
    router.home_owner=owner

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
                     {'main_light','pendant','led_strip','downlight','spotlight','accent_light','power_supply','generic'}if kind in {'light','switch'}else {'generic'})
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
            if request.operation!='cover.position' or set(request.desired)!={'position'} or type(request.desired['position'])is not int or not 0<=request.desired['position']<=100:
                raise HTTPException(422,detail='unsupported_curtain_parameters')
            result=await device_commands.command(None,'position:'+str(request.desired['position']),request.event_id,target=request.target)
            return dict(status=result.get('status','unknown_outcome'),code=result.get('code','curtain_readback'),**{'result':result})
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

    @router.post('/recordings/play')
    async def play(request:RecordingRequest):
        try:
            return await owner.recordings.prepare(request.id)
        except (ValueError,OSError):
            raise HTTPException(409,detail='recording_refresh_required')from None

    @router.get('/recordings/media/{token}')
    async def media(token:str,request:Request):
        if request.headers.get('sec-fetch-site')not in (None,'same-origin','none'):
            raise HTTPException(403)
        try:
            chunks,headers,status=await asyncio.to_thread(owner.recordings.media,token,request.headers.get('range'))
            return StreamingResponse(chunks,media_type='video/mp4',headers=headers,status_code=status)
        except (ValueError,OSError):
            raise HTTPException(416,detail='recording_unavailable')from None

    return router
