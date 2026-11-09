"""Eight qualified camera feeds and a serial slow classifier, synthetic only."""

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from homeplane.embodied.camera_audio import AudioIngress, PCMFramer
from homeplane.embodied.camera_provenance import camera_source_context
from tests.embodied.test_sound_review import Engine, Journey, settle


class SlowEngine(Engine):
    def __init__(self, clock, *, alternate=False):
        super().__init__()
        self.clock, self.alternate = clock, alternate
        self.pending = None
        self.active = self.maximum = 0
        self.calls = []
        self.windows = []
        self.counts = Counter()
        self.analyze.side_effect = self.run

    async def run(self, pcm):
        marker = pcm[0]
        self.calls.append((marker, self.clock()))
        self.windows.append((marker, pcm[1] + 256 * pcm[2], pcm[-6399] + 256 * pcm[-6398]))
        self.counts[marker] += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        future = asyncio.get_running_loop().create_future()
        self.pending = (self.clock() + 2.0, future)
        try:
            await future
            kind = "scream" if self.alternate and self.counts[marker] % 2 == 0 else "crying"
            return dict(kind=kind, confidence=0.96)
        finally:
            self.active -= 1
            if self.pending and self.pending[1] is future:
                self.pending = None

    def release_due(self):
        if self.pending and self.clock() >= self.pending[0]:
            future = self.pending[1]
            if not future.done():
                future.set_result(None)
                return True
        return False


class Home:
    def __init__(self, tmp_path, monkeypatch, *, alternate=False):
        self.j = Journey(tmp_path, monkeypatch)
        self.engine = SlowEngine(lambda: self.j.clock, alternate=alternate)
        self.j.runtime.sound.engine = self.engine
        self.feeds = []
        for i in range(1, 9):
            camera = dict(id=f"cam_{i:010x}", device=dict(did=f"SYNTHETIC-{i}"))
            capture = f"{i:032x}"
            self.feeds.append(
                SimpleNamespace(
                    marker=i,
                    camera=camera,
                    context=camera_source_context(camera),
                    framer=PCMFramer(camera, capture),
                    gate=AudioIngress(camera, capture),
                    capture=capture,
                )
            )
        self.excluded = set()

    async def tick(self, index):
        self.j.clock = index * 0.2
        task = self.j.runtime.sound.task
        if self.engine.release_due() and task:
            await task
            await settle()
        for feed in self.feeds:
            if feed.camera["id"] in self.excluded:
                continue
            await self.j.runtime.observe(
                feed.camera["id"],
                "SYNTHETIC-visual",
                [],
                self.j.clock,
                self.j.clock,
                stable=True,
                jpeg=b"SYNTHETIC-FRAME",
                source_context=feed.context,
                source_name="合成镜头",
            )
            frame = dict(
                type="frame",
                received_at=1000 + self.j.clock,
                capture_session=feed.capture,
                camera_source_context=feed.context,
            )
            assert feed.gate.observe_frame(frame, wall=1000 + self.j.clock, mono=self.j.clock)
            samples = bytes([feed.marker, index % 256, index // 256]) + bytes(6397)
            packet = feed.framer.push(
                samples,
                received=1000 + self.j.clock,
                decoded=1000 + self.j.clock,
                wall=1000 + self.j.clock,
                mono=self.j.clock,
            )[0]
            admitted = feed.gate.accept(packet, wall=1000 + self.j.clock, mono=self.j.clock)
            self.j.runtime.observe_audio(feed.camera["id"], admitted)
        await settle()


@pytest.mark.asyncio
async def test_eight_live_cameras_can_confirm_sound_without_weakening_the_six_second_evidence_window(
    tmp_path, monkeypatch
):
    home = Home(tmp_path, monkeypatch)
    try:
        for i in range(226):
            await home.tick(i)
        events = home.j.store.events()
        assert {e["camera"] for e in events} == {f.camera["id"] for f in home.feeds}, (
            "round-robin delay must not expire every corroborating pair"
        )
        assert len(events) == 8
        assert all(
            e["sound_evidence"]["corroborating_windows"] == 2 and e["sound_evidence"]["audio_seconds"] == 4
            for e in events
        )
        assert all(
            not e["raw_audio_stored"] and not e["directional_audio"] and e["person_id"] is None
            for e in events
        )
        for marker in range(1, 9):
            first, second = [w for w in home.engine.windows if w[0] == marker][:2]
            assert second[1] > first[2] and 0 < (second[2] - first[2]) * 0.2 <= 6
        assert home.engine.maximum == 1
        assert all(len(s.packets) == 10 for s in home.j.runtime.sound.windows.values())
        home.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.j.runtime.close()


@pytest.mark.asyncio
async def test_revoking_the_priority_source_cancels_it_without_blocking_other_cameras(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch)
    try:
        for i in range(21):
            await home.tick(i)
        camera = home.feeds[0].camera["id"]
        assert home.j.runtime.sound.active_camera == camera and home.engine.counts[1] == 2
        home.excluded.add(camera)
        home.j.runtime.clear(camera)
        await settle()
        for i in range(21, 226):
            await home.tick(i)
        assert {e["camera"] for e in home.j.store.events()} == {f.camera["id"] for f in home.feeds[1:]}
        assert camera not in home.j.runtime.sound.windows
        assert home.engine.maximum == 1
        home.j.runtime.speaker.say.assert_not_awaited()
    finally:
        await home.j.runtime.close()


@pytest.mark.asyncio
async def test_a_changing_candidate_cannot_monopolize_the_classifier(tmp_path, monkeypatch):
    home = Home(tmp_path, monkeypatch, alternate=True)
    try:
        for i in range(226):
            await home.tick(i)
        assert not home.j.store.events()
        assert set(home.engine.counts) == set(range(1, 9))
        assert min(home.engine.counts.values()) >= 2
        run = maximum = 0
        previous = None
        for marker, _ in home.engine.calls:
            run = run + 1 if marker == previous else 1
            maximum = max(maximum, run)
            previous = marker
        assert maximum <= 2, "one unconfirmed camera may get at most one follow-up before others take a turn"
        assert home.engine.maximum == 1
    finally:
        await home.j.runtime.close()
