"""Automatic execution intent must not manufacture household mode evidence."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeplane.home_experience import HomeExperience
from tests.test_homeplane_core_rules import cleanup, enable, event, fixture, proposal


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['sleep', 'manual', 'unknown', 'awake'])
async def test_expired_mode_never_becomes_awake(tmp_path, mode):
    core, rows, calls, fault, path, grant, rules, manager, runtime = await fixture(tmp_path)
    try:
        await runtime.set_mode(mode)
        original = runtime.modes
        runtime.mode_deadline = time.monotonic() - 1
        owner = HomeExperience(core, SimpleNamespace(path=tmp_path/'settings.json'), None, None, runtime)
        runtime.set_mode = AsyncMock(wraps=runtime.set_mode)
        await owner._maintain_once()
        await runtime.set_execution(False)
        await runtime.set_execution(True)
        runtime.set_mode.assert_not_awaited()
        assert runtime.modes is original and not calls
    finally:
        await cleanup(core, rules, runtime)


@pytest.mark.asyncio
async def test_manual_hold_blocks_dispatch_then_restores_existing_awake_without_rewriting_mode(tmp_path):
    core, rows, calls, fault, path, grant, rules, manager, runtime = await fixture(tmp_path)
    try:
        await enable(manager, await proposal(core, manager))
        await runtime.set_mode('awake', 1800)
        original = runtime.modes
        await runtime.set_execution(True, manual_seconds=60)
        await runtime.ingest(core.adapter, event(core))
        assert calls == [] and runtime.modes is original
        runtime.manual_until = time.monotonic() - 1
        await runtime.ingest(core.adapter, event(core))
        assert len(calls) == 1 and runtime.modes is original
        await runtime.set_execution(False)
        await runtime.ingest(core.adapter, event(core))
        assert len(calls) == 1 and runtime.modes is original
    finally:
        await cleanup(core, rules, runtime)


@pytest.mark.asyncio
async def test_manual_hold_never_replaces_sleep_and_expiry_stays_protected(tmp_path):
    core, rows, calls, fault, path, grant, rules, manager, runtime = await fixture(tmp_path)
    try:
        await enable(manager, await proposal(core, manager))
        await runtime.set_mode('sleep', 1800)
        original = runtime.modes
        await runtime.set_execution(True, manual_seconds=60)
        runtime.manual_until = time.monotonic() - 1
        await runtime.ingest(core.adapter, event(core))
        assert calls == [] and runtime.modes is original
        assert runtime.modes['home.sleep'].value is True
    finally:
        await cleanup(core, rules, runtime)


@pytest.mark.asyncio
async def test_maintenance_waiter_cannot_overwrite_newer_unknown(tmp_path):
    core, rows, calls, fault, path, grant, rules, manager, runtime = await fixture(tmp_path)
    try:
        await runtime.set_mode('awake', 10)
        modes = runtime.modes
        await manager.lock.acquire()
        renewal = asyncio.create_task(runtime.renew_awake(modes))
        await asyncio.sleep(0)
        # Explicit unknown invalidates modes immediately, even while the same
        # device queue lock is busy, then finishes its cleanup under the lock.
        unknown = asyncio.create_task(runtime.set_mode('unknown'))
        await asyncio.sleep(0)
        manager.lock.release()
        await asyncio.gather(renewal, unknown)
        assert runtime.modes == {} and not calls
    finally:
        await cleanup(core, rules, runtime)
