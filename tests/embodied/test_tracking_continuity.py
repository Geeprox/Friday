"""Synthetic detector packets through the installed ByteTrack, no model or I/O."""
import copy
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from homeplane.embodied.stable_tracks import StableTracks


def box(x=.1,w=.3):
    return dict(x=x,y=.05,w=w,h=.85,confidence=.95)


def pose(x=.25):
    points={'root':(x,.55),'neck':(x,.25),'leftShoulder':(x-.04,.3),
            'rightShoulder':(x+.04,.3),'leftWrist':(x-.05,.12),'rightWrist':(x+.05,.12),
            'leftElbow':(x-.08,.22),'rightElbow':(x+.08,.22),
            'leftHip':(x-.04,.55),'rightHip':(x+.04,.55)}
    return {'joints':{name:dict(x=px,y=py,confidence=.95) for name,(px,py) in points.items()}}


def frame():
    return dict(boxes=[box()],poses=[pose()])


def warm(tracker, *, detection=None):
    result=[]
    for at in [0,.05,.1,.15]:result=tracker.update(copy.deepcopy(detection or frame()),at)
    assert result and 'raised_arm' in result[0]['actions']
    return result


def test_repeated_packet_is_not_new_pose_evidence():
    tracker=StableTracks();warm(tracker)
    assert tracker.update(frame(),.15)==[]


def test_pose_streak_restarts_after_gap_while_short_occlusion_keeps_box_track():
    tracker=StableTracks();old=warm(tracker)[0]
    returned=tracker.update(frame(),2.0)[0]
    assert returned['id']==old['id']
    assert returned['actions']==['pose_unknown'] and returned['motion']=='unknown'
    assert tracker.update(frame(),2.05)[0]['actions']==['pose_unknown']
    assert 'raised_arm' in tracker.update(frame(),2.1)[0]['actions']


def test_long_input_gap_never_reuses_retired_track_or_pose_confirmation():
    tracker=StableTracks();old=warm(tracker)[0]['id']
    assert tracker.update(frame(),6)==[]
    returned=tracker.update(frame(),6.05)[0]
    assert returned['id']!=old and returned['actions']==['pose_unknown']


def test_continuous_empty_frames_do_not_keep_an_old_person_association_for_twenty_seconds():
    tracker=StableTracks();old=warm(tracker)[0]['id']
    for at in np.arange(.2,6,.2):assert tracker.update({'boxes':[],'poses':[]},float(at))==[]
    assert tracker.update(frame(),6)==[]
    assert tracker.update(frame(),6.05)[0]['id']!=old


def test_overlap_keeps_body_boxes_but_does_not_guess_whose_pose_has_more_joints():
    tracker=StableTracks();detection=dict(boxes=[box(.2,.3),box(.4,.3)],poses=[pose(.44),pose(.46)])
    people=[]
    for at in [0,.05,.1,.15]:people=tracker.update(copy.deepcopy(detection),at)
    assert len(people)==2
    assert all(not p['joints'] and p['actions']==['pose_unknown'] for p in people)


def test_separate_people_keep_their_own_pose_and_track_when_detector_order_reverses():
    tracker=StableTracks()
    first=dict(boxes=[box(.05),box(.65)],poses=[pose(.2),pose(.8)])
    tracker.update(copy.deepcopy(first),0);people=tracker.update(copy.deepcopy(first),.05)
    ids={round(p['box']['x'],2):p['id'] for p in people}
    second=dict(boxes=first['boxes'][::-1],poses=first['poses'][::-1])
    for at in [.1,.15,.2]:people=tracker.update(copy.deepcopy(second),at)
    assert len(people)==2
    for p in people:
        assert p['id']==ids[round(p['box']['x'],2)]
        assert p['joints']['root']['x']==pytest.approx(p['box']['x']+.15)
        assert 'raised_arm' in p['actions']


@pytest.mark.parametrize('bad', [float('nan'),float('inf'),-.1])
def test_invalid_frame_clock_retires_old_continuity(bad):
    tracker=StableTracks();old=warm(tracker)[0]['id']
    assert tracker.update(frame(),bad)==[]
    assert tracker.update(frame(),.2)==[]
    assert tracker.update(frame(),.25)[0]['id']!=old


@pytest.mark.parametrize('matrix',[np.zeros((3,3)),np.full((3,3),float('nan')),np.eye(2),
                                 np.array([[1,0,7],[0,1,0],[0,0,1]])])
def test_invalid_registration_never_reaches_kalman_or_relabels_an_old_track(matrix):
    tracker=StableTracks();old=warm(tracker)[0]['id']
    assert tracker.update(frame(),.2,transform=matrix)==[]
    assert tracker.input_valid is False
    assert tracker.update(frame(),.25)==[]
    assert tracker.update(frame(),.3)[0]['id']!=old


@pytest.mark.parametrize('bad',[{'w':-.3},{'x':-1},{'confidence':None},{'confidence':float('inf')}])
def test_invalid_image_box_is_never_a_tracked_person(bad):
    tracker=StableTracks();detection=frame();detection['boxes'][0].update(bad)
    for at in [0,.1,.2,.3]:
        assert tracker.update(detection,at)==[]
        assert tracker.input_valid is False


def test_invalid_tracking_never_becomes_empty_room_patrol_or_a_motor_command():
    from homeplane.embodied.camera_director import CameraDirector
    tracker=StableTracks();director=CameraDirector();bad=frame();bad['boxes'][0]['confidence']=None
    for at in range(100):
        people=tracker.update(copy.deepcopy(bad),at)
        decision=director.choose(dict(status='live',_local_received=at,scene_visible=True,
            people=people,session_id='synthetic-session',tracking_current=tracker.input_valid),at)
        assert decision is None and director.public()['view_usable'] is False
    assert director.phase=='waiting_video'
    people=tracker.update(frame(),100)
    assert director.choose(dict(status='live',_local_received=100,scene_visible=True,
        people=people,session_id='synthetic-session',tracking_current=tracker.input_valid),100) is None
    assert director.phase=='checking_view'
    people=tracker.update(frame(),100.05)
    assert director.choose(dict(status='live',_local_received=100.05,scene_visible=True,
        people=people,session_id='synthetic-session',tracking_current=tracker.input_valid),100.05)=='left'


@pytest.mark.asyncio
@pytest.mark.parametrize('overlap',[False,True])
async def test_original_camera_observation_to_actual_gesture_engine_preserves_pose_ownership(monkeypatch,overlap):
    import homeplane.embodied.camera_recognition as recognition
    from homeplane.embodied.interaction import Binding,Configuration,InteractionEngine
    clock=[0.0]
    monkeypatch.setattr(recognition,'time',SimpleNamespace(monotonic=lambda:clock[0],time=lambda:1000+clock[0]))
    monkeypatch.setattr(recognition,'analyze',AsyncMock(return_value=[]))
    detection=(dict(boxes=[box(.2),box(.4)],poses=[pose(.44),pose(.46)]) if overlap else frame())
    session=recognition.CameraRecognition.__new__(recognition.CameraRecognition)
    session.camera='SYNTHETIC';session.session='synthetic-session';session.tracks=StableTracks()
    session.pet_tracks=recognition.PetTracks();session.animals_enabled=False;session.animals_intent=None
    session.motion_generation=0;session.motion_until=-100;session.last=None;session.timing={}
    session.identities=SimpleNamespace(discard=lambda:None)
    session.vision=SimpleNamespace(stdin=SimpleNamespace(write=lambda _:None,drain=AsyncMock()),
        stdout=SimpleNamespace(readline=AsyncMock(return_value=json.dumps(detection).encode())))
    from tests.embodied.test_room_process import room_port
    session.room=room_port(registration=np.eye(3))
    engine=InteractionEngine('SYNTHETIC','synthetic-session')
    binding=Binding(id='1'*32,target='SYNTHETIC:light',gesture='body.hands_up',action='on',enabled=True)
    engine.config=Configuration(revision=0,enabled=True,bindings=[binding]);engine.loaded=0
    events=[]
    for index in range(10):
        clock[0]=index*.1
        observation=await session.observe(dict(received_at=1000+clock[0],
            jpeg=base64.b64encode(b'SYNTHETIC-NOT-CAPTURED-VIDEO').decode(),width=640,height=360,
            camera_source_context='a'*64,camera_source_name='合成来源'))
        assert observation['camera_source_context']=='a'*64
        assert observation['tracking_current'] is True
        people=observation['people']
        owner=people[0]['id'] if people else None
        events+=engine.update(people,[],owner,observation['registration'],True,clock[0])
    if overlap:
        assert events==[]
    else:
        assert len(events)==1
        assert events[0]['binding']==binding.id and events[0]['target']==binding.target
        assert events[0]['action']=='on' and events[0]['camera']=='SYNTHETIC'
        assert .7 <= events[0]['observed_mono'] <= .9
    assert len(session.last['people'])==(2 if overlap else 1)
