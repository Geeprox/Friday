"""Qualified synthetic sound through original camera loops and the event store."""

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied.camera_audio import AudioIngress, PCMFramer
from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.sound_review import SoundReviewer
from tests.embodied.test_review_motion import source_function
from tests.embodied.test_sound_followup import Home, SlowEngine
from tests.embodied.test_sound_review import CAMERA, CONTEXT, Engine, Journey, settle


class EpisodeEngine(SlowEngine):
    def __init__(self, clock):
        super().__init__(clock)
        self.qualified = []

    async def run(self, pcm):
        result = await super().run(pcm)
        if all(pcm[i * 6400] == 1 for i in range(10)):
            self.qualified.append((pcm[1]+256*pcm[2], pcm[-6399]+256*pcm[-6398]))
            return result
        return None


class SoundPatrol:
    def __init__(self, tmp_path, monkeypatch, owner, *, episode=(14.2, 18.2)):
        self.j = Journey(tmp_path, monkeypatch)
        self.engine = EpisodeEngine(lambda: self.j.clock)
        self.j.runtime.sound.engine = self.engine
        self.owner, self.episode = owner, episode
        self.index = 0
        self.moving_until = 0
        self.moves, self.phases = [], []
        self.state = {}
        self.session = "SYNTHETIC-sound-motion"
        self.director = CameraDirector()
        self.item = dict(privacy_closed=False, source_active=True, control_epoch=1,
                         mode="auto", mode_revision="intent")
        self.scope = dict(time=SimpleNamespace(monotonic=lambda: self.j.clock),
                          asyncio=SimpleNamespace(sleep=self.advance, CancelledError=asyncio.CancelledError),
                          CameraDirector=lambda: self.director, camera_is_open=AsyncMock(return_value=True))

    async def advance(self, _=0):
        self.index += 1
        self.j.clock = self.index * .2
        if self.j.clock > 40:
            raise asyncio.CancelledError
        task = self.j.runtime.sound.task
        if self.engine.release_due() and task:
            await task
            await settle()
        stable = self.j.clock >= self.moving_until
        await self.j.runtime.observe(CAMERA["id"], self.session, [], self.j.clock, self.j.clock,
            stable=stable, jpeg=b"SYNTHETIC-FRAME", source_context=CONTEXT, source_name="合成镜头")
        frame = dict(type="frame", received_at=1000+self.j.clock, capture_session="a"*32,
                     camera_source_context=CONTEXT)
        assert self.j.gate.observe_frame(frame, wall=1000+self.j.clock, mono=self.j.clock)
        marker = int(self.episode[0] <= self.j.clock < self.episode[1])
        samples = bytes([marker, self.index % 256, self.index // 256]) + bytes(6397)
        packet = self.j.framer.push(samples, received=1000+self.j.clock, decoded=1000+self.j.clock,
                                  wall=1000+self.j.clock, mono=self.j.clock)[0]
        self.j.runtime.observe_audio(CAMERA["id"], self.j.gate.accept(
            packet, wall=1000+self.j.clock, mono=self.j.clock))
        self.state.update(status="live", _local_received=self.j.clock, session_id=self.session,
                          tracking_current=True, scene_visible=stable, people=[])
        self.scope["last_frame"] = self.j.clock
        self.phases.append((self.j.clock, (self.state if self.owner == "primary" else self.item)
                            .get("camera_control", {}).get("phase")))
        await settle()

    async def motor(self, direction, *_, **kwargs):
        if callback := kwargs.get("decision_current"):
            assert callback(), "original final-dispatch guard must still admit this move"
        elif self.owner == "secondary":
            assert self.director.decision_current(self.state, self.j.clock, direction,
                review_pending=self.j.runtime.review_pending(CAMERA["id"], self.session))
        self.moves.append((self.j.clock, direction))
        self.moving_until = self.j.clock + 1
        self.j.runtime.clear(CAMERA["id"])
        if self.owner == "secondary":
            self.director.sent(self.j.clock, direction)

    async def run(self):
        await self.advance()
        if self.owner == "primary":
            lease = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock(return_value=True),
                                    presets=[], active=None, event=lambda *_, **__: None)
            planner = SimpleNamespace(public=lambda _: {}, mode="auto", intent_revision="intent",
                                      intent_status="configured")
            self.scope.update(Transport=lambda: SimpleNamespace(close=AsyncMock()),
                CameraLease=lambda *a, **k: lease, SearchPlanner=lambda *a: planner,
                camera_intents=SimpleNamespace(load=lambda: SimpleNamespace(
                    mode="auto", revision="intent", status="configured")),
                os=SimpleNamespace(environ={}), SELECTED={"id": CAMERA["id"]}, state=self.state,
                games=MotionGames(), previews=SimpleNamespace(sessions={}),
                home=SimpleNamespace(camera_focus=None), safety=self.j.runtime,
                motor_step=self.motor, camera_lease=None, camera_planner=None,
                reap=AsyncMock(), capture=None)
            loop = source_function("homeplane/embodied/worker.py", "camera_search", self.scope)
        else:
            self.scope.update(item=self.item, selected={"id": CAMERA["id"]}, director=self.director,
                self=SimpleNamespace(focus=lambda _: None, occupied_elsewhere=lambda _: False,
                                     review_pending=self.j.runtime.review_pending),
                current_sample=lambda: dict(self.state), privacy_closed=lambda: None,
                send_motion=self.motor)
            loop = source_function("homeplane/embodied/previews.py", "direct", self.scope)
        try:
            await loop()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("original camera loop exited before the bounded replay ended")
        await self.j.runtime.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["primary", "secondary"])
async def test_short_sound_near_a_patrol_deadline_keeps_both_original_loops_stable(
    tmp_path, monkeypatch, owner
):
    patrol = SoundPatrol(tmp_path, monkeypatch, owner)
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
        assert patrol.engine.maximum == 1
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
                              session_id="SYNTHETIC-visual", _local_received=home.j.clock)
                pending = home.j.runtime.review_pending(camera, "SYNTHETIC-visual")
                direction = director.choose(sample, home.j.clock, review_pending=pending)
                if direction and director.decision_current(sample, home.j.clock, direction, review_pending=pending):
                    moves[camera].append(home.j.clock)
                    home.j.runtime.clear(camera)
                    moving_until[camera] = home.j.clock+1
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
