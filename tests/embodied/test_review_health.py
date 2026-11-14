"""用合成画面和声音验证复核状态与当前采集来源的对应。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import service as service_module
from homeplane.embodied.safety import SafetyPolicy
from homeplane.embodied.temporal_review import TemporalReviewer
from tests.embodied.test_camera_intent import actual_worker_functions
from tests.embodied.test_sound_review import CAMERA, Journey, settle


@pytest.mark.asyncio
async def test_visual_health_needs_recent_results_from_the_current_view(monkeypatch):
    monkeypatch.setattr('homeplane.embodied.temporal_review.model_readiness', lambda: {'available': True})
    now = 0
    never = asyncio.Event()
    result = {'event': 'none', 'observation': '无变化'}

    async def late_reply(_frames):
        await never.wait()
        return result

    engine = SimpleNamespace(analyze=AsyncMock(side_effect=late_reply), close=AsyncMock())
    policy = SafetyPolicy(enabled=True, visual_review=True)
    reviewer = TemporalReviewer(policy=lambda: policy, deliver=AsyncMock(), engine=engine, clock=lambda: now)

    async def offer(stamp, session='first', camera='one'):
        nonlocal now
        now = stamp
        reviewer.offer(camera, session, policy.revision, stamp, b'SYNTHETIC', stable=True)
        await settle()

    try:
        assert reviewer.status()['progress'] == 'waiting_input'
        for second in range(4):
            await offer(second)
        assert engine.analyze.await_count == 1
        assert reviewer.status()['progress'] == 'waiting'
        for second in range(4, 22):
            await offer(second)
        assert reviewer.status()['progress'] == 'delayed'
        never.set()
        await settle()
        assert reviewer.completed == 0
        await offer(22)
        assert reviewer.status()['progress'] == 'running'
        await offer(22, camera='two')
        assert reviewer.status()['progress'] == 'waiting'
        await offer(23, session='replacement')
        assert reviewer.status()['progress'] == 'waiting'
        now = 26
        assert reviewer.status()['progress'] == 'waiting_input'
    finally:
        await reviewer.close()


@pytest.mark.asyncio
async def test_sound_readback_reports_pressure_and_revokes_old_worker_status(tmp_path, monkeypatch):
    journey = Journey(tmp_path, monkeypatch)
    journey.engine.analyze.return_value = None
    runtime = journey.runtime
    monkeypatch.setattr(service_module, 'PRIVATE', tmp_path)
    monkeypatch.setattr(service_module, 'configuration', lambda: {'cameras': [CAMERA]})
    owner = service_module.EmbodiedService()
    owner.configured = lambda: True
    owner.process = SimpleNamespace(returncode=None)
    owner.capture_enabled = True
    scope = dict(view=lambda: {'status': 'live', 'frame': 'SYNTHETIC', 'people': []}, safety=runtime,
                 time=SimpleNamespace(monotonic=lambda: journey.clock), full_vision_deadline=0.0)
    actual_worker_functions({'status', 'benchmark_full_vision'}, scope)

    async def request(path):
        assert path == '/api/status'
        snapshot = await scope['status']()
        assert 'frame' not in snapshot
        return snapshot

    async def state():
        return (await owner.status())['safety_review']

    owner.request = request
    try:
        await runtime.sync_policy()
        assert (await request('/api/status'))['full_vision_benchmark'] is False
        scope['full_vision_deadline'] = journey.clock + 1
        assert (await request('/api/status'))['full_vision_benchmark'] is True
        assert await state() == {'sound': 'waiting_input'}
        for index in range(9):
            await journey.tick(index)
        assert (await request('/api/status'))['full_vision_benchmark'] is False
        assert await state() == {'sound': 'waiting'}
        await journey.tick(9)
        assert await state() == {'sound': 'running'}
        journey.engine.analyze.side_effect = ValueError('sound_cpu_busy')
        for index in range(10, 25):
            await journey.tick(index)
        assert await state() == {'sound': 'resource_limited'}

        pause = tmp_path / 'voice-input-paused'
        pause.touch()
        assert await state() == {'sound': 'paused'}
        pause.unlink()
        journey.engine.analyze.side_effect = None
        for index in range(25, 108):
            await journey.tick(index)
        assert await state() == {'sound': 'running'}
        journey.clock += 3
        assert await state() == {'sound': 'recovering'}
        await runtime.sync_policy()
        assert await state() == {'sound': 'waiting_input'}
        journey.engine.readiness = lambda: {'available': False}
        assert await state() == {'sound': 'unavailable'}

        async def replaced(path):
            snapshot = await request(path)
            owner.process = SimpleNamespace(returncode=None)
            return snapshot

        owner.request = replaced
        replaced_status = await owner.status()
        assert replaced_status['state'] == 'unavailable' and 'safety_review' not in replaced_status
        assert journey.store.events() == []
        runtime.speaker.say.assert_not_awaited()
    finally:
        await owner.client.aclose()
        await runtime.close()
