"""A fresh worker event can expire while waiting for the shared actuator owner."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from homeplane.embodied.household import DeviceCommands

@pytest.mark.asyncio
async def test_gesture_expiring_behind_another_command_never_reaches_miot():
    lock=asyncio.Lock();service=SimpleNamespace(miot_cover=AsyncMock(return_value=dict(acknowledged=True,reached_target=False)))
    commands=DeviceCommands(None,lock,service)
    commands.inventory=AsyncMock(return_value=[dict(id='curtain',kind='cover',source_kind='miot',controllable=True,capabilities=['cover.position'])])
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
    service.miot_cover.assert_awaited_once_with('open','synthetic-manual')

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
