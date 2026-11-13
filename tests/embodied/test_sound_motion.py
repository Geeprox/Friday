"""Qualified synthetic sound through original camera loops and the event store."""

import asyncio
import json
import math
import secrets
import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from homeplane.embodied import safety, safety_api
from homeplane.embodied.camera_audio import AudioIngress, PCMFramer
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.camera_intent import camera_intent_readback, finish_camera_survey
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.local_motor import SOUND_MOTION_DENIALS
from homeplane.embodied.party_camera import PartyCamera
from homeplane.embodied.safety import SafetyAdmissionChanged
from homeplane.embodied.sound_review import SoundReviewer
from tests.embodied.test_camera_director import patrol_view, person, sample
from tests.embodied.test_camera_follow_appearance import picture, subject
from tests.embodied.test_motor_recovery import capture_motor_command, inert_motor, primary_scope
from tests.embodied.test_review_motion import recorded_sound_only, source_function
from tests.embodied.test_sound_followup import Home, SlowEngine
from tests.embodied.test_sound_review import CAMERA, CONTEXT, Engine, Journey, settle


class EpisodeEngine(SlowEngine):
    def __init__(self, clock, *, infant=False):
        super().__init__(clock)
        self.qualified = []
        self.infant = infant

    async def run(self, pcm):
        result = await super().run(pcm)
        if all(pcm[i * 6400] == 1 for i in range(10)):
            self.qualified.append((pcm[1]+256*pcm[2], pcm[-6399]+256*pcm[-6398]))
            return dict(result, infant_cry=True) if self.infant else result
        return None


class SoundPatrol:
    def __init__(self, tmp_path, monkeypatch, owner, *, episode=(14.2, 18.2), infant=False,
                 people=None, motor_shifts_view=True, enable_search=True, occupied_elsewhere=False,
                 follow_shifts_view=True, run_until=40, mode="auto"):
        self.j = Journey(tmp_path, monkeypatch)
        if enable_search:
            self.j.store.configure_sound_search(self.j.store.policy().revision, True)
        self.engine = EpisodeEngine(lambda: self.j.clock, infant=infant)
        self.j.runtime.sound.engine = self.engine
        self.owner, self.episode, self.mode = owner, episode, mode
        self.index = 0
        self.moving_until = 0
        self.view_offset = 0.
        self.view_height = 0.
        self.moves, self.phases = [], []
        self.sound_moves, self.follow_moves = [], []
        self.sound_sent_at = []
        self.occupied_with_saved_sound = False
        self.run_until = run_until
        self.people = people or (lambda _: [])
        self.motor_shifts_view = motor_shifts_view
        self.follow_shifts_view = follow_shifts_view
        self.occupied_elsewhere = occupied_elsewhere
        self.state = {}
        self.exit_control = {}
        self.session = "SYNTHETIC-sound-motion"
        self.process = SimpleNamespace(returncode=None)
        self.director = CameraDirector()
        self.item = dict(privacy_closed=False, source_active=True, control_epoch=1,
                         mode=mode, mode_revision="intent")
        self.scope = dict(time=SimpleNamespace(monotonic=lambda: self.j.clock),
                          asyncio=SimpleNamespace(sleep=self.advance, CancelledError=asyncio.CancelledError),
                          CameraDirector=lambda: self.director, camera_is_open=AsyncMock(return_value=True))

    async def advance(self, _=0):
        # 等已接纳的画面写入 SQLite，再推进合成时钟。
        pending_views = tuple(self.j.runtime.sound_view_tasks.values())
        if pending_views:
            await asyncio.gather(*pending_views)
        self.index += 1
        self.j.clock = self.index * .2
        if self.j.clock > self.run_until:
            raise asyncio.CancelledError
        task = self.j.runtime.sound.task
        if self.engine.release_due() and task:
            await task
            await settle()
        stable = self.j.clock >= self.moving_until
        people = self.people(self) if stable else []
        jpeg = f"SYNTHETIC-FRAME-{self.index}".encode()
        frame = dict(type="frame", received_at=1000+self.j.clock, capture_session="a"*32,
                     camera_source_context=CONTEXT)
        assert self.j.gate.observe_frame(frame, wall=1000+self.j.clock, mono=self.j.clock)
        # 固定合成时钟下的数据库先后顺序，保留真实的策略读取与留档。
        async with self.j.runtime.io.lock:
            await self.j.runtime.sync_policy()
        self.j.runtime.capture_frame(CAMERA["id"], self.j.gate.session, CONTEXT, self.j.clock)
        self.j.runtime.offer(CAMERA["id"], self.session, people, self.j.clock, self.j.clock,
            stable=stable, jpeg=jpeg, source_context=CONTEXT, source_name="合成镜头",
            capture_session=self.j.gate.session)
        if task := self.j.runtime.observation_task:
            await task
        marker = int(self.episode[0] <= self.j.clock < self.episode[1])
        samples = bytes([marker, self.index % 256, self.index // 256]) + bytes(6397)
        packet = self.j.framer.push(samples, received=1000+self.j.clock, decoded=1000+self.j.clock,
                                  wall=1000+self.j.clock, mono=self.j.clock)[0]
        self.j.runtime.observe_audio(CAMERA["id"], self.j.gate.accept(
            packet, wall=1000+self.j.clock, mono=self.j.clock))
        if people and self.j.runtime.sound_search_cue(CAMERA["id"], self.session) is not None:
            self.occupied_with_saved_sound = True
        self.state.update(status="live", _local_received=self.j.clock, session_id=self.session,
                          tracking_current=True, scene_visible=stable, people=people,
                          _follow_jpeg=jpeg,
                          _patrol_view=patrol_view(self.j.clock, x=self.view_offset,
                                                  y=self.view_height) if stable else None)
        self.scope["last_frame"] = self.j.clock
        self.scope["last_jpeg"] = jpeg
        self.phases.append((self.j.clock, (self.state if self.owner == "primary" else self.item)
                            .get("camera_control", {}).get("phase")))
        await settle()

    async def motor(self, direction, *_, **kwargs):
        if callback := kwargs.get("decision_current"):
            assert callback(), "original final-dispatch guard must still admit this move"
            recorded_sound = self.director.recorded_sound_for_motion(direction)
            assert kwargs.get('recorded_sound') is recorded_sound
            if recorded_sound is not None:
                assert await self.j.runtime.check_sound_motion(CAMERA['id'], self.session, recorded_sound)
        elif self.owner == "secondary":
            assert self.director.decision_current(self.state, self.j.clock, direction,
                occupied_elsewhere=self.occupied_elsewhere,
                review_pending=self.j.runtime.review_pending(CAMERA["id"], self.session),
                sound_cue=self.j.runtime.sound_search_cue(CAMERA["id"], self.session,
                                                         self.director.sound_search_cue))
        if self.director.phase == 'sound_search':
            events = await self.j.runtime.io.call(self.j.store.events)
            assert len(events) == 1, '先确认原留档成功，再准许转向'
            self.sound_moves.append(direction)
            self.sound_sent_at.append(self.j.clock)
        if self.director.phase == 'following':
            self.follow_moves.append((direction, self.director.target))
        self.moves.append((self.j.clock, direction))
        self.moving_until = self.j.clock + 1
        if self.motor_shifts_view and (self.director.phase != 'following' or self.follow_shifts_view):
            self.view_offset += .05 * ((direction == 'right') - (direction == 'left'))
            self.view_height += .05 * ((direction == 'down') - (direction == 'up'))
        self.j.runtime.clear_view(CAMERA["id"])
        if self.owner == "secondary":
            self.director.sent(self.j.clock, direction)

    async def run(self):
        await self.advance()
        if self.owner == "primary":
            lease = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock(return_value=True),
                                    presets=[], active=None, event=lambda *_, **__: None)
            planner = SimpleNamespace(public=lambda _: {"mode": self.mode}, mode=self.mode, intent_revision="intent",
                                      intent_status="configured")
            other = {"cam_2222222222": {"people": 1}} if self.occupied_elsewhere else {}

            async def reap(_):
                self.exit_control = self.state.get("camera_control", {})

            self.scope.update(Transport=lambda: SimpleNamespace(close=AsyncMock()),
                CameraLease=lambda *a, **k: lease, SearchPlanner=lambda *a: planner,
                camera_intents=SimpleNamespace(load=lambda: SimpleNamespace(
                    mode=self.mode, revision="intent", status="configured")),
                os=SimpleNamespace(environ={}), SELECTED={"id": CAMERA["id"]}, state=self.state,
                games=MotionGames(), party_camera=PartyCamera(),
                previews=SimpleNamespace(sessions=other, status=lambda _: {"state": "live"}),
                home=SimpleNamespace(camera_focus=None), safety=self.j.runtime,
                camera_motion_cue=recorded_sound_only(self.j.runtime),
                motor_step=self.motor, camera_lease=None, camera_planner=None,
                reap=reap, capture=self.process, generation=1,
                camera_intent_readback=camera_intent_readback, finish_camera_survey=finish_camera_survey)
            loop = source_function("homeplane/embodied/worker.py", "camera_search", self.scope)
        else:
            self.scope.update(item=self.item, selected={"id": CAMERA["id"]}, director=self.director,
                process=self.process, recognizer=SimpleNamespace(session=self.session),
                self=SimpleNamespace(focus=lambda _: None, occupied_elsewhere=lambda _: self.occupied_elsewhere,
                                     review_pending=self.j.runtime.review_pending,
                                     fall_pending=self.j.runtime.fall_pending,
                                     sound_cue=recorded_sound_only(self.j.runtime),
                                     sound_view=self.j.runtime.offer_sound_search_frame,
                                     sound_motion=self.j.runtime.check_sound_motion,
                                     source_current=lambda _, process: process is self.process),
                current_sample=lambda: dict(self.state), privacy_closed=lambda: None,
                send_motion=self.motor)
            loop = source_function("homeplane/embodied/previews.py", "direct", self.scope)
        try:
            await loop()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError(f"original camera loop exited at {self.j.clock:.1f}s: {self.exit_control}")
        await self.j.runtime.drain()

    def assert_saved_views(self, event):
        views = sorted(event['sound_search_views'], key=lambda view: view['step'])
        assert [view['step'] for view in views] == list(range(1, len(self.sound_moves) + 1))
        for view, sent_at in zip(views, self.sound_sent_at, strict=True):
            frame = next(f for f in event['frames'] if f['ordinal'] == view['ordinal'])
            assert frame['at'] > event['captured_at']
            assert frame['at'] - 1000 >= sent_at + 2.35 - 1e-8
            index = round((frame['at'] - 1000) / .2)
            assert self.j.store.frame(event['id'], view['ordinal']) == f"SYNTHETIC-FRAME-{index}".encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["primary", "secondary"])
async def test_short_sound_near_a_patrol_deadline_keeps_both_original_loops_stable(
    tmp_path, monkeypatch, owner
):
    patrol = SoundPatrol(tmp_path, monkeypatch, owner, run_until=55)
    try:
        await patrol.run()
        events = patrol.j.store.events()
        assert len(events) == 1, (owner, patrol.moves, patrol.engine.windows)
        event = events[0]
        assert event["kind"] == "sound_review" and event["sound_kind"] == "crying"
        assert event["sound_evidence"]["corroborating_windows"] == 2
        assert event["sound_evidence"]["audio_seconds"] == 4
        assert event["frames"] and event["camera_source_context"] == CONTEXT
        assert event["person_id"] is None and not event["directional_audio"]
        assert patrol.engine.qualified == [(71, 80), (81, 90)], "two complete, disjoint synthetic windows"
        assert patrol.moves and any(phase == "review_held" for _, phase in patrol.phases)
        assert patrol.sound_moves == ['right', 'left', 'left']
        patrol.assert_saved_views(event)
        assert patrol.moves[-1][0] >= patrol.sound_sent_at[-1] + 15
        assert any(phase == "patrolling" and stamp > patrol.sound_sent_at[-1] + 15
                   for stamp, phase in patrol.phases)
        assert patrol.engine.maximum == 1
        patrol.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["primary", "secondary"])
async def test_saved_sound_interrupts_survey_and_retains_follow_views(tmp_path, monkeypatch, owner):
    def people(patrol):
        if patrol.sound_moves:
            return [sound_person(42, .43 if patrol.follow_moves else .76, "SYNTHETIC-new")]
        return []

    patrol = SoundPatrol(tmp_path, monkeypatch, owner, mode="survey", episode=(18.2, 22.2),
                         people=people, occupied_elsewhere=True, run_until=78)
    try:
        await patrol.run()
        event, = patrol.j.store.events()
        assert event["kind"] == "sound_review"
        assert patrol.sound_moves == ["right"], (patrol.moves, patrol.engine.qualified)
        route_moves = [at for at, _ in patrol.moves if at < patrol.sound_sent_at[0]]
        assert all(at < event["captured_at"] - 1000 for at in route_moves)
        assert patrol.follow_moves == [("right", 42)]
        assert len(patrol.moves) == len(route_moves) + 2
        patrol.assert_saved_views(event)
        assert event["sound_follow_view"] and event["track"] is None and not event["directional_audio"]
        report = patrol.exit_control if owner == "primary" else patrol.item["camera_control"]
        assert report["survey_result"] == "interrupted" and report["survey_interruption"] == "sound"
        assert report["survey_confirmed_moves"] == report["survey_attempts"] == len(route_moves)
        patrol.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await patrol.j.runtime.close()


def test_unsent_survey_step_yields_to_sound_and_retains_confirmed_progress():
    director = CameraDirector()
    director.apply_mode("survey", "SYNTHETIC-survey")

    def frame(at, *, x=0, people=()):
        return dict(sample(at, people=people, x=x), tracking_current=True)

    assert director.choose(frame(.1), .1) is None
    assert director.choose(frame(.3), .3) is None
    assert director.choose(frame(15.4), 15.4) == "right"
    director.sent(15.4)
    for at in (17.6, 18.2, 18.6):
        assert director.choose(frame(at, x=.05), at) is None
    assert director.survey_confirmed_moves == 1

    pending = frame(27.5, x=.05)
    assert director.choose(pending, 27.5) == "right"
    cue = SimpleNamespace(captured=22.0)
    current = frame(27.7, x=.05)
    assert not director.decision_current(current, 27.7, "right", sound_cue=cue)
    assert director.choose(current, 27.7, sound_cue=cue) == "right"
    assert director.phase == "sound_search"
    occupied = frame(27.8, x=.05, people=(sound_person(42, .5, "SYNTHETIC-visible"),))
    assert not director.decision_current(occupied, 27.8, "right", sound_cue=cue)
    assert not director.decision_current(current, 27.7, "right", sound_cue=None)
    assert director.choose(frame(27.9, x=.05), 27.9) is None
    report = director.public()
    assert report["survey_confirmed_moves"] == 1
    assert report["survey_result"] == "interrupted" and report["survey_interruption"] == "sound"
    assert director.choose(frame(30.0, x=.05), 30.0, sound_cue=cue) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
@pytest.mark.parametrize('follow_shifts_view', [True, False], ids=['new-view', 'same-view'])
async def test_saved_sound_search_hands_a_new_visual_track_to_existing_follow(
    tmp_path, monkeypatch, owner, follow_shifts_view
):
    def people(patrol):
        if patrol.sound_moves:
            return [sound_person(42, .43 if patrol.follow_moves else .76, 'SYNTHETIC-new')]
        if 12 <= patrol.j.clock < 22:
            return [sound_person(7, .43, 'SYNTHETIC-previous')]
        return []

    patrol = SoundPatrol(tmp_path, monkeypatch, owner, people=people, occupied_elsewhere=True,
                         follow_shifts_view=follow_shifts_view)
    try:
        await patrol.run()
        assert patrol.occupied_with_saved_sound
        assert patrol.sound_moves == ['right'], ' '.join(
            f'{stamp:.1f}/{phase}' for stamp, phase in patrol.phases[105:132])
        assert patrol.sound_sent_at[0] >= 22
        assert patrol.follow_moves == [('right', 42)]
        assert any(phase == 'target_ambiguous' for _, phase in patrol.phases)
        event, = patrol.j.store.events()
        assert not event['directional_audio'] and event['track'] is None
        patrol.assert_saved_views(event)
        if follow_shifts_view:
            follow = event['sound_follow_view']
            frame = next(row for row in event['frames'] if row['ordinal'] == follow['ordinal'])
            assert follow['ordinal'] not in {row['ordinal'] for row in event['sound_search_views']}
            assert frame['at'] - 1000 >= patrol.moves[1][0] + 2.35 - 1e-8
            index = round((frame['at'] - 1000) / .2)
            assert patrol.j.store.frame(event['id'], frame['ordinal']) == f'SYNTHETIC-FRAME-{index}'.encode()
        else:
            assert 'sound_follow_view' not in event
            assert any(phase == 'target_reacquiring' for _, phase in patrol.phases)
        assert len(patrol.moves) == 2, '普通自动巡视仍受其他镜头在场状态限制'
    finally:
        await patrol.j.runtime.close()


def sound_person(track, x, identity):
    result = person(track, x=x, identity=identity)
    result['box']['confidence'] = .9
    return result


def test_recent_saved_sound_can_start_after_the_original_view_clears():
    director = CameraDirector()
    director.apply_mode('auto', 'intent')
    director.choose(sample(0), 0)
    director.choose(sample(.5), .5)
    cue = SimpleNamespace(captured=.9)
    occupied = sample(1, people=[sound_person(7, .43, 'SYNTHETIC-existing')])
    assert director.choose(occupied, 1, sound_cue=cue) is None
    assert director.sound_search_cue is None

    clear = sample(1.3)
    assert director.choose(clear, 1.3, sound_cue=cue) == 'right'
    assert director.decision_current(clear, 1.3, 'right', sound_cue=cue)


def test_sound_follow_confirms_continuity_and_rechecks_cue_before_dispatch():
    director = CameraDirector()
    director.apply_mode('auto', 'intent')
    cue = SimpleNamespace(captured=.9)
    old = sound_person(7, .43, 'SYNTHETIC-previous')
    target = sound_person(42, .76, 'SYNTHETIC-new')
    replacement = sound_person(43, .81, 'SYNTHETIC-other')

    def frame(at, *people, x=.05):
        return dict(sample(at, people=people, x=x), tracking_current=True)

    director.choose(frame(0, x=0), 0)
    director.choose(frame(.5, old, x=0), .5)
    assert director.choose(frame(1, x=0), 1, sound_cue=cue) == 'right'
    director.sent(1)
    assert director.choose(frame(3, target), 3, sound_cue=cue) is None
    first = frame(3.4, target)
    assert director.choose(first, 3.4, sound_cue=cue) is None
    assert director.choose(first, 3.5, sound_cue=cue) is None
    assert director.choose(frame(3.6, target, replacement), 3.6, sound_cue=cue) is None
    assert director.choose(frame(3.8, target), 3.8, sound_cue=cue) is None
    current = frame(4, target)
    assert director.choose(current, 4, sound_cue=cue) == 'right'
    assert director.decision_current(current, 4, 'right', sound_cue=cue)
    assert not director.decision_current(current, 4, 'right')
    bystander = sound_person(43, .20, 'SYNTHETIC-other')
    ambiguous = dict(target, tracking=dict(overlapping=True))
    assert director.decision_current(frame(4.1, target, bystander), 4.1, 'right', sound_cue=cue)
    assert not director.decision_current(frame(4.1, target, replacement), 4.1, 'right', sound_cue=cue)
    assert not director.decision_current(frame(4.1, ambiguous, replacement), 4.1, 'right', sound_cue=cue)
    duplicate = sound_person(43, .20, 'SYNTHETIC-new')
    assert not director.decision_current(frame(4.1, target, duplicate), 4.1, 'right', sound_cue=cue)
    assert not director.decision_current(frame(4.1, dict(target, identity={}), duplicate), 4.1, 'right',
                                         sound_cue=cue)
    assert not director.decision_current(frame(4.1, target, x=.10), 4.1, 'right', sound_cue=cue)
    assert not director.decision_current(current, 4, 'right', sound_cue=cue, fall_pending=True)
    director.sent(4)
    assert director.choose(frame(6.1, replacement, x=.1), 6.1, sound_cue=cue) is None
    relocated = sound_person(48, .76, 'SYNTHETIC-new')
    assert director.choose(frame(6.3, relocated, x=0), 6.3, sound_cue=cue) is None
    assert director.take_sound_view() is None
    changed = frame(6.5, relocated, x=.1)
    assert director.choose(changed, 6.5, sound_cue=cue) is None
    repeated = dict(frame(6.9, relocated, x=.1), _patrol_view=changed['_patrol_view'])
    assert director.choose(repeated, 6.9, sound_cue=cue) is None
    assert director.take_sound_view() is None
    assert director.choose(frame(7.1, relocated, x=.1), 7.1, sound_cue=cue) == 'right'
    assert director.take_sound_view() == (cue, 'follow', 7.1)
    assert director.target == 48
    director.failed(7.1)
    assert director.sound_search_cue is None
    assert director.choose(frame(9.3, replacement, x=.15), 9.3) is None
    assert director.choose(frame(9.7, replacement, x=.15), 9.7) is None
    assert director.target_identity == 'SYNTHETIC-new'


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
async def test_original_loops_keep_the_confirmed_sound_target_when_someone_walks_in(tmp_path, monkeypatch, owner):
    def people(patrol):
        if not patrol.sound_moves:
            return []
        elapsed = patrol.j.clock - patrol.sound_sent_at[0]
        if elapsed < 3.4:
            return [sound_person(42, .43, 'SYNTHETIC-original')]
        bystander = sound_person(43, .16, 'SYNTHETIC-bystander')
        if not patrol.follow_moves:
            return [bystander, sound_person(42, .76, 'SYNTHETIC-original')]
        return [bystander, sound_person(48, .43, 'SYNTHETIC-original')]

    patrol = SoundPatrol(tmp_path, monkeypatch, owner, people=people, occupied_elsewhere=True)
    try:
        await patrol.run()
        assert patrol.sound_moves == ['right']
        assert patrol.follow_moves == [('right', 42)]
        event, = patrol.j.store.events()
        patrol.assert_saved_views(event)
        frame = next(row for row in event['frames'] if row['ordinal'] == event['sound_follow_view']['ordinal'])
        assert frame['at'] - 1000 >= patrol.moves[1][0] + 2.35 - 1e-8
        assert event['track'] is None and event['directional_audio'] is False
        index = round((frame['at'] - 1000) / .2)
        assert patrol.j.store.frame(event['id'], frame['ordinal']) == f'SYNTHETIC-FRAME-{index}'.encode()
    finally:
        await patrol.j.runtime.close()


def test_sound_follow_reacquires_an_unregistered_person_by_current_view():
    director = CameraDirector()
    director.apply_mode('auto', 'intent')
    cue = SimpleNamespace(captured=.9)
    target, other, returned = subject(42, .68), subject(43, .47), subject(48, .68)

    def observe(at, x, *rows, sound_cue=cue):
        frame = sample(at, people=[row for row, _ in rows], x=x)
        frame.update(tracking_current=True, _follow_jpeg=picture(rows))
        return director.choose(frame, at, sound_cue=sound_cue)

    observe(0, 0, sound_cue=None)
    observe(.5, 0, sound_cue=None)
    assert observe(1, 0) == 'right'
    director.sent(1)
    assert observe(3, .05, (target, 7)) is None
    assert observe(3.4, .05, (target, 7)) is None
    assert observe(4.3, .05, (target, 7)) is None
    assert observe(5, .05, (target, 7)) == 'right'
    assert director.pending_decision['appearance'] is not None
    director.sent(5)
    assert observe(7.2, .1, (other, 8)) is None
    assert observe(7.6, .1, (other, 8)) is None
    assert observe(8.2, .1, (returned, 7), (other, 7)) is None
    assert observe(8.6, .1, (returned, 7), (other, 8)) is None
    assert director.take_sound_view() is None
    assert observe(9.0, .1, (returned, 7), (other, 8)) == 'right'
    assert director.take_sound_view() == (cue, 'follow', 9.0)
    assert director.target == 48 and director.target_identity is None


@pytest.mark.parametrize('visible', [False, True], ids=['obscured', 'unregistered'])
def test_sound_follow_waits_for_the_same_view_without_adopting_a_bystander(visible):
    director = CameraDirector()
    director.apply_mode('auto', 'intent')
    cue = SimpleNamespace(captured=.9)
    target = sound_person(42, .43, 'SYNTHETIC-original')
    bystander = sound_person(43, .76, 'SYNTHETIC-other')

    def observe(at, people=(), *, sound_cue=cue, visible=True, registered=True, x=.05, **view):
        frame = dict(sample(at, people=people, visible=visible, x=x, **view), tracking_current=True)
        if not registered:
            frame['_patrol_view'] = None
        return director.choose(frame, at, sound_cue=sound_cue)

    assert observe(0, sound_cue=None, x=0) is None
    assert observe(.5, sound_cue=None, x=0) is None
    assert observe(1, x=0) == 'right'
    director.sent(1)
    assert observe(3.0, [target]) is None
    assert observe(3.4, [target]) is None
    assert observe(3.8, [target]) is None
    assert director.sound_following
    for stamp in (4.1, 4.7, 5.3):
        assert observe(stamp, visible=visible, registered=False, sound_cue=None) is None
        assert director.public()['target_locked'] is False
        assert director.take_sound_view() is None
    assert director.sound_search_cue is cue
    assert observe(5.6, [bystander]) is None
    assert observe(6.0, [bystander]) is None
    returned = sound_person(48, .76, 'SYNTHETIC-original')
    assert observe(6.2, [bystander, returned]) is None
    assert director.public()['target_locked'] is False
    separate = sound_person(43, .16, 'SYNTHETIC-other')
    assert observe(6.4, [separate, returned]) is None
    assert director.take_sound_view() is None
    assert observe(6.8, [separate, returned]) == 'right'
    assert director.target == 48
    assert director.take_sound_view() is None
    assert director.public()['reason'] == '异响后跟随人物'


@pytest.mark.parametrize('changes', [dict(session='new-capture'), dict(basis='new-background'), {}],
                         ids=['source-changed', 'basis-changed', 'cue-revoked'])
def test_sound_follow_ends_when_the_restored_view_loses_its_original_authority(changes):
    director = CameraDirector()
    director.apply_mode('survey', 'intent')
    cue = SimpleNamespace(captured=.9)
    target = sound_person(42, .43, 'SYNTHETIC-original')

    def frame(at, *, x=.05, people=(target,), **view):
        return dict(sample(at, x=x, people=people, **view), tracking_current=True)

    director.choose(frame(0, x=0, people=()), 0)
    director.choose(frame(.5, x=0, people=()), .5)
    assert director.choose(frame(1, x=0, people=()), 1, sound_cue=cue) == 'right'
    director.sent(1)
    for stamp in (3.0, 3.4, 3.8):
        assert director.choose(frame(stamp), stamp, sound_cue=cue) is None
    assert director.sound_following
    assert director.choose(dict(frame(4.1), _patrol_view=None), 4.1) is None
    assert director.sound_search_cue is cue
    visible = frame(4.6, people=(sound_person(48, .76, 'SYNTHETIC-original'),), **changes)
    assert director.choose(visible, 4.6, sound_cue=cue if changes else None) is None
    assert director.sound_search_cue is None
    assert director.take_sound_view() is None


@pytest.mark.asyncio
async def test_sound_search_stops_when_the_camera_ack_leaves_the_view_unchanged(tmp_path, monkeypatch):
    patrol = SoundPatrol(tmp_path, monkeypatch, 'primary', motor_shifts_view=False)
    try:
        await patrol.run()
        assert patrol.sound_moves == ['right'] and not patrol.follow_moves
        assert patrol.director.sound_search_cue is None
        assert 'sound_search_views' not in patrol.j.store.events()[0]
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
async def test_sound_review_only_still_records_without_starting_a_sound_search(tmp_path, monkeypatch):
    patrol = SoundPatrol(tmp_path, monkeypatch, 'primary', enable_search=False)
    try:
        await patrol.run()
        assert len(patrol.j.store.events()) == 1
        assert patrol.moves and not patrol.sound_moves
        assert 'sound_search_views' not in patrol.j.store.events()[0]
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
async def test_sound_search_requires_a_saved_receipt_and_rechecks_the_active_source(tmp_path, monkeypatch):
    patrol = SoundPatrol(tmp_path, monkeypatch, 'primary', episode=(0, math.inf))
    runtime, camera, session = patrol.j.runtime, CAMERA['id'], patrol.session
    wake = runtime.recording.wake
    monkeypatch.setattr(runtime.recording, 'wake', lambda: None)
    try:
        for _ in range(30):
            await patrol.advance()
        assert len(runtime.recording.jobs) == 1 and not patrol.j.store.events()
        assert runtime.sound_search_cue(camera, session) is None
        window = runtime.sound.windows[camera]
        runtime.clear_view(camera)
        patrol.moving_until = patrol.j.clock + .35
        await patrol.advance()
        assert len(runtime.recording.jobs) == 1 and runtime.sound.windows[camera] is window
        assert runtime.sound_search_cue(camera, session) is None
        await patrol.advance()
        monkeypatch.setattr(runtime.recording, 'wake', wake)
        wake()
        await runtime.drain()
        cue = runtime.sound_search_cue(camera, session)
        assert cue is not None and len(patrol.j.store.events()) == 1
        assert runtime.sound_search_cue('cam_2222222222', session, cue) is None
        assert runtime.sound_search_cue(camera, 'next-session', cue) is None
        runtime.observe_audio(camera, patrol.j.gate.reject('audio_stale'))
        assert not runtime.sound.windows
        assert runtime.sound_search_cue(camera, session) is cue
        runtime.clear_view(camera)
        assert runtime.sound_search_cue(camera, session, cue) is None
        patrol.moving_until = patrol.j.clock + .35
        await patrol.advance()
        assert runtime.sound_search_cue(camera, session) is None
        await patrol.advance()
        assert runtime.sound_search_cue(camera, session) is cue
        assert runtime.sound_search_cue(camera, session, cue) is cue
        pause = tmp_path / 'voice-input-paused'
        pause.touch()
        assert runtime.sound_search_cue(camera, session, cue) is None
        pause.unlink()
        runtime.clear(camera)
        assert runtime.sound_search_cue(camera, session, cue) is None
        await patrol.advance()
        assert runtime.sound_search_cue(camera, session) is None
        assert runtime.sound_search_cue(camera, session, cue) is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pan', 'source', 'process', 'policy', 'audio', 'false_alarm'])
@pytest.mark.parametrize('step', [1, 'follow'])
async def test_sound_search_frame_rechecks_custody_and_policy_at_storage(tmp_path, monkeypatch, change, step):
    patrol = SoundPatrol(tmp_path, monkeypatch, 'primary', episode=(0, math.inf))
    runtime, store, camera = patrol.j.runtime, patrol.j.store, CAMERA['id']
    entered, release = asyncio.Event(), asyncio.Event()
    original_call = runtime.io.call

    async def delayed(operation, *args, **kwargs):
        if operation == store.append_sound_search_frame:
            entered.set()
            await release.wait()
        return await original_call(operation, *args, **kwargs)

    live = True
    try:
        for _ in range(30):
            await patrol.advance()
        await runtime.drain()
        cue = runtime.sound_search_cue(camera, patrol.session)
        event, = store.events()
        assert cue.receipt.event_id == event['id']
        monkeypatch.setattr(runtime.io, 'call', delayed)
        proof = (cue, step, patrol.j.clock)
        jpeg = b'SYNTHETIC-PAN-RESULT'
        assert runtime.offer_sound_search_frame(camera, proof, jpeg, source_current=lambda: live)
        assert not runtime.offer_sound_search_frame(camera, proof, jpeg, source_current=lambda: live)
        await asyncio.wait_for(entered.wait(), 1)
        if change == 'pan':
            # 云台继续查看时重置画面识别。已经核实的上一张仍属同一采集进程。
            runtime.clear_view(camera)
        elif change == 'source':
            runtime.clear(camera)
        elif change == 'process':
            live = False
        elif change == 'policy':
            store.configure_sound_search(cue.revision, False)
        elif change == 'audio':
            (tmp_path/'voice-input-paused').touch()
        else:
            store.update(event['id'], review='false_alarm')
        release.set()
        await runtime.drain()
        result, = store.events()
        assert result['id'] == event['id'] and result['captured_at'] == event['captured_at']
        field = 'sound_follow_view' if step == 'follow' else 'sound_search_views'
        if change == 'pan':
            view = result[field] if step == 'follow' else result[field][0]
            assert store.frame(event['id'], view['ordinal']) == jpeg
            store.append_sound_search_frame(event['id'], (camera, cue.session, cue.context), step,
                1000+patrol.j.clock, jpeg, policy_revision=cue.revision, guard=lambda: True)
            checks = iter((True, False))
            with pytest.raises(SafetyAdmissionChanged, match='safety_source_changed'):
                store.append_sound_search_frame(event['id'], (camera, cue.session, cue.context), 2,
                    1000+patrol.j.clock+1, jpeg, policy_revision=cue.revision, guard=lambda: next(checks))
            with pytest.raises(SafetyAdmissionChanged, match='safety_event_changed'):
                store.append_sound_search_frame(event['id'], ('cam_2222222222', cue.session, cue.context), 2,
                    1000+patrol.j.clock+1, jpeg, policy_revision=cue.revision, guard=lambda: True)
            assert store.events() == [result]
        else:
            assert field not in result and result['frames'] == event['frames']
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('restored', [False, True])
async def test_sound_archive_stays_closed_after_a_false_alarm_even_if_review_changes(tmp_path, monkeypatch, restored):
    patrol = SoundPatrol(tmp_path, monkeypatch, 'primary', episode=(0, math.inf))
    runtime, store, camera = patrol.j.runtime, patrol.j.store, CAMERA['id']
    try:
        for _ in range(30):
            await patrol.advance()
        await runtime.drain()
        cue = runtime.sound_search_cue(camera, patrol.session)
        event, = store.events()
        assert cue.receipt.event_id == event['id']

        store.update(event['id'], review='false_alarm')
        if restored:
            store.update(event['id'], review='acknowledged')
        with pytest.raises(SafetyAdmissionChanged, match='safety_event_changed'):
            store.append_sound_search_frame(event['id'], (camera, cue.session, cue.context), 'follow',
                cue.captured_at + 1, b'SYNTHETIC-FOLLOW', policy_revision=cue.revision, guard=lambda: True)
        assert store.events()[0]['frames'] == event['frames']
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
@pytest.mark.parametrize('change', ['before_pipe', 'before_native', 'follow_before_native',
                                   'other_event', 'acknowledged', 'busy', 'source_changed', 'capture_changed'])
async def test_review_reaches_both_camera_dispatch_boundaries(tmp_path, monkeypatch, owner, change):
    patrol = SoundPatrol(tmp_path, monkeypatch, owner, episode=(0, math.inf))
    runtime, store = patrol.j.runtime, patrol.j.store
    camera = CAMERA['id']
    store.path.rename(tmp_path/'safety.sqlite')
    store.path = tmp_path/'safety.sqlite'
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router, app = APIRouter(), FastAPI()
    console = safety_api.install(router, SimpleNamespace(process=None))
    app.include_router(router)
    try:
        for _ in range(30):
            await patrol.advance()
        await runtime.drain()
        event, = store.events()
        other = store.create(dict(kind='sound_review', camera=camera, session=event['session'],
                                  camera_source_context=CONTEXT, captured_at=event['captured_at']))
        cue = runtime.sound_search_cue(camera, patrol.session)
        assert cue.receipt.event_id == event['id']
        director = patrol.director
        director.apply_mode('auto', 'intent')
        director.choose(dict(patrol.state, _local_received=1), 1)
        director.choose(dict(patrol.state, _local_received=1.5), 1.5)
        current = dict(patrol.state)
        direction = director.choose(current, patrol.j.clock, sound_cue=cue)
        if change == 'follow_before_native':
            director.sent(patrol.j.clock)
            original = sound_person(42, .76, 'SYNTHETIC-original')
            for offset in (2, 2.4, 2.8):
                stamp = patrol.j.clock + offset
                current = dict(sample(stamp, people=[original], x=.05, session=patrol.session),
                               tracking_current=True)
                direction = director.choose(current, stamp, sound_cue=cue)
            patrol.j.clock = current['_local_received']
            runtime.capture_sources[camera].seen = patrol.j.clock
            runtime.last_seen[camera] = patrol.j.clock
            runtime.policy_read_at = patrol.j.clock
            assert director.phase == 'following'
        assert direction == 'right' and director.recorded_sound_for_motion(direction) is cue
        read_cue = recorded_sound_only(runtime)
        motor, native, keepalive = inert_motor(monkeypatch, [patrol.j.clock])
        commands, results = [], []

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            async def review(event_id, value):
                response = await client.post(f"/api/embodied/safety/events/{event_id}/review", json={'review': value})
                assert response.status_code == 200 and response.json()['review'] == value

            async def drain():
                if change in {'before_native', 'follow_before_native'}:
                    await review(event['id'], 'false_alarm')
                    # 改回查看状态，已经撤销的转向仍不能复活。
                    await review(event['id'], 'acknowledged')
                selected = CAMERA if change != 'source_changed' else {
                    **CAMERA, 'device': {'did': 'synthetic-replacement'},
                }
                capture_session = cue.session if change != 'capture_changed' else 'b'*32
                with store._sound_motion_lock(camera) if change == 'busy' else nullcontext():
                    result = await capture_motor_command(commands[-1], motor, selected=selected,
                                                         capture_session=capture_session, private=tmp_path)
                results.append(result)
                request_id = result['request_id']
                waiters[request_id].set_result(result)

            def write(raw):
                commands.append(json.loads(raw))

            checked = False

            async def camera_open(_):
                nonlocal checked
                if not checked and change == 'before_pipe':
                    await review(event['id'], 'false_alarm')
                checked = True
                return True

            if change == 'other_event':
                await review(other['id'], 'false_alarm')
            if change == 'acknowledged':
                await review(event['id'], 'acknowledged')
            pipe = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=drain), returncode=None)
            if owner == 'primary':
                scope, _ = primary_scope()
                waiters = scope['motor_waiters']
                scope.update(safety=runtime, time=safety.time, SELECTED=CAMERA, state=current, capture=pipe,
                             camera_is_open=camera_open, last_frame=patrol.j.clock,
                             SOUND_MOTION_DENIALS=SOUND_MOTION_DENIALS)

                def decision_current():
                    return director.decision_current(current, patrol.j.clock, direction,
                        sound_cue=read_cue(camera, current, director.sound_search_cue))

                dispatch = scope['motor_step'](direction, patrol.j.clock, expected_intent='original',
                                                decision_current=decision_current, recorded_sound=cue)
            else:
                waiters = {}
                worker = SimpleNamespace(focus=lambda _: None, occupied_elsewhere=lambda _: False,
                    review_pending=runtime.review_pending, fall_pending=runtime.fall_pending, sound_cue=read_cue,
                    sound_motion=runtime.check_sound_motion, on_motion=runtime.clear_view,
                    runtime_log=SimpleNamespace(record=lambda *_args, **_kwargs: None))
                scope = dict(asyncio=asyncio, time=safety.time, secrets=secrets, json=json,
                    SOUND_MOTION_DENIALS=SOUND_MOTION_DENIALS, waiters=waiters, motion_lock=asyncio.Lock(),
                    room_lock=asyncio.Lock(), selected=CAMERA, recognizer=None, director=director,
                    camera_is_open=camera_open, current_sample=lambda: current, privacy_closed=lambda: None,
                    item=dict(privacy_closed=False, source_active=True, control_epoch=1), process=pipe, self=worker)
                dispatch = source_function('homeplane/embodied/previews.py', 'send_motion', scope)(
                    direction, patrol.j.clock, expected_epoch=1)
            if change in {'before_pipe', 'before_native', 'follow_before_native', 'busy', 'source_changed', 'capture_changed'}:
                with pytest.raises(RuntimeError, match='stale_input'):
                    await dispatch
                assert native == [] and director.failed_moves == 0
            else:
                assert (await dispatch)['native_code'] == 0 and len(native) == 1
            assert not waiters
            assert bool(commands) is (change != 'before_pipe')
            if commands:
                assert commands[0]['sound_event'] == cue.motor_evidence()
            if change in {'before_native', 'follow_before_native', 'source_changed', 'capture_changed'}:
                assert results[0]['error_code'] == 'sound_motion_revoked'
            if change == 'busy':
                assert results[0]['error_code'] == 'sound_motion_busy' and not cue.withdrawn.is_set()
            if change in {'before_pipe', 'before_native', 'follow_before_native'}:
                assert cue.withdrawn.is_set()
                assert runtime.sound.search_cue(camera, cue.session, cue) is None
                assert not runtime.offer_sound_search_frame(
                    camera, (cue, 1, patrol.j.clock), b'SYNTHETIC-FRAME', source_current=lambda: True)
                frame = dict(current, _local_received=patrol.j.clock + .2)
                assert director.choose(frame, patrol.j.clock + .2) is None
                assert director.sound_search_cue is None and not director.public()['target_locked']
    finally:
        await console.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_review_and_native_dispatch_cannot_cross_their_commit_boundary(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    busy = asyncio.Event()
    clock = [10]
    motor, native, keepalive = inert_motor(monkeypatch, clock)
    send = motor.send

    def reserved_send(*args):
        entered.set()
        assert release.wait(3)
        return send(*args)

    motor.send = reserved_send
    monkeypatch.setattr(safety_api, 'PRIVATE', tmp_path)
    router, app = APIRouter(), FastAPI()
    console = safety_api.install(router, SimpleNamespace(process=None))
    app.include_router(router)
    store = console.store
    policy = store.configure(safety.SafetyPolicy(enabled=True, sound_review=True))
    policy = store.configure_sound_search(policy.revision, True)
    source = dict(kind='sound_review', camera=CAMERA['id'], session='a'*32, camera_source_context=CONTEXT,
                  captured_at=1)
    event = store.create(source)
    other = store.create(dict(source, camera='cam_2222222222'))
    command = dict(action='motor_step', direction='right', request_id=1, expires_mono=11,
                   sound_event=dict(event=event['id'], session=event['session'], revision=policy.revision))
    original_call = console.io.call

    async def observe_busy(operation, *args, **kwargs):
        try:
            return await original_call(operation, *args, **kwargs)
        except safety.SoundMotionBusy:
            busy.set()
            raise

    monkeypatch.setattr(console.io, 'call', observe_busy)

    async def dispatch(request):
        return await capture_motor_command(request, motor, selected=CAMERA,
                                           capture_session=event['session'], private=tmp_path)

    task = asyncio.create_task(dispatch(command))
    pending = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            async def review_event(record):
                return await client.post(f"/api/embodied/safety/events/{record['id']}/review",
                                         json={'review': 'false_alarm'})

            pending = asyncio.create_task(review_event(event))
            await asyncio.wait_for(busy.wait(), 1)
            # 同镜头等已发的操作收尾，另一镜头仍可先提交。
            independent = await asyncio.wait_for(review_event(other), 1)
            assert independent.status_code == 200 and independent.json()['review'] == 'false_alarm'
            assert not pending.done() and store.event(event['id'])['review'] == 'unreviewed'
            release.set()
            assert (await task)['native_code'] == 0 and len(native) == 1
            review = await pending
            assert review.status_code == 200 and review.json()['review'] == 'false_alarm'
        clock[0] = 10.8
        result = await dispatch(dict(command, request_id=2))
        assert result['error_code'] == 'sound_motion_revoked' and len(native) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await console.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
async def test_original_camera_loop_retires_the_stationary_target_for_the_exact_false_alarm(tmp_path, monkeypatch, owner):
    def people(patrol):
        if not patrol.sound_moves:
            return []
        return [sound_person(42, .43, 'SYNTHETIC-original'),
                *([sound_person(43, .19, 'SYNTHETIC-bystander')] if revoked else [])]

    patrol = SoundPatrol(tmp_path, monkeypatch, owner, people=people, occupied_elsewhere=True)
    runtime, store, camera = patrol.j.runtime, patrol.j.store, CAMERA['id']
    revoked = []
    original = runtime.check_sound_motion

    async def revoke_while_stationary(camera_id, session, cue):
        if patrol.director.sound_following and not revoked:
            event, = store.events()
            assert patrol.director.target == 42 and patrol.director.pending_direction is None
            # 同源新提醒已接替最新回执，旧跟随仍须核对自己的事件。
            newer = store.create(dict(kind='sound_review', camera=camera, session=cue.session,
                                      camera_source_context=cue.context, captured_at=cue.captured_at+1))
            latest = replace(cue, captured=patrol.j.clock, captured_at=newer['captured_at'],
                             receipt=replace(cue.receipt, event_id=newer['id']), withdrawn=threading.Event())
            runtime.sound.search_cues[camera] = latest
            store.update(event['id'], review='false_alarm')
            revoked.append((cue, latest, event))
        return await original(camera_id, session, cue)

    monkeypatch.setattr(runtime, 'check_sound_motion', revoke_while_stationary)
    try:
        await patrol.run()
        (cue, latest, event), = revoked
        assert cue.withdrawn.is_set() and runtime.sound_search_cue(camera, patrol.session, cue) is None
        assert not latest.withdrawn.is_set()
        assert patrol.sound_moves == ['right'] and patrol.follow_moves == []
        assert patrol.director.sound_search_cue is None and not patrol.director.public()['target_locked']
        assert store.event(event['id'])['review'] == 'false_alarm'
        assert store.events()[1]['frames'] == event['frames']
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['primary', 'secondary'])
async def test_sustained_infant_cry_reaches_one_review_item_while_original_patrol_holds(tmp_path, monkeypatch, owner):
    patrol = SoundPatrol(tmp_path, monkeypatch, owner, episode=(14.2, 20.6), infant=True)
    try:
        await patrol.run()
        events = patrol.j.store.events()
        assert len(events) == 1, (owner, patrol.moves, patrol.engine.qualified)
        event = events[0]
        assert event['sound_evidence'] == dict(confidence=.96, corroborating_windows=3,
            audio_seconds=6, age_basis='local_sdk_receive', pattern='sustained_infant_cry')
        assert event['camera_source_context'] == CONTEXT and event['frames']
        assert event['status'] == 'pending_review' and event['person_id'] is None
        assert not event['directional_audio'] and not event['raw_audio_stored']
        assert len(patrol.engine.qualified) == 3 and patrol.engine.maximum == 1
        assert any(18.2 < at < 20.6 and phase == 'review_held' for at, phase in patrol.phases)
        assert not any(14.2 < at < 20.6 for at, _ in patrol.moves)
        assert any(at > 20.6 for at, _ in patrol.moves)
        patrol.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session", "source", "stale", "policy", "input_pause", "clear", "token"])
async def test_sound_stability_hint_requires_the_original_current_source_and_input(
    tmp_path, monkeypatch, change
):
    patrol = SoundPatrol(tmp_path, monkeypatch, "primary", episode=(0, math.inf))
    try:
        for _ in range(20):
            await patrol.advance()
        runtime, camera, session = patrol.j.runtime, CAMERA["id"], patrol.session
        assert runtime.review_pending(camera, session), "a current sound candidate should get a bounded hold"
        if change == "session":
            session = "replaced"
        elif change == "source":
            runtime.frames[camera]["source_context"] = None
        elif change == "stale":
            patrol.j.clock += 2
        elif change == "policy":
            patrol.j.store.configure(patrol.j.store.policy().model_copy(update={"sound_review": False}))
            await runtime.sync_policy()
        elif change == "input_pause":
            (tmp_path/"voice-input-paused").touch()
        elif change == "clear":
            runtime.clear(camera)
        elif change == "token":
            runtime.source_tokens[camera].set()
        assert not runtime.review_pending(camera, session)
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
async def test_a_recorded_sound_does_not_keep_requesting_camera_stability(tmp_path, monkeypatch):
    patrol = SoundPatrol(tmp_path, monkeypatch, "primary", episode=(0, math.inf))
    try:
        for _ in range(41):
            await patrol.advance()
        assert len(patrol.j.store.events()) == 1
        assert patrol.j.runtime.sound.task is not None
        assert not patrol.j.runtime.review_pending(CAMERA["id"], patrol.session)
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
async def test_normal_sound_cannot_disable_the_original_patrol_loop(tmp_path, monkeypatch):
    patrol = SoundPatrol(tmp_path, monkeypatch, "primary", episode=(0, 0))
    try:
        await patrol.run()
        assert not patrol.j.store.events()
        assert patrol.moves and patrol.moves[0][0] <= 32.2
        assert any(phase == "review_held" for _, phase in patrol.phases)
        patrol.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await patrol.j.runtime.close()


@pytest.mark.asyncio
async def test_retired_sound_views_keep_no_pcm_and_the_scheduling_memory_stays_bounded():
    clock = [0.]
    reviewer = SoundReviewer(policy=lambda: SimpleNamespace(enabled=True, sound_review=True, revision=1),
        current=lambda *_: True, deliver=AsyncMock(), engine=Engine(), clock=lambda: clock[0])
    try:
        for index in range(24):
            camera = dict(id=f"cam_{index:010x}", device=dict(did=f"SYNTHETIC-{index}"))
            context = camera_source_context(camera)
            capture = f"{index+1:032x}"
            framer, gate = PCMFramer(camera, capture), AudioIngress(camera, capture)
            for sample in range(10):
                clock[0] = index*3+sample*.2
                frame = dict(type="frame", received_at=1000+clock[0], capture_session=capture,
                             camera_source_context=context)
                assert gate.observe_frame(frame, wall=1000+clock[0], mono=clock[0])
                packet = framer.push(bytes(6400), received=1000+clock[0], decoded=1000+clock[0],
                    wall=1000+clock[0], mono=clock[0])[0]
                reviewer.offer(camera["id"], "SYNTHETIC-visual", context, 1,
                    gate.accept(packet, wall=1000+clock[0], mono=clock[0]))
                await settle()
            reviewer.clear(camera["id"])
            await settle()
            assert not reviewer.windows and len(reviewer.served) <= 8
        await reviewer.invalidate()
        assert not reviewer.served
    finally:
        await reviewer.close()


@pytest.mark.asyncio
async def test_eight_patrolling_sound_sources_each_keep_a_disjoint_corroboration(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    directors = {feed.camera["id"]: CameraDirector() for feed in home.feeds}
    moving_until = {camera: 0 for camera in directors}
    view_offset = {camera: 0. for camera in directors}
    moves = {camera: [] for camera in directors}
    try:
        for index in range(1, 301):
            home.j.clock = index*.2
            task = home.j.runtime.sound.task
            if home.engine.release_due() and task:
                await task
                await settle()
            for feed in home.feeds:
                camera = feed.camera["id"]
                stable = home.j.clock >= moving_until[camera]
                await home.j.runtime.observe(camera, "SYNTHETIC-visual", [], home.j.clock, home.j.clock,
                    stable=stable, jpeg=b"SYNTHETIC-FRAME", source_context=feed.context)
                frame = dict(type="frame", received_at=1000+home.j.clock, capture_session=feed.capture,
                             camera_source_context=feed.context)
                assert feed.gate.observe_frame(frame, wall=1000+home.j.clock, mono=home.j.clock)
                samples = bytes([feed.marker, index % 256, index // 256]) + bytes(6397)
                packet = feed.framer.push(samples, received=1000+home.j.clock, decoded=1000+home.j.clock,
                    wall=1000+home.j.clock, mono=home.j.clock)[0]
                home.j.runtime.observe_audio(camera, feed.gate.accept(
                    packet, wall=1000+home.j.clock, mono=home.j.clock))
                director = directors[camera]
                director.apply_mode("auto", "SYNTHETIC")
                sample = dict(status="live", tracking_current=True, scene_visible=stable, people=[],
                              session_id="SYNTHETIC-visual", _local_received=home.j.clock,
                              _patrol_view=patrol_view(home.j.clock, x=view_offset[camera]) if stable else None)
                pending = home.j.runtime.review_pending(camera, "SYNTHETIC-visual")
                direction = director.choose(sample, home.j.clock, review_pending=pending)
                if direction and director.decision_current(sample, home.j.clock, direction, review_pending=pending):
                    moves[camera].append(home.j.clock)
                    home.j.runtime.clear(camera)
                    moving_until[camera] = home.j.clock+1
                    view_offset[camera] += .05 if direction == 'right' else -.05
                    director.sent(home.j.clock, direction)
            await settle()
        events = home.j.store.events()
        assert {e["camera"] for e in events} == set(directors), (home.engine.calls, moves)
        assert all(e["sound_evidence"]["corroborating_windows"] == 2 for e in events)
        for event in events:
            marker = int(event["camera"].removeprefix("cam_"), 16)
            end = round((event["captured_at"]-1000)/.2)
            second = next(window for window in home.engine.windows if window[0] == marker and window[2] == end)
            assert any(first[0] == marker and first[2] < second[1]
                and 0 < (second[2]-first[2])*.2 <= 6
                and not any(first[1]*.2 <= move <= second[2]*.2 for move in moves[event["camera"]])
                for first in home.engine.windows), "both disjoint windows must survive in the same stable view"
        assert all(moves.values()) and home.engine.maximum == 1
        assert all(len(window.packets) <= 10 for window in home.j.runtime.sound.windows.values())
        assert len(home.j.runtime.sound.served) <= 8
        home.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.j.runtime.close()
