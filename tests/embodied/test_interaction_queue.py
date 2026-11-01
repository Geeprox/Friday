"""A fresh worker event can expire while waiting for the shared actuator owner."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from homeplane.embodied.household import DeviceCommands

@pytest.mark.asyncio
async def test_gesture_expiring_behind_another_command_never_reaches_miot():
    lock=asyncio.Lock();service=SimpleNamespace(miot_cover=AsyncMock(return_value=dict(acknowledged=True,reached_target=False)))
    commands=DeviceCommands(None,lock,service)
    commands.inventory=AsyncMock(return_value=[dict(id='curtain',kind='cover',source_kind='miot',controllable=True,capabilities=['cover.position'],binding_revision='synthetic-binding')])
    admitted=[True]
    await lock.acquire()
    task=asyncio.create_task(commands.command(None,'open','synthetic-gesture',target='curtain',dispatch_guard=lambda:admitted[0]))
    await asyncio.sleep(0)
    admitted[0]=False;lock.release()
    result=await task
    assert result['outcomes'][0]['code']=='stale_interaction' and not result['write_attempted'] and not result['acknowledged']
    service.miot_cover.assert_not_called()
    # Manual UI commands are independent of a camera event's short validity.
    manual=await commands.command(None,'open','synthetic-manual',target='curtain')
    assert manual['acknowledged']
    service.miot_cover.assert_awaited_once_with('open','synthetic-manual',expected_revision='synthetic-binding')

@pytest.mark.asyncio
async def test_actual_ha_send_boundary_rechecks_event_after_plan(tmp_path):
    from tests.test_homeplane_device_controls import rig,plan
    from tests.test_homeplane_actions import close
    core,rows,calls,fault,path,authority=rig(tmp_path)
    try:
        trace=await plan(core,'light.set',{'power':'on'})
        assert trace.status=='planned'
        with pytest.raises(ValueError,match='dispatch_guard_rejected'):
            await core.actions.execute(trace.action_id,dispatch_guard=lambda:False)
        assert not calls
    finally:await close(core)


@pytest.mark.asyncio
async def test_fresh_ha_gesture_dispatches_while_previous_readback_is_pending(tmp_path, monkeypatch):
    """Waiting for a curtain to settle must not consume the next gesture's TTL."""
    from tests.test_homeplane_device_controls import rig
    from tests.test_homeplane_actions import close

    core, _, calls, *_ = rig(tmp_path)
    commands = DeviceCommands(core, asyncio.Lock())
    reading, release = asyncio.Event(), asyncio.Event()
    settle = core.actions._settle
    tasks = []

    async def delayed_readback(writer, desired):
        if desired == {'position': 100}:
            reading.set()
            await release.wait()
        return await settle(writer, desired)

    monkeypatch.setattr(core.actions, '_settle', delayed_readback)
    try:
        target = next(i['id'] for i in await commands.inventory() if i['kind'] == 'cover')
        first = asyncio.create_task(commands.command(
            None, 'open', 'SIMULATED-open', target=target, dispatch_guard=lambda: True))
        tasks.append(first)
        await asyncio.wait_for(reading.wait(), 1)
        second = asyncio.create_task(commands.command(
            None, 'close', 'SIMULATED-close', target=target, dispatch_guard=lambda: True))
        tasks.append(second)
        result = await asyncio.wait_for(asyncio.shield(second), 1)
        assert not first.done()
        assert [json.loads(c.content)['position'] for c in calls] == [100, 0]
        assert result['reached_target'] and not result['physical_confirmation']
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_readback', [False, True])
async def test_duplicate_ha_gesture_waits_for_own_readback_without_resend(tmp_path, monkeypatch, cancel_readback):
    from tests.test_homeplane_device_controls import rig
    from tests.test_homeplane_actions import close

    core, _, calls, *_ = rig(tmp_path)
    commands = DeviceCommands(core, asyncio.Lock())
    reading, release = asyncio.Event(), asyncio.Event()
    settle = core.actions._settle
    tasks = []

    async def delayed_readback(writer, desired):
        reading.set()
        await release.wait()
        return await settle(writer, desired)

    monkeypatch.setattr(core.actions, '_settle', delayed_readback)
    try:
        target = next(i['id'] for i in await commands.inventory() if i['kind'] == 'cover')
        async def same_event():
            return await commands.command(None, 'open', 'SIMULATED-same-event',
                                          target=target, dispatch_guard=lambda: True)
        first = asyncio.create_task(same_event())
        tasks.append(first)
        await asyncio.wait_for(reading.wait(), 1)
        duplicate = asyncio.create_task(same_event())
        tasks.append(duplicate)
        await asyncio.sleep(0)
        assert not duplicate.done() and len(calls) == 1
        if cancel_readback:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        else:
            release.set()
        result = await asyncio.wait_for(duplicate, 1)
        assert len(calls) == 1 and result['write_attempted']
        assert result['status'] == ('unknown_outcome' if cancel_readback else 'reported_match')
        assert not result['physical_confirmation']
        if not cancel_readback:
            assert await first == result
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close(core)


@pytest.mark.asyncio
async def test_ha_send_still_serializes_and_rejects_gesture_expiring_during_transport(tmp_path, monkeypatch):
    from homeplane.ha_control import HADeviceWriter
    from tests.test_homeplane_device_controls import rig
    from tests.test_homeplane_actions import close

    core, _, calls, fault, *_ = rig(tmp_path)
    fault['mismatch'] = True  # No state delta: exercise the final freshness guard.
    monkeypatch.setattr(core.actions, '_settle', AsyncMock(return_value={'position': 0}))
    commands = DeviceCommands(core, asyncio.Lock())
    sending, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    send = HADeviceWriter.send
    admitted = [True]
    tasks = []

    async def delayed_send(writer, desired):
        sending.set()
        await release.wait()
        return await send(writer, desired)

    def second_guard():
        checked.set()
        return admitted[0]

    monkeypatch.setattr(HADeviceWriter, 'send', delayed_send)
    try:
        target = next(i['id'] for i in await commands.inventory() if i['kind'] == 'cover')
        first = asyncio.create_task(commands.command(
            None, 'open', 'SIMULATED-slow-send', target=target, dispatch_guard=lambda: True))
        tasks.append(first)
        await asyncio.wait_for(sending.wait(), 1)
        second = asyncio.create_task(commands.command(
            None, 'close', 'SIMULATED-expiring', target=target, dispatch_guard=second_guard))
        tasks.append(second)
        await asyncio.wait_for(checked.wait(), 1)
        assert not calls
        admitted[0] = False
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert len(calls) == 1 and json.loads(calls[0].content)['position'] == 100
        assert not results[1]['write_attempted']
        assert results[1]['outcomes'][0]['code'] == 'stale_interaction'
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close(core)
