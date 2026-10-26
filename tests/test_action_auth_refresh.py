import asyncio
import time
from unittest.mock import AsyncMock
import pytest
from tests.test_homeplane_actions import rig, plan, close


@pytest.mark.asyncio
async def test_cached_snapshot_still_refreshes_write_grant_once(tmp_path):
 core,_,_,post,_,_,_=rig(tmp_path)
 try:
  trace=await plan(core);snapshot=await core.snapshot()
  core.snapshot=AsyncMock(return_value=snapshot)
  core.adapter._token='EXPIRED-SYNTHETIC'
  core.adapter._token_provider=AsyncMock(return_value='FRESH-SYNTHETIC')
  result=await core.actions.execute(trace.action_id)
  assert result.status=='reported_match' and len(post)==1
  assert post[0].headers['Authorization']=='Bearer FRESH-SYNTHETIC'
  core.adapter._token_provider.assert_awaited_once()
  await core.actions.execute(trace.action_id)
  assert len(post)==1
 finally:await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['expired','auth','gesture'])
async def test_refresh_wait_cannot_bypass_final_dispatch_guards(tmp_path,failure):
 core,_,_,post,_,_,_=rig(tmp_path)
 try:
  trace=await plan(core);snapshot=await core.snapshot();core.snapshot=AsyncMock(return_value=snapshot)
  current=True
  async def token():
   nonlocal current
   if failure=='auth':raise ValueError('SYNTHETIC-secret')
   if failure=='expired':await asyncio.sleep(.05)
   current=False
   return 'FRESH-SYNTHETIC'
  core.adapter._token_provider=token
  if failure=='expired':core.actions._deadlines[trace.action_id]=time.monotonic()+.005
  if failure=='gesture':
   with pytest.raises(ValueError,match='dispatch_guard_rejected'):
    await core.actions.execute(trace.action_id,dispatch_guard=lambda:current)
  else:
   result=await core.actions.execute(trace.action_id)
   assert result.status==('expired' if failure=='expired' else 'rejected')
   assert not result.real_write_attempted and 'secret' not in result.model_dump_json()
  assert not post
 finally:await close(core)
