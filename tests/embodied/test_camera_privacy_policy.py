"""Exercise lifecycle entry points without SDKs, models, or device I/O."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.embodied import service as module


@pytest.mark.asyncio
async def test_background_recovery_discards_old_camera_open_permission():
    owner=module.EmbodiedService()
    owner.start=AsyncMock()
    owner.set_interaction=AsyncMock()
    try:
        await owner.ensure_background(open_privacy=True)
        owner.start.assert_awaited_once_with(open_privacy=False)
    finally:
        await owner.client.aclose()


@pytest.mark.asyncio
async def test_pause_blocks_game_start_preview_and_background_without_any_io(monkeypatch):
    owner=module.EmbodiedService()
    owner.capture_enabled=False
    owner.stop=AsyncMock()
    forbidden=AsyncMock(side_effect=AssertionError('paused capture must not perform I/O'))
    owner.request=forbidden
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',forbidden)
    monkeypatch.setattr(module,'camera',lambda *_: (_ for _ in ()).throw(AssertionError('no configuration I/O')))
    try:
        for app in (None,'werewolf'):
            with pytest.raises(ValueError,match='camera_capture_paused'):
                await owner.start(app_id=app)
        for options in ({},{'start':True},{'frame':True}):
            with pytest.raises(ValueError,match='camera_capture_paused'):
                await owner.preview('cam_1111111111',**options)
        await owner.ensure_background(open_privacy=True)
        owner.stop.assert_awaited_once()
        forbidden.assert_not_awaited()
    finally:
        await owner.client.aclose()


@pytest.mark.asyncio
async def test_explicit_start_cannot_create_persistent_shutter_open_permission():
    owner=module.EmbodiedService()
    try:
        with pytest.raises(ValueError,match='camera_open_in_mijia_required'):
            await owner.start(open_privacy=True)
        assert owner.process is None
    finally:
        await owner.client.aclose()


@pytest.mark.asyncio
async def test_recovery_replaces_preexisting_auto_open_worker_before_restarting():
    owner=module.EmbodiedService()
    owner.process=SimpleNamespace(returncode=None)
    owner.open_privacy=True
    async def stop():owner.process=None
    owner.stop=AsyncMock(side_effect=stop)
    owner.start=AsyncMock()
    owner.set_interaction=AsyncMock()
    try:
        await owner.ensure_background(open_privacy=True)
        owner.stop.assert_awaited_once()
        owner.start.assert_awaited_once_with(open_privacy=False)
    finally:
        await owner.client.aclose()
