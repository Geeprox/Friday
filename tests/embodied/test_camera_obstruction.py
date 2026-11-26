import cv2
import numpy as np
from homeplane.embodied.camera_audio import WakeCameraCue
from homeplane.embodied.camera_director import CameraDirector,visible_scene
from tests.embodied.test_camera_director import patrol_view


def view(at,visible):
    return dict(status='live',_local_received=at,scene_visible=visible,people=[],session_id='one',
                _patrol_view=patrol_view(at))


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


def restored_camera():
    d=CameraDirector();d.choose(view(0,True),0);d.choose(view(.1,True),.1)
    d.sent(1,'right',automatic=False)
    for at in (3,3.5):assert d.choose(view(at,False),at) is None
    assert d.choose(view(4,False),4)=='left'
    d.sent(4)
    assert d.choose(view(6,True),6) is None
    assert d.choose(view(6.5,True),6.5) is None
    return d


def test_turn_out_of_bad_pan_then_keep_the_recovered_view_before_patrolling():
    d = restored_camera()
    for at in range(7, 27):
        assert d.choose(view(at, True), at) is None
        assert d.public()['view_usable']
    assert d.choose(view(27, True), 27) == 'left'
    assert (1,0) in d.blocked_positions
    d.hold()
    assert d.position==(0,0) and not d.blocked_positions


def test_a_frame_gap_restarts_the_clear_interval_before_automatic_patrol():
    director = restored_camera()
    for at in (*range(7, 16), *range(17, 37)):
        assert director.choose(view(at, True), at) is None
    assert director.recovery_steps == 1
    assert director.choose(view(37, True), 37) == 'left'
    assert director.recovery_steps == 0


def test_current_wake_can_move_while_the_recovered_view_is_held_for_observation():
    director = restored_camera()
    for at in range(7, 18):
        assert director.choose(view(at, True), at) is None
    wake_source = ('synthetic-capture', 'synthetic-context')
    cue = WakeCameraCue(17.4, 'one', wake_source, 1, confirmed_mono=17.8)
    frame = dict(view(18, True), tracking_current=True, _wake_source=wake_source)
    direction = director.choose(frame, 18, sound_cue=cue)
    assert direction == 'left'
    assert director.decision_current(frame, 18, direction, sound_cue=cue)
    assert director.recovery_steps == 1


def test_old_or_future_frames_cannot_recover_obstruction():
    d=CameraDirector()
    for at in range(30):
        assert d.choose(view(at-2,False),at) is None
        assert d.choose(view(at+1,False),at) is None
    assert d.recovery_steps==0
