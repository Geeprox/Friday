"""Actual WindowGesture with procedural joints, no identity/model/device I/O."""
import pytest
from homeplane.embodied.smart_home import WindowGesture


def person(opened,track=7):
    coords={'leftShoulder':(.4,.3),'rightShoulder':(.6,.3),
        'leftHip':(.4,.6),'rightHip':(.6,.6),
        'leftWrist':(.1 if opened else .45,.32),
        'rightWrist':(.9 if opened else .55,.32)}
    return dict(id=track,joints={name:dict(x=x,y=y,confidence=.95)
        for name,(x,y) in coords.items()})


@pytest.mark.parametrize('interval',[.4,.5,.7])
def test_fresh_low_cadence_full_paired_movement_emits_exactly_one_close(interval):
    gesture=WindowGesture();events=[]
    for index,opened in enumerate([True,True,False,False,False,False]):
        event=gesture.update(person(opened),100.+index*interval)
        if event:events.append(event)
    assert [event['action'] for event in events]==['close']
    assert events[0]['person_id']==7


def test_source_gap_does_not_reuse_old_open_anchor():
    gesture=WindowGesture()
    for at in [100.,100.1,100.2]:assert gesture.update(person(True),at) is None
    for at in [102.,102.1,102.2,102.3]:assert gesture.update(person(False),at) is None


def test_static_open_pose_and_track_replacement_never_emit_close():
    gesture=WindowGesture()
    for n in range(10):assert gesture.update(person(True),100.+n*.1) is None
    for n in range(10):assert gesture.update(person(False,track=8),101.+n*.1) is None


def test_only_one_arm_moving_inward_is_not_paired_close():
    gesture=WindowGesture()
    for n in range(5):assert gesture.update(person(True),100.+n*.1) is None
    one_arm=person(False);one_arm['joints']['rightWrist']['x']=.9
    for n in range(5):assert gesture.update(one_arm,100.5+n*.1) is None
