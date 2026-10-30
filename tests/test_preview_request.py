"""A disconnected local viewer must release its synthetic render operation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.preview_request import prepare_for_viewer


@pytest.mark.asyncio
async def test_disconnect_cancels_and_waits_for_renderer_cleanup():
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def render():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    with pytest.raises(ValueError, match="archive_preview_cancelled"):
        await prepare_for_viewer(request, render)
    assert entered.is_set() and cleaned.is_set()


@pytest.mark.asyncio
async def test_timeout_and_caller_cancellation_both_release_renderer():
    for cancel_caller in (False, True):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def render():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        task = asyncio.create_task(prepare_for_viewer(request, render, timeout=0.05))
        await entered.wait()
        if cancel_caller:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel_caller else TimeoutError):
            await task
        assert cleaned.is_set()


@pytest.mark.asyncio
async def test_completed_preview_and_domain_failure_are_preserved():
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    async def render():
        return {"fixture": "SIMULATED", "privacy_protected": True}

    assert await prepare_for_viewer(request, render) == {
        "fixture": "SIMULATED", "privacy_protected": True,
    }

    async def failed():
        raise ValueError("archive_privacy_budget")

    with pytest.raises(ValueError, match="archive_privacy_budget"):
        await prepare_for_viewer(request, failed)


@pytest.mark.asyncio
async def test_completed_but_undelivered_preview_is_revoked():
    for cancel_caller in (False, True):
        checking = asyncio.Event()
        discarded = AsyncMock()

        async def disconnect():
            checking.set()
            if cancel_caller:
                await asyncio.Event().wait()
            return True

        async def render():
            return {"token": "SIMULATED-finished-preview"}

        task = asyncio.create_task(prepare_for_viewer(
            SimpleNamespace(is_disconnected=disconnect), render, discard=discarded,
        ))
        await checking.wait()
        if cancel_caller:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel_caller else ValueError):
            await task
        discarded.assert_awaited_once_with({"token": "SIMULATED-finished-preview"})
