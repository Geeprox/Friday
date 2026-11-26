from homeplane.embodied.camera_audio import WakeCameraCue
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.camera_patrol import CameraPatrol
from tests.embodied.test_camera_obstruction import view


def frame(at, *, clear=True, tracking=True, state='live'):
    return dict(view(at, clear), status=state, tracking_current=tracking)


def test_patrol_reserves_the_view_until_a_current_post_move_frame():
    patrol = CameraPatrol()
    patrol.observe('main', frame(20), {'view_usable': True}, 20)
    patrol.observe('side', frame(20), {'view_usable': True}, 20)
    assert patrol.allows('main', 20)
    patrol.dispatched('main', 20)
    assert not patrol.allows('side', 20)
    patrol.observe('main', frame(21.9), {'view_usable': True}, 21.9)
    assert not patrol.allows('side', 21.9)
    patrol.observe('main', frame(22), {'view_usable': False}, 22)
    assert not patrol.allows('side', 22)
    patrol.observe('main', frame(22.2), {'view_usable': True}, 22.2)
    assert patrol.allows('side', 22.2)


def test_offline_or_closed_peer_releases_the_last_view_to_patrol():
    patrol = CameraPatrol()
    patrol.observe('side', frame(12, tracking=False), {'view_usable': True}, 12)
    assert not patrol.allows('main', 12)
    patrol.observe('side', frame(11.5), {'view_usable': True}, 12)
    assert not patrol.allows('main', 12)
    patrol.observe('side', frame(12.1), {'mode': 'hold'}, 12.1)
    assert patrol.allows('main', 12.1)
    patrol.observe('side', frame(12.2, clear=False), {'mode': 'hold'}, 12.2)
    assert not patrol.allows('main', 12.2)
    assert patrol.allows('main', 14)
    patrol.observe('side', frame(14.1, tracking=False), {}, 14.1)
    assert not patrol.allows('main', 14.1)
    patrol.observe('side', frame(14.2, state='shutter_closed'), {}, 14.2)
    assert patrol.allows('main', 14.2)


def test_unconfirmed_motion_does_not_block_patrol_forever():
    patrol = CameraPatrol()
    patrol.dispatched('side', 20)
    patrol.forget_view('side')
    assert not patrol.allows('main', 30.9)
    assert patrol.allows('main', 31)


def test_patrol_yields_while_wake_and_obscured_view_can_move():
    director = CameraDirector()
    director.choose(frame(0), 0)
    director.choose(frame(.1), .1)
    assert director.choose(frame(16), 16, ordinary_patrol_allowed=False) is None
    assert director.public()['phase'] == 'patrol_deferred'
    assert director.public()['view_usable']
    assert director.choose(frame(16.2), 16.2) == 'right'
    assert director.ordinary_patrol_pending()
    assert director.decision_current(frame(16.2), 16.2, 'right')

    wake = CameraDirector()
    wake.choose(frame(0), 0)
    wake.choose(frame(.1), .1)
    capture = ('synthetic-capture', 'synthetic-context')
    cue = WakeCameraCue(15.4, 'one', capture, 1, confirmed_mono=15.8)
    current = dict(frame(16), _wake_source=capture)
    direction = wake.choose(current, 16, sound_cue=cue, ordinary_patrol_allowed=False)
    assert direction in {'left', 'right'} and wake.phase == 'sound_search'
    assert not wake.ordinary_patrol_pending()
    assert wake.decision_current(current, 16, direction, sound_cue=cue)

    blocked = CameraDirector()
    blocked.choose(frame(0), 0)
    blocked.choose(frame(.1), .1)
    for at in (1, 1.5):
        assert blocked.choose(frame(at, clear=False), at, ordinary_patrol_allowed=False) is None
    assert blocked.choose(frame(2, clear=False), 2, ordinary_patrol_allowed=False) == 'right'
    assert not blocked.ordinary_patrol_pending()
