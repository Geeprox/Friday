"""使用合成图像、真实导演和两条原云台循环验证转动前后的目标。"""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from homeplane.embodied.camera_director import CameraDirector, visible_scene
from homeplane.embodied.camera_follow_appearance import find_follow_target, follow_reference_in_view, remember_follow_target
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.party_camera import PartyCamera
from tests.embodied.test_camera_director import patrol_view
from tests.embodied.test_review_motion import source_function


def subject(track, x, *, pets=False):
    row = dict(id=track, box=dict(x=x, y=.25, w=.2, h=.45, confidence=.94))
    if pets:
        row['species'] = 'dog'
    return row


def picture(subjects, *, textured_background=False):
    image = np.full((480, 800), 141, np.uint8)
    if textured_background:
        texture = np.random.default_rng(137).integers(18, 238, (96, 160), dtype=np.uint8)
        image = cv2.resize(texture, (800, 480), interpolation=cv2.INTER_NEAREST)
    for row, seed in subjects:
        box = row['box']
        left, top = round(box['x']*800), round(box['y']*480)
        width, height = round(box['w']*800), round(box['h']*480)
        if seed is None:
            image[top:top+height, left:left+width] = 141
            continue
        texture = np.random.default_rng(seed).integers(18, 238, (54, 40), dtype=np.uint8)
        image[top:top+height, left:left+width] = cv2.resize(texture, (width, height), interpolation=cv2.INTER_NEAREST)
    return cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 87])[1].tobytes()


def appearance_sample(at, subjects, *, x=0, basis='same-room'):
    return dict(_patrol_view=patrol_view(at, x=x, basis=basis), _follow_jpeg=picture(subjects))


def test_target_features_require_one_correspondence_in_the_same_calibrated_view():
    original = subject(1, .72)
    anchor = remember_follow_target(appearance_sample(1, [(original, 7)]), original)
    assert anchor is not None
    assert follow_reference_in_view(anchor, appearance_sample(2, []), original)
    assert not follow_reference_in_view(anchor, appearance_sample(2, [], basis='new-room'), original)
    assert not follow_reference_in_view(anchor, appearance_sample(2, []), subject(1, .05))
    assert not follow_reference_in_view(anchor, appearance_sample(2, []), subject('dog:1', .72, pets=True))
    target, second = subject(4, .49), subject(5, .72)
    visible = appearance_sample(4, [(target, 7), (second, 8)], x=.14)
    assert find_follow_target(anchor, visible, [target, second]) is target
    matching_both = appearance_sample(4, [(target, 7), (second, 7)], x=.14)
    assert find_follow_target(anchor, matching_both, [target, second]) is None
    changed_view = dict(visible, _patrol_view=patrol_view(4, x=.14, basis='new-room'))
    assert find_follow_target(anchor, changed_view, [target, second]) is None
    corrupt = dict(visible, _follow_jpeg=b'SYNTHETIC-CORRUPT-JPEG')
    assert find_follow_target(anchor, corrupt, [target, second]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
@pytest.mark.parametrize(('mode', 'weak_frame'), [
    ('auto', False), ('pets', False), ('person_target', False), ('pet_target', False),
    ('person_target', True), ('pet_target', True),
])
async def test_original_camera_loop_aims_twice_at_the_same_subject_despite_track_renumbering(owner, mode, weak_frame):
    pets = mode in {'pets', 'pet_target'}
    original = subject('dog:1' if pets else 1, .72, pets=pets)
    impostor = subject('dog:2' if pets else 2, .05, pets=pets)
    reacquired = subject('dog:3' if pets else 3, .60, pets=pets)
    walking = subject(reacquired['id'], .72, pets=pets)
    rows = iter([(0, [(original, 7)], 0), (.3, [(original, 7)], 0), (1, [], .14),
                 (2.7, [(impostor, 8)], .14), (3, [(impostor, 8)], .14), (3.4, [(impostor, 8)], .14),
                 (4.2, [(reacquired, 7)], .14), (4.5, [(reacquired, 7)], .14), (4.8, [(walking, 7)], .14)])
    director = CameraDirector()
    if mode in {'person_target', 'pet_target'}:
        director.apply_mode(mode, 'intent')
        director.select_target(dict(status='live', tracking_current=True, scene_visible=True,
            session_id='one-camera', _local_received=-.3, **appearance_sample(-.3, [(original, 7)])), original, -.3)
    state, moves, phases = {}, [], []
    item = dict(source_active=True, privacy_closed=False, control_epoch=1, mode=mode, mode_revision='intent')
    scope = {}
    clock = 0

    async def advance(_):
        nonlocal clock
        phases.append(director.phase)
        entry = next(rows, None)
        if entry is None:
            raise asyncio.CancelledError
        clock, subjects, offset = entry
        visible = [row for row, _ in subjects]
        low_detail = weak_frame and clock == .3
        if low_detail:
            subjects = [(row, None) for row, _ in subjects]
        jpeg = picture(subjects, textured_background=weak_frame)
        if low_detail:
            assert visible_scene(jpeg)
        basis = patrol_view(clock, x=offset, basis='same-room')
        scene = dict(tracking_current=True, animals_current=pets, animals_intent='intent',
                     people=[] if pets else visible, animals=visible if pets else [], scene_visible=True,
                     _patrol_view=basis)
        state.update(scene, status='live', session_id='one-camera', frame=base64.b64encode(jpeg).decode())
        scope.update(last_frame=clock, latest=dict(scene, session='one-camera', now=clock,
                                                  captured_mono=clock, safety_jpeg=jpeg))

    async def motor(direction, stamp, *_args, **kwargs):
        assert stamp == clock and director.pending_decision['appearance'] is not None
        if owner == 'primary':
            assert kwargs['decision_current']()
        else:
            assert director.decision_current(scope['current_sample'](), clock, direction)
        moves.append((direction, director.target))
        if owner == 'secondary':
            director.sent(clock, direction)

    quiet_safety = SimpleNamespace(review_pending=lambda *_: False, fall_pending=lambda *_: False,
                                   sound_search_cue=lambda *_: None)
    scope.update(time=SimpleNamespace(monotonic=lambda: clock),
                 asyncio=SimpleNamespace(sleep=advance, CancelledError=asyncio.CancelledError),
                 CameraDirector=lambda: director, camera_is_open=AsyncMock(return_value=True),
                 selected={'id': 'synthetic-camera'}, item=item, director=director)
    await advance(0)
    if owner == 'primary':
        lease = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock(return_value=True),
                                presets=[], active=None, event=lambda *_args, **_kwargs: None)
        planner = SimpleNamespace(public=lambda _: {})
        scope.update(Transport=lambda: SimpleNamespace(close=AsyncMock()), CameraLease=lambda *_a, **_kw: lease,
                     SearchPlanner=lambda *_: planner, camera_intents=SimpleNamespace(load=lambda: SimpleNamespace(
                         mode=mode, revision='intent', status='configured')), SELECTED=scope['selected'],
                     os=SimpleNamespace(environ={}), state=state, games=MotionGames(), party_camera=PartyCamera(),
                     previews=SimpleNamespace(sessions={}), home=SimpleNamespace(camera_focus=None),
                     safety=quiet_safety, motor_step=motor, reap=AsyncMock(), capture=None)
        loop = source_function('homeplane/embodied/worker.py', 'camera_search', scope)
    else:
        source_function('homeplane/embodied/previews.py', 'current_sample', scope)
        scope.update(self=SimpleNamespace(focus=lambda _: None, occupied_elsewhere=lambda _: False,
                     review_pending=quiet_safety.review_pending, fall_pending=quiet_safety.fall_pending,
                     sound_cue=quiet_safety.sound_search_cue), send_motion=motor, privacy_closed=lambda: None)
        loop = source_function('homeplane/embodied/previews.py', 'direct', scope)
    with pytest.raises(asyncio.CancelledError):
        await loop()
    assert moves == [('right', original['id']), ('right', reacquired['id'])]
    assert 'target_reacquiring' in phases


def test_unconfirmed_sole_subject_stays_lost_until_automatic_mode_is_selected_again():
    director = CameraDirector()
    director.apply_mode('auto', 'before')
    original, replacement = subject(1, .72), subject(2, .05)

    def frame(at, person):
        return dict(status='live', scene_visible=True, session_id='one-camera', _local_received=at,
                    people=[person], **appearance_sample(at, [(person, person['id'])]))

    assert director.choose(frame(0, original), 0) is None
    assert director.choose(frame(.3, original), .3) == 'right'
    director.sent(.3)
    for at in (3, 3.3, 4, 6, 9, 20):
        assert director.choose(frame(at, replacement), at) is None
    assert director.public()['phase'] == 'target_lost'
    assert director.reframe_appearance is None and director.target == 1
    director.apply_mode('auto', 'explicit-choice')
    assert director.choose(frame(20.3, replacement), 20.3) is None
    assert director.choose(frame(20.6, replacement), 20.6) == 'left'
