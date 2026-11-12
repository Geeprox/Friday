"""Original patrol loops and visual review/private store, with inert camera ports."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied.camera_audio import WakeCameraCue
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.camera_intent import camera_intent_readback, finish_camera_survey
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.party_camera import PartyCamera
from tests.test_visual_followup import Home
from tests.embodied.test_camera_director import patrol_view, person
from tests.embodied.test_sound_review import settle


def source_function(path, name, scope):
    node = next(n for n in ast.walk(ast.parse(Path(path).read_text()))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), path, 'exec'), scope)
    return scope[name]


def recorded_sound_only(safety):
    scope = dict(safety=safety, interaction_enabled=False, WakeCameraCue=WakeCameraCue)
    return source_function('homeplane/embodied/worker.py', 'camera_motion_cue', scope)


class Patrol:
    def __init__(self, home, owner, *, mode='auto', duration=80, activity=None):
        self.home, self.owner = home, owner
        self.mode, self.duration = mode, duration
        self.people = [person(1, .08)] if activity == 'same_camera' else []
        self.occupied_elsewhere = activity == 'other_camera'
        self.marker, self.camera, self.context = home.feeds[0]
        self.moves, self.phases = [], []
        self.exit_state = None
        self.moving_until = 0
        self.view_offset = 0.
        self.view_height = 0.
        self.session = 'SYNTHETIC-review-motion'
        self.state = {}
        self.director = CameraDirector()
        self.item = dict(privacy_closed=False, source_active=True, control_epoch=1, mode=mode, mode_revision='intent')
        self.scope = dict(time=SimpleNamespace(monotonic=lambda:home.clock),
                          asyncio=SimpleNamespace(sleep=self.advance, CancelledError=asyncio.CancelledError),
                          CameraDirector=lambda:self.director, camera_is_open=AsyncMock(return_value=True))

    async def advance(self, _):
        home = self.home
        home.clock += .5
        if home.clock > self.duration:
            raise asyncio.CancelledError
        task = home.runtime.visual.task
        if home.engine.release_due() and task:
            await task
            await settle()
        stable = home.clock >= self.moving_until
        await home.runtime.observe(self.camera, self.session, self.people if stable else [], home.clock, home.clock,
            stable=stable, jpeg=bytes([self.marker])+b'SYNTHETIC-FRAME',
            source_context=self.context, source_name='合成镜头')
        self.state.update(status='live', _local_received=home.clock, session_id=self.session,
                          tracking_current=True, scene_visible=stable, people=self.people if stable else [],
                          _patrol_view=patrol_view(home.clock, x=self.view_offset, y=self.view_height) if stable else None)
        self.scope['last_frame'] = home.clock
        self.phases.append((home.clock, (self.state if self.owner=='primary' else self.item).get('camera_control',{}).get('phase')))
        await settle()

    async def motor(self, direction, *_, **kwargs):
        if kwargs.get('decision_current') and not kwargs['decision_current']():
            raise RuntimeError('stale_input')
        if self.owner == 'secondary' and not self.director.decision_current(
                self.state, self.home.clock, direction, occupied_elsewhere=self.occupied_elsewhere,
                review_pending=self.home.runtime.review_pending(self.camera, self.session),
                fall_pending=self.home.runtime.fall_pending(self.camera, self.session)):
            raise RuntimeError('stale_input')
        self.moves.append((self.home.clock, direction))
        self.moving_until = self.home.clock + 1
        self.view_offset += .05 * ((direction == 'right') - (direction == 'left'))
        self.view_height += .05 * ((direction == 'down') - (direction == 'up'))
        self.home.runtime.clear(self.camera)
        if self.owner == 'secondary':
            self.director.sent(self.home.clock, direction)
        return {'native_code':0}

    async def run(self):
        await self.advance(0)
        if self.owner == 'primary':
            async def release():
                self.exit_state = dict(self.state.get('camera_control',{}))
                return True
            lease = SimpleNamespace(acquire=AsyncMock(), release=release,
                                    presets=[], active=None, event=lambda *_,**__:None)
            planner = SimpleNamespace(public=lambda _: {'mode': self.mode}, mode=self.mode, intent_revision='intent',
                                      intent_status='configured')
            other_views = {'synthetic-other': {'people': 1}} if self.occupied_elsewhere else {}
            self.scope.update(Transport=lambda:SimpleNamespace(close=AsyncMock()), CameraLease=lambda *a,**k:lease,
                SearchPlanner=lambda *a:planner, camera_intents=SimpleNamespace(load=lambda:SimpleNamespace(mode=self.mode,revision='intent',status='configured')),
                os=SimpleNamespace(environ={}), SELECTED={'id':self.camera}, state=self.state,
                games=MotionGames(), party_camera=PartyCamera(),
                previews=SimpleNamespace(sessions=other_views, status=lambda _: {'state': 'live'}),
                home=SimpleNamespace(camera_focus=None),
                safety=self.home.runtime, camera_motion_cue=recorded_sound_only(self.home.runtime),
                motor_step=self.motor, camera_lease=None, camera_planner=None,
                reap=AsyncMock(),capture=None, camera_intent_readback=camera_intent_readback,
                finish_camera_survey=finish_camera_survey)
            loop=source_function('homeplane/embodied/worker.py','camera_search',self.scope)
        else:
            self.scope.update(item=self.item, selected={'id':self.camera}, director=self.director,
                self=SimpleNamespace(focus=lambda _:None, occupied_elsewhere=lambda _:self.occupied_elsewhere,
                                     review_pending=self.home.runtime.review_pending,
                                     fall_pending=self.home.runtime.fall_pending,
                                     sound_cue=recorded_sound_only(self.home.runtime)),
                current_sample=lambda:dict(self.state), privacy_closed=lambda:None, send_motion=self.motor)
            loop=source_function('homeplane/embodied/previews.py','direct',self.scope)
        try:
            try:
                await loop()
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError(('original_loop_exited', self.exit_state))
        finally:
            await self.home.runtime.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
@pytest.mark.parametrize('activity', [None, 'same_camera', 'other_camera'])
async def test_original_owners_complete_a_two_level_survey(tmp_path, monkeypatch, owner, activity):
    home = Home(tmp_path, monkeypatch)
    home.store.configure(home.store.policy().model_copy(update={'visual_review': False}))
    patrol = Patrol(home, owner, mode='survey', duration=155, activity=activity)
    try:
        await patrol.run()
        assert [direction for _, direction in patrol.moves] == [
            'right', 'right', 'down', 'left', 'left', 'left', 'left', 'up', 'right', 'right',
        ]
        report = patrol.exit_state if owner == 'primary' else patrol.item['camera_control']
        assert report['survey_result'] == 'complete' and report['survey_confirmed_moves'] == 10
        assert report['physical_motion_verified'] is False
        assert any(phase == 'confirming_view' for _, phase in patrol.phases)
    finally:
        await home.runtime.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
async def test_original_owners_run_a_scheduled_survey_without_a_viewer(tmp_path, monkeypatch, owner):
    home = Home(tmp_path, monkeypatch)
    home.store.configure(home.store.policy().model_copy(update={'visual_review': False}))
    patrol = Patrol(home, owner, mode='patrol', duration=156, activity='other_camera')
    patrol.people = [person(1, .42)]
    patrol.director.SCHEDULE_INTERVAL = 20
    try:
        await patrol.run()
        assert tuple(direction for _, direction in patrol.moves) == patrol.director.SURVEY_ROUTE
        assert patrol.moves[0][0] >= patrol.director.SCHEDULE_INTERVAL
        report = patrol.exit_state if owner == 'primary' else patrol.item['camera_control']
        assert report['mode'] == 'patrol' and report['survey_result'] == 'complete'
        assert report['survey_confirmed_moves'] == 10 and not report['scheduled_survey_active']
        assert report['physical_motion_verified'] is False
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner',['primary','secondary'])
@pytest.mark.parametrize('mode',['auto','survey'])
async def test_patrol_preserves_a_positive_review_and_resumes_afterward(tmp_path,monkeypatch,owner,mode):
    home=Home(tmp_path,monkeypatch)
    patrol=Patrol(home,owner,mode=mode,activity='other_camera' if mode=='survey' else None)
    try:
        await patrol.run()
        events=home.store.events()
        assert events, (owner, 'patrol erased every corroboration', patrol.moves, home.engine.windows)
        assert all(e['kind']=='visual_review' and len(e['frames'])==8 for e in events)
        assert home.engine.maximum==1
        held_at=next(at for at,phase in patrol.phases if phase=='review_held')
        assert any(at>held_at for at,_ in patrol.moves), 'completed review must not disable patrol permanently'
        assert patrol.director.scan_steps or patrol.director.survey_confirmed_moves
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


@pytest.mark.parametrize('mode',['auto','survey'])
def test_changing_candidates_cannot_extend_the_hold_or_keep_cancelling_dispatch(mode):
    from tests.embodied.test_camera_director import sample
    director=CameraDirector();director.apply_mode(mode,'SYNTHETIC')
    director.choose(sample(0),0);director.choose(sample(.1),.1)
    assert director.choose(sample(1),1,review_pending=True) is None
    assert director.phase=='review_held' and director.public()['view_usable']
    director.choose(sample(4),4,review_pending=False)
    for at in (5,10,15,20,30.9):
        assert director.choose(sample(at),at,review_pending=True) is None
        assert director.phase=='review_held'
    assert director.choose(sample(31),31,review_pending=True)=='right'
    assert director.decision_current(sample(31.1),31.1,'right',review_pending=True)
    assert director.survey_attempts==int(mode=='survey')


def test_manual_pet_game_and_expired_source_keep_their_original_priority():
    from tests.embodied.test_camera_director import sample,person
    director=CameraDirector();director.apply_mode('hold','manual')
    assert director.choose(sample(1),1,review_pending=True) is None and director.phase=='held'
    director.apply_mode('pets','pet');pet=dict(sample(2),animals_current=True,animals_intent='pet',animals=[person(1,.9)])
    assert director.choose(pet,2,review_pending=True) is None
    pet.update(_local_received=2.2)
    assert director.choose(pet,2.2,review_pending=True)=='right'
    assert not director.decision_current(pet,2.2,'right',fall_pending=True)
    director.apply_mode('auto','next');active=dict(sample(3),game={'mode':'ski'})
    assert director.choose(active,3,review_pending=True) is None and director.phase=='game_held'
    assert director.choose(sample(3),5,review_pending=True) is None and director.phase=='waiting_video'


async def candidate(home):
    home.feeds=home.feeds[:1]
    for index in range(1,29):
        await home.tick(index)
    camera=home.feeds[0][1]
    assert home.runtime.review_pending(camera,'SYNTHETIC-visual')
    return camera


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['session','camera','stale','policy','clear','token','context'])
async def test_only_current_positive_same_source_can_request_stability(tmp_path,monkeypatch,change):
    home=Home(tmp_path,monkeypatch)
    try:
        camera=await candidate(home);session='SYNTHETIC-visual'
        if change=='session':session='different'
        elif change=='camera':camera='cam_9999999999'
        elif change=='stale':home.clock+=2
        elif change=='policy':
            home.store.configure(home.store.policy().model_copy(update={'visual_review':False}))
            await home.runtime.sync_policy()
        elif change=='clear':home.runtime.clear(camera)
        elif change=='token':home.runtime.source_tokens[camera].set()
        elif change=='context':home.runtime.frames[camera]['source_context']=None
        assert not home.runtime.review_pending(camera,session)
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_active_initial_analysis_is_protected_and_recorded_duplicates_release_the_view(tmp_path,monkeypatch):
    home=Home(tmp_path,monkeypatch);home.feeds=home.feeds[:1];camera=home.feeds[0][1]
    try:
        for index in range(1,8):
            await home.tick(index)
        assert home.runtime.review_pending(camera,'SYNTHETIC-visual')
        for index in range(8,49):
            await home.tick(index)
        # 镜头在留档回执送回事件循环后才放行。
        await asyncio.wait_for(home.runtime.drain(), 2)
        assert len(home.store.events())==1
        assert not home.runtime.review_pending(camera,'SYNTHETIC-visual')
        assert home.engine.maximum==1
    finally:
        await home.runtime.close()


def test_original_secondary_pool_constructor_binds_the_runtime_hint(tmp_path):
    noop=lambda *_:None
    safety=SimpleNamespace(review_pending=noop,fall_pending=noop,sound_search_cue=noop,
                           offer_sound_search_frame=lambda *_, **__: False, capture_frame=noop)
    tree=ast.parse(Path('homeplane/embodied/worker.py').read_text())
    assignment=next(n for n in tree.body if isinstance(n,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='previews'for t in n.targets))
    scope=dict(PreviewPool=lambda **kwargs:kwargs,identity_store=None,wall=None,safety=safety,
               secondary_observation=noop,secondary_audio=noop,clear_camera=noop,clear_camera_view=noop,
               clear_camera_observation=noop,
               camera_motion_cue=recorded_sound_only(safety))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[assignment],type_ignores=[])),'original-preview-construction','exec'),scope)
    assert scope['previews']['review_pending'] is safety.review_pending
    assert scope['previews']['fall_pending'] is safety.fall_pending
    assert scope['previews']['sound_cue'] is scope['camera_motion_cue']
    assert scope['previews']['sound_view'] is safety.offer_sound_search_frame
    assert scope['previews']['on_capture_frame'] is safety.capture_frame
    assert scope['previews']['on_motion'] is scope['clear_camera_view']
    assert scope['previews']['on_view_lost'] is scope['clear_camera_observation']


async def patrol_fleet(home,steps=480):
    directors={camera:CameraDirector()for _,camera,_ in home.feeds}
    moving_until={camera:0 for _,camera,_ in home.feeds}
    view_offset={camera:0. for camera in directors}
    for index in range(1,steps+1):
        home.clock=index*.5
        task=home.runtime.visual.task
        if home.engine.release_due()and task:
            await task
            await settle()
        for marker,camera,context in home.feeds:
            stable=home.clock>=moving_until[camera]
            await home.runtime.observe(camera,'SYNTHETIC-visual',[],home.clock,home.clock,
                stable=stable,jpeg=bytes([marker])+b'SYNTHETIC-FRAME',source_context=context)
            director=directors[camera];director.apply_mode('auto','SYNTHETIC')
            sample=dict(status='live',tracking_current=True,scene_visible=stable,people=[],
                        session_id='SYNTHETIC-visual',_local_received=home.clock,
                        _patrol_view=patrol_view(home.clock, x=view_offset[camera]) if stable else None)
            pending=home.runtime.review_pending(camera,'SYNTHETIC-visual')
            direction=director.choose(sample,home.clock,review_pending=pending)
            if direction and director.decision_current(sample,home.clock,direction,review_pending=pending):
                home.runtime.clear(camera);moving_until[camera]=home.clock+1;director.sent(home.clock,direction)
                view_offset[camera] += .05 if direction == 'right' else -.05
        await settle()
    return directors


@pytest.mark.asyncio
async def test_eight_patrolling_feeds_each_get_a_complete_corroboration(tmp_path,monkeypatch):
    home=Home(tmp_path,monkeypatch)
    try:
        directors=await patrol_fleet(home)
        events=home.store.events()
        assert {e['camera']for e in events}==set(directors),(home.engine.calls,{e['camera']for e in events})
        assert all(len(e['frames'])==8 for e in events)and home.engine.maximum==1
        assert len(home.runtime.visual.served)<=8
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_normal_analysis_stays_quiet_and_cannot_disable_patrol_permanently(tmp_path,monkeypatch):
    from homeplane.embodied.temporal_model import VisualResult
    home=Home(tmp_path,monkeypatch);original=home.engine.analyze
    async def normal(frames):
        await original(frames)
        return VisualResult(event='none',observation='合成普通画面')
    home.engine.analyze=normal
    try:
        patrol=Patrol(home,'primary');await patrol.run()
        assert not home.store.events() and len(patrol.moves)>=2
        assert any(phase=='review_held'for _,phase in patrol.phases)
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_scheduler_ledger_is_bounded_and_retains_no_frames_after_view_retirement():
    from homeplane.embodied.temporal_review import TemporalReviewer
    from homeplane.embodied.temporal_model import VisualResult
    now=[0.];engine=SimpleNamespace(analyze=AsyncMock(return_value=VisualResult(event='none',observation='合成画面')),close=AsyncMock())
    reviewer=TemporalReviewer(policy=lambda:SimpleNamespace(enabled=True,visual_review=True,revision=1),
                              deliver=AsyncMock(),engine=engine,clock=lambda:now[0])
    try:
        for index in range(24):
            camera=f'cam_{index:010x}'
            for second in range(4):
                now[0]=index*10+second;reviewer.offer(camera,'synthetic',1,now[0],b'SYNTHETIC',stable=True)
            await settle();reviewer.clear(camera);await settle()
            assert len(reviewer.served)<=8 and not reviewer.windows
        await reviewer.invalidate();assert reviewer.served=={}
    finally:
        await reviewer.close()
