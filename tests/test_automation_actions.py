import asyncio
import json
from datetime import UTC,datetime,timedelta

import pytest

from homeplane.automation_actions import AutomationActions,RecordEdit,RunRecord
from tests.test_homeplane_actions import close
from tests.test_homeplane_core_rules import cleanup, fixture, proposal, enable, independent_lighting


@pytest.mark.asyncio
async def test_pause_resume_edit_fences_and_explicit_run_reuse_action_owner(tmp_path):
    core,rows,calls,fault,path,grant,owner,manager,runtime=await fixture(tmp_path)
    try:
        plan=await enable(manager,await proposal(core,manager));record=plan.new
        service=AutomationActions(manager,runtime,asyncio.Lock())
        paused=await service.set_enabled(record.id,RecordEdit(expected_fingerprint=record.fingerprint,
            expected_context=manager.context()['fingerprint'],enabled=False))
        assert not paused.enabled and calls==[]
        with pytest.raises(ValueError):await service.set_enabled(record.id,RecordEdit(
            expected_fingerprint=record.fingerprint,expected_context=manager.context()['fingerprint'],enabled=True))
        active=await service.set_enabled(paused.id,RecordEdit(expected_fingerprint=paused.fingerprint,
            expected_context=manager.context()['fingerprint'],enabled=True))
        request=RunRecord(expected_fingerprint=active.fingerprint,expected_context=manager.context()['fingerprint'],event_id='explicit-run')
        result=await service.run(active.id,request)
        assert result['status']=='reported_match' and result['physically_confirmed'] is False
        assert await service.run(active.id,request)==result
        assert len(calls)==1
    finally:await close(core)


@pytest.mark.asyncio
async def test_manual_independent_lighting_uses_the_same_offline_policy_and_receipt(tmp_path):
    core, rows, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path, second_light=True)
    try:
        record = (await enable(manager, await independent_lighting(core, manager))).new
        primary = next(row for row in rows if row["entity_id"] == "light.simulated")
        primary.update(state="unavailable", attributes={})
        actions = AutomationActions(manager, runtime, asyncio.Lock())
        for enabled in (False, True):
            changed = await actions.set_enabled(record.id, RecordEdit(expected_fingerprint=record.fingerprint,
                expected_context=manager.context()["fingerprint"], enabled=enabled))
            assert changed.enabled is enabled and changed.rule.actions == record.rule.actions and not calls
            record = changed
        request = RunRecord(expected_fingerprint=record.fingerprint,
                            expected_context=manager.context()["fingerprint"], event_id="synthetic-partial")
        result = await actions.run(record.id, request)
        assert result["status"] == "partial" and result["physically_confirmed"] is False
        assert [effect["status"] for effect in result["effects"]] == ["skipped_unavailable", "reported_match"]
        assert await actions.run(record.id, request) == result
        assert [json.loads(call.content)["entity_id"] for call in calls] == ["light.simulated_second"]
        assert manager.read(record.id) == record
    finally:
        await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_known_false_time_condition_can_be_saved_without_sending_actions(tmp_path):
    core,rows,calls,fault,path,grant,owner,manager,runtime=await fixture(tmp_path)
    try:
        now=datetime.now(UTC);start=(now+timedelta(hours=2)).strftime('%H:%M');end=(now+timedelta(hours=3)).strftime('%H:%M')
        request=await proposal(core,manager,conditions=[dict(type='time_window',start=start,end=end,timezone='UTC')])
        plan=await manager.plan(request);trial=await manager.trial(plan.id)
        assert trial.trial.reasons==['outside_time_window']
        assert trial.status=='awaiting_approval' and calls==[]
    finally:await close(core)
