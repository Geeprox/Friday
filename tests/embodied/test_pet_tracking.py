"""Original tracker/director/secondary observation paths, with offline rectangles only."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from homeplane.embodied.pet_tracks import PetTracks
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.camera_intent import CAMERA_MODES
from homeplane.embodied.stable_tracks import StableTracks
from tests.embodied.test_camera_follow_appearance import appearance_sample


@pytest.mark.asyncio
async def test_existing_api_routes_pet_choice_only_to_registered_camera():
    import asyncio
    import httpx
    from fastapi import FastAPI
    from homeplane.embodied.api import create_router
    control=AsyncMock(return_value={'mode':'pets'})
    service=SimpleNamespace(camera_control=control,cameras=lambda:[{'id':'cam_1111111111'}])
    app=FastAPI();app.include_router(create_router(service,SimpleNamespace(),asyncio.Lock()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        path='/api/embodied/cameras/cam_1111111111/control'
        assert (await client.post(path,json={'mode':'pets'})).json()=={'mode':'pets'}
        control.assert_awaited_once_with('cam_1111111111',direction=None,mode='pets')
        for body in ({'mode':'animal'},{'mode':'pets','direction':'left'}):
            assert (await client.post(path,json=body)).status_code==422
        assert (await client.post(path.replace('1111111111','2222222222'),json={'mode':'pets'})).status_code==409
        assert control.await_count==1


def animal(species='cat', x=.78, *, y=.3, h=.25):
    return dict(species=species, box=dict(x=x,y=y,w=.15,h=h,confidence=.9))


def detection(*animals):
    return dict(boxes=[],poses=[],animals_supported=True,animals_status='ready',animals=list(animals))


def packet(at, animals=(), **changes):
    return dict(status='live',_local_received=at,tracking_current=True,scene_visible=True,
                session_id='synthetic-camera-session',people=[],animals=list(animals),
                animals_current=True,animals_intent='choice-one',**changes)


def observed(p, rows, at):
    return p.update(detection(*rows),at,intent='choice-one')


def test_original_bytetrack_namespaces_species_and_never_exports_human_authority():
    p=PetTracks()
    for at in (0,.1,.2):
        result=observed(p,[animal(),animal('dog',.1)],at)
    assert len(result)==2 and {a['species'] for a in result}=={'cat','dog'}
    assert len({a['id'] for a in result})==2
    for a in result:
        assert a['id'].startswith(a['species']+':') and a['tracking']['backend']=='ByteTrack'
        assert set(a)=={'id','species','box','motion','tracking'}
    assert p.public()==dict(status='ready',supported=True,current=True,species=['cat','dog'],identity=False)
    assert observed(p,[],.3)==[], 'Lost tracker state must not be returned as a visible pet'

def test_cat_track_marks_a_crossing_dog_before_it_has_a_confirmed_track():
    tracker = PetTracks()
    observed(tracker, [animal()], 0)
    cat = observed(tracker, [animal()], .1)[0]
    crossing = observed(tracker, [animal(), animal('dog', .76)], .2)
    obstructed = next(row for row in crossing if row['id'] == cat['id'])
    assert obstructed['tracking']['overlapping']
    assert obstructed['tracking']['follow_revision'] != cat['tracking']['follow_revision']
    clear = observed(tracker, [animal()], .3)[0]
    assert clear['id'] == cat['id'] and not clear['tracking']['overlapping']
    assert clear['tracking']['follow_revision'] == obstructed['tracking']['follow_revision']


@pytest.mark.parametrize('invalid',[
    {},dict(animals_supported=False),dict(animals_status='unavailable'),dict(animals=None),
    dict(animals=[animal('person')]),dict(animals=[animal()]*11),
    dict(animals=[dict(species='cat',box=dict(x=float('nan'),y=.1,w=.2,h=.2,confidence=.9))]),
    dict(animals=[dict(species='dog',box=dict(x=.9,y=.1,w=.3,h=.2,confidence=.9))]),
    dict(animals=[dict(species='cat',box=dict(x=.1,y=.1,w=.2,h=.2,confidence=.74))]),
])
def test_unavailable_or_invalid_native_output_retires_every_pet(invalid):
    p=PetTracks()
    for at in (0,.1,.2):observed(p,[animal()],at)
    value=detection(animal());value.update(invalid)
    if invalid=={}:value={}
    assert p.update(value,.3,intent='choice-one')==[]
    assert not p.current and not p.trackers


@pytest.mark.parametrize('change',['intent','motion','time','registration','disabled'])
def test_new_intent_movement_replay_bad_registration_and_disabling_cannot_reuse_pet(change):
    p=PetTracks()
    for at in (0,.1,.2):observed(p,[animal()],at)
    kwargs=dict(intent='choice-one',transform=None,camera_moving=False)
    at=.3
    if change=='intent':kwargs['intent']='choice-two'
    elif change=='motion':kwargs['camera_moving']=True
    elif change=='time':at=.2
    elif change=='registration':kwargs['transform']=np.zeros((3,3))
    else:kwargs['intent']=None
    assert p.update(detection(animal()),at,**kwargs)==[]
    if change!='intent':assert not p.current


def test_real_pet_tracks_reach_bounded_director_and_pending_check_without_a_person():
    tracks=PetTracks();d=CameraDirector();d.apply_mode('pets','choice-one')
    for at in (0,.1,.2):
        animals=observed(tracks,[animal()],at)
        s=packet(at,animals,**appearance_sample(at,[(pet,7) for pet in animals]));direction=d.choose(s,at)
    assert direction=='right' and not s['people']
    assert d.decision_current(s,.2,'right') and d.public()['reason']=='跟随宠物'
    for change in (dict(animals_current=False),dict(animals_intent='old-choice'),
                   dict(game={'status':'playing'}),dict(tracking_current=False)):
        changed=dict(s,**change)
        assert not d.decision_current(changed,.3,'right')
    d.sent(.2)
    assert d.choose(s,2.3) is None, 'A pre-movement packet cannot justify another motor impulse'
    for at in (2.4,2.5,2.6,4.3):
        animals=observed(tracks,[animal()],at)
        s=packet(at,animals,**appearance_sample(at,[(pet,7) for pet in animals]));direction=d.choose(s,at)
    assert direction is None
    for at in (4.4, 4.5):
        animals = observed(tracks, [animal()], at)
        s = packet(at, animals, **appearance_sample(at, [(pet, 7) for pet in animals]))
        direction = d.choose(s, at)
    assert direction=='right'
    d.apply_mode('hold','manual-choice')
    assert not d.decision_current(s,4.3,'right') and d.choose(s,4.4) is None


def test_pets_do_not_borrow_human_focus_or_choose_between_multiple_animals():
    d=CameraDirector();d.apply_mode('pets','choice-one')
    animals=[dict(id='cat:1',**animal()),dict(id='dog:1',**animal('dog',.1))]
    for at in (0,.1,.2):assert d.choose(packet(at,animals),at,focus={'track':'cat:1','at':at}) is None
    assert d.phase=='target_ambiguous' and d.target is None
    d.apply_mode('auto','human-choice')
    assert d.choose(packet(.3,animals),.3) is None and d.target is None


def test_rebuilt_pet_tracks_cannot_silently_select_a_different_animal_after_motion():
    tracks = PetTracks()
    director = CameraDirector()
    director.apply_mode('pets', 'choice-one')
    for at in (0, .1, .2):
        animals = observed(tracks, [animal()], at)
        direction = director.choose(packet(at, animals), at)
    assert direction == 'right'
    original = animals[0]['id']
    director.sent(.2)
    tracks.clear()
    for at in (4.3, 4.4, 4.5):
        animals = observed(tracks, [animal(x=.04), animal(x=.78)], at)
        direction = director.choose(packet(at, animals), at)
    assert len(animals) == 2 and original not in {pet['id'] for pet in animals}
    assert direction is None and director.phase == 'target_reacquiring'


def test_unavailable_pet_detection_prevents_patrol_and_cover_recovery_too():
    d=CameraDirector();d.apply_mode('pets','choice-one')
    for at in range(200):
        s=packet(at);s.update(animals_current=False,scene_visible=at%2==0)
        assert d.choose(s,at) is None
    assert d.phase=='pet_unavailable'
    for at in (201,202,203):
        s=packet(at);s['scene_visible']=False;direction=d.choose(s,at)
    assert direction=='up'
    assert not d.decision_current(dict(s,animals_current=False),203,'up')
    assert not d.decision_current(dict(s,game={'status':'calibrating'}),203,'up')


@pytest.mark.asyncio
async def test_original_secondary_native_pipe_keeps_pets_out_of_human_pipelines(monkeypatch):
    from homeplane.embodied import camera_recognition as module
    clock=[100.0]
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0],time=lambda:1000+clock[0]))
    monkeypatch.setattr(module,'analyze',AsyncMock(return_value=[]))
    sent=[];detection_bytes=(json.dumps(detection(animal()))+'\n').encode()
    c=module.CameraRecognition.__new__(module.CameraRecognition)
    c.camera='cam_1111111111';c.session='synthetic-session';c.tracks=StableTracks();c.pet_tracks=PetTracks()
    c.animals_enabled=False;c.animals_intent='choice-one';c.motion_generation=0;c.motion_until=0
    c.identities=SimpleNamespace(discard=Mock())
    c.vision=module.VisionProcess('synthetic-vision')
    c.vision.process=SimpleNamespace(stdin=SimpleNamespace(write=sent.append,drain=AsyncMock()),
                            stdout=SimpleNamespace(readline=AsyncMock(return_value=detection_bytes)))
    from tests.embodied.test_room_process import room_port
    c.room=room_port()
    frame=lambda:dict(jpeg=base64.b64encode(b'synthetic-frame').decode(),received_at=1000+clock[0],width=640,height=360)
    result=await c.observe(frame())
    assert json.loads(sent[-1])['animals_enabled'] is False and result['animals']==[]
    c.animals_enabled=True
    for _ in range(3):clock[0]+=.1;result=await c.observe(frame())
    assert json.loads(sent[-1])['animals_enabled'] is True
    assert len(result['animals'])==1 and result['animals_current'] is True
    assert result['animals_intent']=='choice-one' and result['people']==[]
    assert c.room.update.call_args.args[1]==[]
    assert c.room.register.call_args.args[1]==[{'box':animal()['box']}]
    assert module.analyze.call_args.args[2]==[]
    c.room.update.return_value=dict(registration={'status':'moving'});clock[0]+=.1
    late_motion=await c.observe(frame())
    assert late_motion['animals']==[] and late_motion['animals_current'] is False
    c.room.update.return_value=dict(registration={'status':'locked'})
    c.mark_motion(clock[0]);clock[0]+=.1
    c.room.expect_motion=Mock()
    assert (await c.observe(frame()))['animals']==[]


@pytest.mark.asyncio
@pytest.mark.parametrize(('target', 'expected_direction'), [
    (animal(), 'right'), (animal(x=.43, y=.78, h=.20), 'down'), (animal(x=.43, y=.02, h=.2), 'up'),
])
async def test_primary_persisted_pet_choice_reaches_original_motor_dispatch_once(tmp_path, target, expected_direction):
    from homeplane.embodied.calibration_frames import CalibrationFrames
    import ast
    import asyncio
    import secrets
    from pathlib import Path
    from homeplane.embodied.camera_intent import CameraIntentStore
    selected=dict(id='cam_1111111111',device={'did':'synthetic-camera'})
    store=CameraIntentStore(tmp_path/'camera',selected)
    clock=[0.0];sent=[];noop=lambda *_,**__:None
    scope=dict(asyncio=asyncio,secrets=secrets,json=json,CAMERA_MODES=CAMERA_MODES,
        time=SimpleNamespace(monotonic=lambda:clock[0],time=lambda:1000+clock[0]),
        camera_intents=store,camera_planner=SimpleNamespace(mode='auto',intent_revision='old',intent_status='default'),
        motor_sequence=0,motor_waiters={},motor_last_sent=-100,motion_generation=0,calibration_frames=CalibrationFrames(),
        camera_lease=SimpleNamespace(snapshot={'status':'owned'}),room_lock=asyncio.Lock(),
        SELECTED=selected,camera_is_open=AsyncMock(return_value=True),close_primary_view=noop,secondary_homes={},
        identities=SimpleNamespace(discard=noop),fleet=SimpleNamespace(clear=noop),
        safety=SimpleNamespace(clear_view=noop),
        home=SimpleNamespace(clear_view_for_motion=noop,runtime_log=SimpleNamespace(record=noop)),
        room=SimpleNamespace(expect_motion=noop))
    def send(raw):
        command=json.loads(raw);sent.append(command)
        asyncio.get_running_loop().call_soon(scope['motor_waiters'][command['request_id']].set_result,{'native_code':0})
    scope['capture']=SimpleNamespace(stdin=SimpleNamespace(write=send,drain=AsyncMock()))
    path=Path('homeplane/embodied/worker.py')
    nodes=[n for n in ast.parse(path.read_text()).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
           and n.name in {'set_primary_camera_mode','motor_step','clear_camera_view'}]
    assert len(nodes)==3
    for n in nodes:n.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),scope)
    scope['set_primary_camera_mode']('pets')
    restored=CameraIntentStore(tmp_path/'camera',selected).load()
    assert restored.mode=='pets'
    d=CameraDirector();d.apply_mode(restored.mode,restored.revision);tracks=PetTracks()
    for at in (0, .1, .2, .3):
        clock[0]=at
        animals=tracks.update(detection(target),at,intent=restored.revision)
        s=packet(at,animals);s['animals_intent']=restored.revision
        direction=d.choose(s,at)
        if direction:
            break
    assert direction == expected_direction
    scope.update(state=s, last_frame=at)
    reply=await scope['motor_step'](direction,at,expected_intent=restored.revision,
        decision_current=lambda:d.decision_current(scope['state'],clock[0],direction))
    assert reply['native_code']==0 and len(sent)==1 and sent[0]['direction']==expected_direction
    assert sent[0]['action']=='motor_step' and sent[0]['expires_mono']==pytest.approx(at+1.5)
    d.sent(at)
    clock[0] = at + 2.1
    assert d.choose(s,clock[0]) is None and len(sent)==1 and not scope['motor_waiters']
