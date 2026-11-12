"""Eight synthetic video feeds through original runtime/reviewer/private store."""

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied.camera_provenance import camera_source_context
from homeplane.embodied.safety import SafetyPolicy, SafetyRuntime, SafetyStore
from homeplane.embodied.temporal_model import VisualResult
from tests.embodied.test_sound_review import settle


class SlowVisual:
    def __init__(self, clock, *, alternate=False):
        self.clock, self.alternate = clock, alternate
        self.pending = None
        self.calls, self.windows = [], []
        self.counts = Counter()
        self.active = self.maximum = 0

    async def analyze(self, frames):
        marker = frames[0][1][0]
        self.calls.append(marker)
        self.windows.append((marker, frames[0][0], frames[-1][0]))
        self.counts[marker] += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        future = asyncio.get_running_loop().create_future()
        self.pending = (self.clock() + 10, future)
        try:
            await future
            event = "rapid_movement" if self.alternate and self.counts[marker] % 2 == 0 else "face_obscured"
            return VisualResult(event=event, observation="合成可见变化")
        finally:
            self.active -= 1
            if self.pending and self.pending[1] is future:
                self.pending = None

    async def close(self):
        pass

    def release_due(self):
        if self.pending and self.clock() >= self.pending[0] and not self.pending[1].done():
            self.pending[1].set_result(None)
            return True
        return False


class Home:
    def __init__(self, tmp_path, monkeypatch, *, alternate=False):
        from homeplane.embodied import safety as module

        self.clock = 0.0
        monkeypatch.setattr(
            module, "time", SimpleNamespace(monotonic=lambda: self.clock, time=lambda: 1000 + self.clock)
        )
        monkeypatch.setattr("homeplane.embodied.paths.PRIVATE", tmp_path)
        self.store = SafetyStore(tmp_path / "events.sqlite")
        self.store.configure(SafetyPolicy(enabled=True, visual_review=True))
        self.engine = SlowVisual(lambda: self.clock, alternate=alternate)
        self.runtime = SafetyRuntime(
            self.store, visual_engine=self.engine, speaker=SimpleNamespace(say=AsyncMock())
        )
        self.runtime.visual.clock = lambda: self.clock
        self.runtime.visual.wall = lambda: 1000 + self.clock
        self.feeds = [
            (
                i,
                f"cam_{i:010x}",
                camera_source_context(dict(id=f"cam_{i:010x}", device={"did": f"SYNTHETIC-{i}"})),
            )
            for i in range(1, 9)
        ]
        self.excluded = set()

    async def tick(self, index):
        self.clock = index * 0.5
        task = self.runtime.visual.task
        if self.engine.release_due() and task:
            await task
            await settle()
        for marker, camera, context in self.feeds:
            if camera not in self.excluded:
                await self.runtime.observe(
                    camera,
                    "SYNTHETIC-visual",
                    [],
                    self.clock,
                    self.clock,
                    stable=True,
                    jpeg=bytes([marker]) + b"SYNTHETIC-FRAME",
                    source_context=context,
                    source_name="合成镜头",
                )
        await settle()


@pytest.mark.asyncio
async def test_eight_cameras_can_corroborate_before_oldest_evidence_expires(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    try:
        for i in range(1, 341):
            await home.tick(i)
        events = home.store.events()
        assert {e["camera"] for e in events} == {c for _, c, _ in home.feeds}, (
            "serial round-robin must not expire all corroborating pairs"
        )
        assert len(events) == 8 and home.engine.maximum == 1
        assert all(
            e["kind"] == "visual_review"
            and len(e["frames"]) == 8
            and e["announcement"]["status"] == "not_requested"
            for e in events
        )
        for marker in range(1, 9):
            first, second = [w for w in home.engine.windows if w[0] == marker][:2]
            assert second[1] > first[2] and second[2] - first[2] <= 60
        assert all(len(w.frames) == 4 for w in home.runtime.visual.windows.values())
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_changing_visual_candidates_yield_after_one_followup(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch, alternate=True)
    try:
        for i in range(1, 341):
            await home.tick(i)
        assert not home.store.events() and set(home.engine.counts) == set(range(1, 9))
        assert min(home.engine.counts.values()) >= 2
        previous = None
        run = 0
        for marker in home.engine.calls:
            run = run + 1 if marker == previous else 1
            assert run <= 2, "a changing candidate cannot starve other cameras"
            previous = marker
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_revoked_priority_camera_cannot_record_or_block_other_feeds(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    try:
        for i in range(1, 29):
            await home.tick(i)
        camera = home.feeds[0][1]
        assert home.runtime.visual.active_camera == camera and home.engine.counts[1] == 2
        home.excluded.add(camera)
        home.runtime.clear(camera)
        await settle()
        for i in range(29, 341):
            await home.tick(i)
        assert {e["camera"] for e in home.store.events()} == {c for _, c, _ in home.feeds[1:]}
        assert camera not in home.runtime.visual.windows and home.engine.maximum == 1
        home.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.runtime.close()


@pytest.mark.asyncio
async def test_policy_revocation_clears_priority_without_waiting_for_another_camera(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    try:
        for i in range(1, 29):
            await home.tick(i)
        calls = len(home.engine.calls)
        policy = home.store.policy()
        home.store.configure(policy.model_copy(update={"visual_review": False}))
        await home.runtime.sync_policy()
        for i in range(29, 60):
            await home.tick(i)
        assert not home.store.events() and not home.runtime.visual.windows
        assert len(home.engine.calls) == calls and home.engine.active == 0
    finally:
        await home.runtime.close()
