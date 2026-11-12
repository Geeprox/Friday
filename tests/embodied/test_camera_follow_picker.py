"""使用合成帧验证镜头点选，保留实际 API 和跟随授权逻辑。"""

import base64
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from homeplane.embodied import service as service_module
from homeplane.embodied.api import create_router
from homeplane.embodied.camera_director import CameraDirector, visible_scene
from homeplane.embodied.camera_follow_picker import CameraFollowPicker, FollowChoose, FollowCommand, FollowPrepare, FollowStop
from homeplane.embodied.camera_intent import CameraIntentStore
from homeplane.embodied.previews import PreviewPool
from homeplane.embodied.service import EmbodiedService
from tests.embodied.test_camera_director import patrol_view
from tests.embodied.test_camera_follow_appearance import picture, subject
from tests.embodied.test_review_motion import source_function


class Camera:
    def __init__(self, directory, camera_id='cam_1111111111'):
        self.id = camera_id
        self.now = 100.
        self.state = {}
        self.director = CameraDirector()
        self.store = CameraIntentStore(directory, {'id': camera_id, 'device': {'did': 'synthetic'}})
        self.set_mode('auto')
        self.picker = CameraFollowPicker(self.director, lambda: self.state, self.intent, self.set_mode,
                                         clock=lambda: self.now)
        self.left, self.right = subject(1, .05), subject(2, .72)

    def set_mode(self, mode):
        self.setting = self.store.save(mode)
        self.director.apply_mode(*self.intent())

    def intent(self):
        return self.setting.mode, self.setting.revision

    def frame(self, delta=0., *, people=None, animals=(), **overrides):
        self.now += delta
        people = [self.right, self.left] if people is None else people
        jpeg = picture([(row, index) for index, row in enumerate([*people, *animals]) if row.get('box')])
        self.state = dict(status='live', session_id='synthetic-capture', _local_received=self.now, tracking_current=True,
                          scene_visible=True, _patrol_view=patrol_view(self.now), people=people, animals=list(animals),
                          animals_current=bool(animals), animals_intent=self.intent()[1], _follow_jpeg=jpeg)
        self.state.update(overrides)
        return self.state

    def prepare(self, kind='person'):
        return self.picker.command(FollowPrepare(action='prepare', kind=kind, revision=self.intent()[1]))


def test_only_the_selected_subject_can_move_the_camera(tmp_path):
    camera = Camera(tmp_path / 'intent')
    director = camera.director
    camera.frame()
    prepared = camera.prepare()
    assert prepared == {'mode': 'person_target', 'intent_revision': camera.intent()[1]}
    assert camera.store.load().mode == 'hold'
    for _ in range(3):
        assert director.choose(camera.frame(.3), camera.now) is None
    assert director.public()['phase'] == 'target_selecting'

    view = camera.picker.snapshot()
    assert base64.b64decode(view['jpeg']) == camera.state['_follow_jpeg']
    assert view['choices'] == [
        {'choice': index, 'species': None, 'box': {key: row['box'][key] for key in ('x', 'y', 'w', 'h')}}
        for index, row in enumerate([camera.left, camera.right], 1)
    ]
    choose = FollowChoose(action='choose', snapshot=view['snapshot'], choice=2)
    assert camera.picker.command(choose)['status'] == 'selected'
    with pytest.raises(ValueError, match='follow_target_changed'):
        camera.picker.command(choose)

    focus = dict(track=1, session=camera.state['session_id'], at=camera.now)
    assert director.choose(camera.state, camera.now, focus=focus) is None
    assert director.choose(camera.frame(.3), camera.now, focus=focus) is None
    assert director.choose(camera.frame(.3), camera.now, focus=focus) == 'right'
    assert director.target == 2 and director.decision_current(camera.state, camera.now, 'right', focus=focus)
    revised = dict(camera.right, tracking={'follow_revision': 1, 'overlapping': False})
    latest = dict(camera.state, people=[camera.left, revised], _local_received=camera.now+.1)
    assert not director.decision_current(latest, camera.now+.1, 'right', focus=focus)
    director.sent(camera.now, 'right')
    for delta in (2.5, .3, .3, 12., 14.):
        assert director.choose(camera.frame(delta, people=[camera.left]), camera.now) is None
    assert director.public()['reason'] == '未找到所选目标'
    assert director.choose(camera.frame(.3, session_id='new-capture'), camera.now) is None
    stopped = camera.picker.command(FollowStop(action='stop', revision=camera.intent()[1]))
    assert stopped['mode'] == camera.store.load().mode == 'hold'


def test_pet_pick_waits_for_the_current_inference_revision(tmp_path):
    camera = Camera(tmp_path / 'intent')
    dog, cat = subject('dog:1', .72, pets=True), dict(subject('cat:1', .05), species='cat')
    old = camera.intent()[1]
    camera.frame(animals=[dog, cat])
    camera.prepare('pet')
    camera.frame(.3, animals=[dog, cat], animals_intent=old)
    assert camera.picker.snapshot()['status'] == 'waiting'
    camera.frame(.3, animals=[dog, cat], animals_current=False)
    assert camera.picker.snapshot()['status'] == 'waiting'
    camera.frame(.3, animals=[dog, cat])
    view = camera.picker.snapshot()
    assert [entry['species'] for entry in view['choices']] == ['cat', 'dog']
    assert camera.picker.command(FollowChoose(action='choose', snapshot=view['snapshot'], choice=1))['status'] == 'selected'
    assert camera.director.choose(camera.frame(.3, animals=[cat, dog]), camera.now) is None
    assert camera.director.choose(camera.frame(.3, animals=[cat, dog]), camera.now) == 'left'
    assert camera.director.target == 'cat:1'


@pytest.mark.parametrize('changed', ['stale', 'expired', 'session', 'revision', 'moved', 'game',
                                    'duplicate', 'gone', 'identity', 'continuity', 'overlap'])
def test_old_choices_cannot_grant_motion(tmp_path, changed):
    camera = Camera(tmp_path / 'intent')
    camera.right['identity'] = {'state': 'tracked', 'person_id': 'synthetic-one'}
    camera.frame(_follow_motion=4)
    camera.prepare()
    view = camera.picker.snapshot()
    command = FollowChoose(action='choose', snapshot=view['snapshot'], choice=2)
    if changed == 'stale':
        camera.now += 2
    elif changed == 'expired':
        camera.frame(8, _follow_motion=4)
    elif changed == 'revision':
        camera.prepare()
    elif changed == 'duplicate':
        camera.state['people'].append(dict(camera.right, box=None))
    elif changed == 'gone':
        camera.state['people'].remove(camera.right)
    elif changed == 'identity':
        camera.right['identity']['person_id'] = 'synthetic-other'
    elif changed in {'continuity', 'overlap'}:
        camera.right['tracking'] = {'follow_revision': 1, 'overlapping': changed == 'overlap'}
    else:
        camera.state.update({'session': {'session_id': 'new-capture'}, 'moved': {'_follow_motion': 5},
                             'game': {'party_camera': {'active': True, 'scope': 'synthetic'}}}[changed])
    with pytest.raises(ValueError, match='follow_target_changed'):
        camera.picker.command(command)
    assert camera.director.target is None and camera.director.pending_direction is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
async def test_public_api_relays_the_pick_to_the_camera_owner(tmp_path, monkeypatch, owner):
    primary_id, secondary_id = 'cam_1111111111', 'cam_2222222222'
    camera = Camera(tmp_path / 'intent', primary_id if owner == 'primary' else secondary_id)
    initial_frame = camera.frame()
    if owner == 'primary':
        initial_frame['frame'] = base64.b64encode(initial_frame.pop('_follow_jpeg')).decode()
    process = SimpleNamespace(returncode=None)
    previews = object.__new__(PreviewPool)
    previews.sessions = {secondary_id: dict(source_active=True, process=process, state='live', follow_picker=camera.picker)}
    scope = dict(SELECTED={'id': primary_id}, previews=previews, HTTPException=HTTPException, Request=Request,
                 JSONResponse=JSONResponse, FollowCommand=FollowCommand,
                 camera_planner=SimpleNamespace(follow_picker=camera.picker),
                 camera_lease=SimpleNamespace(snapshot={'status': 'owned'}))
    worker_source = 'homeplane/embodied/worker.py'
    source_function(worker_source, 'follow_picker', scope)
    worker = FastAPI()
    worker_path = '/api/previews/{camera_id}/follow-target'
    worker.add_api_route(worker_path, source_function(worker_source, 'follow_target_view', scope), methods=['GET'])
    worker.add_api_route(worker_path, source_function(worker_source, 'follow_target_command', scope), methods=['POST'])
    allowed = {'id': camera.id}
    monkeypatch.setattr(service_module, 'camera', lambda camera_id: allowed if camera_id == camera.id else None)
    monkeypatch.setattr(service_module, 'configuration', lambda: {'viewer_port': 18743})
    service = object.__new__(EmbodiedService)
    service._url = lambda path: 'http://127.0.0.1:18743' + path
    service._game_lifecycle, service.selected, service.process = 1, primary_id, process
    service.closed, service.capture_enabled, service.recovery_pending = False, True, False
    service.manual_previews = SimpleNamespace(has=lambda camera_id: False)
    app = FastAPI()
    app.include_router(create_router(service, None, None))
    path = f'/api/embodied/cameras/{camera.id}/follow-target'
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=worker)) as service.client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as ui,
    ):
        assert (await ui.get(path)).status_code == 409
        body = dict(action='prepare', kind='person', revision=camera.intent()[1])
        assert (await ui.post(path, json=dict(body, track=2))).status_code == 422
        assert (await ui.post(path, json=body)).json()['mode'] == 'person_target'
        response = await ui.get(path)
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        view = response.json()
        assert view['status'] == 'ready'
        choose = dict(action='choose', snapshot=view['snapshot'], choice=2)
        assert (await ui.post(path, json=choose)).json()['status'] == 'selected'
        assert camera.director.target == camera.right['id']
        assert (await ui.post(path, json=choose)).status_code == 409
        assert (await ui.post(path, json=dict(action='stop', revision=view['intent_revision']))).json()['mode'] == 'hold'
        service.capture_enabled = False
        assert (await ui.get(path)).status_code == 409


def test_explicit_follow_requires_two_new_observations_containing_the_selected_subject(tmp_path):
    camera = Camera(tmp_path / 'intent')
    camera.frame()
    camera.prepare()
    view = camera.picker.snapshot()
    camera.picker.command(FollowChoose(action='choose', snapshot=view['snapshot'], choice=2))
    assert camera.director.choose(camera.frame(.3, people=[camera.left]), camera.now) is None
    assert camera.director.choose(camera.frame(.3), camera.now) is None
    assert camera.director.choose(camera.frame(.3), camera.now) == 'right'


@pytest.mark.parametrize('interruption', ['missing', 'tracking', 'time_gap', 'crossed_between_ticks'])
def test_stationary_follow_ignores_reused_track_and_recovers_only_the_original_appearance(tmp_path, interruption):
    camera = Camera(tmp_path / 'intent')
    director = camera.director
    original = subject(2, .42)
    impostor = subject(2, .72)
    restored = subject(9, .72)

    def frame(delta, *rows, **overrides):
        return camera.frame(delta, people=[person for person, _ in rows], _follow_jpeg=picture(rows), **overrides)

    frame(0, (original, 7))
    camera.prepare()
    view = camera.picker.snapshot()
    camera.picker.command(FollowChoose(action='choose', snapshot=view['snapshot'], choice=1))
    for _ in range(2):
        assert director.choose(frame(.3, (original, 7)), camera.now) is None
    assert director.public()['phase'] == 'following'
    if interruption == 'missing':
        assert director.choose(frame(.3), camera.now) is None
    elif interruption == 'tracking':
        assert director.choose(frame(.3, (impostor, 8), tracking_current=False), camera.now) is None
    elif interruption == 'crossed_between_ticks':
        impostor['tracking'] = {'follow_revision': 1, 'overlapping': False}
    gap = 1.8 if interruption == 'time_gap' else .3
    assert director.choose(frame(gap, (impostor, 8)), camera.now) is None
    assert director.choose(frame(.3, (impostor, 8)), camera.now) is None
    assert director.choose(frame(.3, (restored, 7)), camera.now) is None
    assert director.choose(camera.state, camera.now + .1) is None
    if interruption == 'crossed_between_ticks':
        restored['tracking'] = {'follow_revision': 1, 'overlapping': False}
        assert director.choose(frame(.3, (restored, 7)), camera.now) is None
    assert director.choose(frame(.3, (restored, 7)), camera.now) == 'right'
    assert director.target == 9 and director.decision_current(camera.state, camera.now, 'right')


@pytest.mark.parametrize('weak_frames', [7, 24, 29])
def test_low_detail_frames_cannot_erase_or_renew_last_verified_reference(tmp_path, weak_frames):
    camera = Camera(tmp_path / 'intent')
    director = camera.director
    original, other, recovered = subject(2, .42), subject(2, .72), subject(9, .72)
    low_detail = picture([(original, None)], textured_background=True)
    assert visible_scene(low_detail)

    camera.frame(people=[original], _follow_jpeg=picture([(original, 7)]))
    camera.prepare()
    view = camera.picker.snapshot()
    camera.picker.command(FollowChoose(action='choose', snapshot=view['snapshot'], choice=1))
    for _ in range(weak_frames):
        assert director.choose(camera.frame(.3, people=[original], _follow_jpeg=low_detail), camera.now) is None
    assert director.public()['phase'] == 'following'
    assert director.choose(camera.frame(.3, people=[]), camera.now) is None
    for _ in range(2):
        assert director.choose(camera.frame(.3, people=[other], _follow_jpeg=picture([(other, 8)])), camera.now) is None
    assert director.choose(camera.frame(.3, people=[recovered], _follow_jpeg=picture([(recovered, 7)])), camera.now) is None
    result = director.choose(camera.frame(.3, people=[recovered], _follow_jpeg=picture([(recovered, 7)])), camera.now)
    if weak_frames == 7:
        assert result == 'right' and director.target == 9
    else:
        assert result is None and director.target == 2


def test_explicit_follow_reconfirms_a_known_person_before_dispatching_on_a_new_track(tmp_path):
    camera = Camera(tmp_path / 'intent')
    director = camera.director
    identity = dict(state='matched', person_id='synthetic-person')
    target = dict(camera.right, identity=identity)
    camera.frame(people=[target])
    camera.prepare()
    view = camera.picker.snapshot()
    camera.picker.command(FollowChoose(action='choose', snapshot=view['snapshot'], choice=1))
    assert director.choose(camera.frame(.3, people=[target]), camera.now) is None
    assert director.choose(camera.frame(.3, people=[target]), camera.now) == 'right'
    replacement = dict(target, id=6)
    assert not director.decision_current(camera.frame(.3, people=[replacement]), camera.now, 'right')
    assert director.choose(camera.state, camera.now) is None
    assert director.pending_decision is None
    assert director.choose(camera.frame(.3, people=[replacement, dict(replacement, id=7)]), camera.now) is None
    assert director.choose(camera.frame(.3, people=[replacement]), camera.now) is None
    assert director.choose(camera.frame(.3, people=[replacement]), camera.now) == 'right'
    assert director.target == 6 and director.decision_current(camera.state, camera.now, 'right')

def test_body_continuity_cannot_reuse_face_evidence_that_predates_crossing(tmp_path):
    camera = Camera(tmp_path / 'intent')
    director = camera.director
    identity = dict(state='matched', person_id='synthetic-person', face_observed_monotonic=camera.now)
    target = dict(camera.right, identity=identity)
    camera.frame(people=[target])
    camera.prepare()
    view = camera.picker.snapshot()
    camera.picker.command(FollowChoose(action='choose', snapshot=view['snapshot'], choice=1))
    assert director.choose(camera.frame(.3, people=[target]), camera.now) is None
    assert director.choose(camera.frame(.3, people=[target]), camera.now) == 'right'
    resumed = dict(target, tracking={'follow_revision': 1, 'overlapping': False,
                                    'follow_changed_at': camera.now+.2})
    resumed['identity'] = dict(identity, state='tracked')
    for _ in range(3):
        assert director.choose(camera.frame(.3, people=[resumed]), camera.now) is None
        assert director.pending_decision is None
    resumed['identity'] = dict(identity, face_observed_monotonic=camera.now+.1)
    assert director.choose(camera.frame(.3, people=[resumed]), camera.now) is None
    assert director.choose(camera.frame(.3, people=[resumed]), camera.now) == 'right'
