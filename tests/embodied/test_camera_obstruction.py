import cv2
import numpy as np
from homeplane.embodied.camera_director import CameraDirector,visible_scene


def view(at,visible):
    return dict(status='live',_local_received=at,scene_visible=visible,people=[],session_id='one')


def test_detail_in_small_corner_cannot_hide_mostly_obscured_scene():
    frame=np.full((360,640),160,np.uint8)
    frame[100:350,:80]=np.random.default_rng(1).integers(0,255,(250,80),np.uint8)
    assert not visible_scene(cv2.imencode('.jpg',frame)[1].tobytes())
    frame[:]=np.random.default_rng(2).integers(0,255,frame.shape,np.uint8)
    assert visible_scene(cv2.imencode('.jpg',frame)[1].tobytes())


def test_short_occlusion_does_not_move_and_repeated_frame_never_counts():
    d=CameraDirector();d.choose(view(0,True),0);d.choose(view(.1,True),.1)
    assert d.choose(view(1,False),1) is None
    assert d.choose(view(1,False),1.3) is None
    assert d.choose(view(1.5,True),1.5) is None
    assert d.recovery_steps==0


def test_clear_left_strip_does_not_qualify_mostly_blank_wall():
    frame=np.full((360,640),160,np.uint8)
    frame[:,:240]=np.random.default_rng(3).integers(0,255,(360,240),np.uint8)
    assert not visible_scene(cv2.imencode('.jpg',frame)[1].tobytes())


def test_turn_out_of_bad_pan_then_avoid_that_angle_on_patrol():
    d=CameraDirector();d.choose(view(0,True),0);d.choose(view(.1,True),.1)
    d.phase='patrolling';d.sent(1,'right')
    for at in (3,3.5):assert d.choose(view(at,False),at) is None
    assert d.choose(view(4,False),4)=='left'
    d.sent(4)
    assert d.choose(view(6,True),6) is None
    assert d.choose(view(6.5,True),6.5) is None
    assert d.choose(view(25,True),25)=='left'
    assert (1,0) in d.blocked_positions
    d.hold()
    assert d.position==(0,0) and not d.blocked_positions


def test_old_or_future_frames_cannot_recover_obstruction():
    d=CameraDirector()
    for at in range(30):
        assert d.choose(view(at-2,False),at) is None
        assert d.choose(view(at+1,False),at) is None
    assert d.recovery_steps==0
