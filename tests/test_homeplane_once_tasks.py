"""One-off task date/elapsed timing and durable source execution; synthetic only."""
import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from pydantic import ValidationError
from homeplane.rule_contracts import RuleIR, ScheduleTrigger
from homeplane.rule_compiler import digest
from homeplane.task_composition import is_task_request

def once_rule(at='2026-11-10T07:00', zone='Asia/Shanghai'):
    return RuleIR.model_validate(dict(id='SYNTHETIC-once',revision=1,intent='SYNTHETIC one-off light',
        trigger=dict(type='once',at=at,timezone=zone),
        actions=[dict(type='device.set',id='one',target='SYNTHETIC-target',operation='light.set',desired={'power':'on'})],
        execution={'preferred_backend':'home_core'}))

def test_recurring_rule_serialization_and_fingerprint_remain_compatible():
    trigger=ScheduleTrigger(at='22:00',weekdays=[1,2,3,4,5],timezone='Asia/Shanghai')
    assert digest(trigger)=='956848d57dfd52e20a8a039ef008c8ec952e41bebd135c13ee8d3d20dd2c0c60'

@pytest.mark.parametrize('text',['十分钟后把灯关了','30秒后打开灯','半小时后关闭插座','两小时以后关灯','2027-01-01 08:00打开灯','11月10日7点打开插座','2027年1月9日07:00关闭灯'])
def test_elapsed_instructions_enter_the_task_lane_without_losing_the_delay(text):
    assert is_task_request(text)

def test_one_off_local_time_is_canonical_and_never_recurs_the_next_day():
    from homeplane.rule_schedule import due_occurrence,next_occurrence
    rule=once_rule();trigger=rule.trigger;at=datetime(2026,11,9,23,tzinfo=UTC)
    assert trigger.at==at.isoformat()
    assert due_occurrence(trigger,at+timedelta(seconds=1),grace_seconds=3).at==at
    assert due_occurrence(trigger,at+timedelta(days=1),grace_seconds=3) is None
    assert next_occurrence(trigger,at-timedelta(seconds=1)).at==at
    assert next_occurrence(trigger,at+timedelta(seconds=1)) is None
    assert next_occurrence(trigger,at,after_day='2026-11-10') is None

@pytest.mark.parametrize('at,zone',[('2026-02-30T07:00','UTC'),('2026-03-08T02:30','America/New_York'),('2026-11-10','UTC'),('2026-11-10T25:00','UTC'),('2026-11-10T07:00','missing/invalid'),(True,'UTC')])
def test_invalid_or_nonexistent_one_off_times_are_rejected(at,zone):
    with pytest.raises(ValidationError):once_rule(at,zone)

def test_elapsed_deadline_keeps_the_exact_second_fold_instant():
    from homeplane.rule_schedule import next_occurrence
    trigger=once_rule('2026-11-01T06:30:00+00:00','America/New_York').trigger
    assert next_occurrence(trigger,datetime(2026,11,1,6,tzinfo=UTC)).at==datetime(2026,11,1,6,30,tzinfo=UTC)
    local=once_rule('2026-11-01T01:30','America/New_York').trigger
    assert next_occurrence(local,datetime(2026,11,1,4,tzinfo=UTC)).at==datetime(2026,11,1,5,30,tzinfo=UTC)

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['once','after'])
async def test_console_task_keeps_the_entire_one_off_plan_non_actuating_until_approval(tmp_path,kind):
    from tests.test_homeplane_task_composition import TaskRuntime,task_app
    from tests.test_homeplane_home_flow import domain,request,turn
    model=TaskRuntime();local=(datetime.now(UTC)+timedelta(days=1)).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%dT%H:%M')
    def timed(draft,data):
        draft['trigger']=dict(type='once',at=local)if kind=='once'else dict(type='after',seconds=600)
        return draft
    model.mutate=timed
    async with task_app(tmp_path,model)as(app,client,calls,session):
        before=datetime.now(UTC)
        shown=await turn(client,request(session,operation='task:one',text='明天7点先打开灯，等待50毫秒，再开插座。'if kind=='once'else'十分钟后先打开灯，等待50毫秒，再开插座。'))
        assert shown['phase']=='preview',shown
        result=domain(shown);trigger=result['plan']['new']['rule']['trigger']
        assert trigger['type']=='once'and trigger['timezone']=='Asia/Shanghai'
        actual=datetime.fromisoformat(trigger['at'])
        if kind=='after':assert before+timedelta(seconds=600)<=actual<=datetime.now(UTC)+timedelta(seconds=600)
        else:assert actual==datetime.fromisoformat(local).replace(tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(UTC)
        assert result['task_review']['trigger_kind']=='once'
        assert result['plan']['status']=='awaiting_approval'
        assert result['plan']['trial']['simulated']and not result['plan']['trial']['real_write_attempted']
        assert len(result['plan']['new']['rule']['actions'])==3
        assert not calls and not app.state.core_rules.records()


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario',['completed','missed','paused'])
async def test_confirmed_once_task_survives_console_disposal_and_never_replays(tmp_path,monkeypatch,scenario):
    from homeplane import core_rules,core_schedule
    from homeplane.core_rules import CoreRuleRuntime
    from tests.test_homeplane_task_composition import TaskRuntime,task_app,approve
    from tests.test_homeplane_home_flow import domain,request,turn
    due=(datetime.now(UTC)+timedelta(seconds=8)).replace(microsecond=0)
    model=TaskRuntime()
    def timed(draft,data):
        draft['trigger']=dict(type='once',at=due.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%dT%H:%M:%S'))
        return draft
    model.mutate=timed
    async with task_app(tmp_path,model)as(app,client,calls,session):
        runtime=app.state.core_rule_runtime
        if runtime.worker:
            runtime.worker.cancel();await asyncio.gather(runtime.worker,return_exceptions=True);runtime.worker=None
        runtime.start=lambda:None
        shown=await turn(client,request(session,operation='task:one',text='创建任务：指定时间先开灯，等待50毫秒，再开插座。'))
        assert shown['phase']=='preview',shown
        saved=domain(await turn(client,approve(session,shown)))['plan']['new']
        await client.post('/api/home-flow/dispose',json={'session_id':session})
        runtime=app.state.core_rule_runtime;manager=app.state.core_rules
        await app.state.home_experience.household_mode.select('awake')
        if scenario=='paused':
            disabled=await client.post('/api/home/automations/records/'+saved['id']+'/state',json=dict(
                enabled=False,expected_fingerprint=saved['fingerprint'],expected_context=manager.context()['fingerprint']))
            assert disabled.status_code==200,disabled.text
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return (due+timedelta(seconds=60 if scenario=='missed' else 1)).astimezone(tz or UTC)
        monkeypatch.setattr(core_rules,'datetime',Frozen);monkeypatch.setattr(core_schedule,'datetime',Frozen)
        await runtime.tick();await asyncio.sleep(.06);await runtime.tick()
        state=runtime.status()['clock']['one_off_tasks'][saved['id']]['state']
        if scenario=='completed':
            assert len(calls)==2,runtime.status()
            assert state=='completed'
        else:
            assert not calls
            assert state==('missed'if scenario=='missed'else'cancelled')
        await runtime.tick();before=len(calls)
        if scenario!='paused':
            with app.state.core.store.connection:app.state.core.store.connection.execute('DELETE FROM core_rule_runs')
            assert runtime.status()['clock']['one_off_tasks'][saved['id']]['state']=='finished'
        await runtime.close();restarted=CoreRuleRuntime(manager)
        try:
            await restarted.tick();assert len(calls)==before and not restarted.pending
        finally:await restarted.close()


@pytest.mark.asyncio
async def test_a_one_off_time_that_passes_during_review_cannot_be_approved(tmp_path,monkeypatch):
    from homeplane import core_rules
    from tests.test_homeplane_core_rules import fixture,proposal,cleanup
    from homeplane.core_rules import CoreRuleApproval
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        proposed=await proposal(core,manager);due=datetime.now(UTC)+timedelta(seconds=5)
        trigger=once_rule(due.isoformat(),'UTC').trigger
        proposed=proposed.model_copy(update={'rule':proposed.rule.model_copy(update={'trigger':trigger})})
        plan=await manager.plan(proposed);plan=await manager.trial(plan.id)
        assert plan.status=='awaiting_approval'
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return(due+timedelta(seconds=1)).astimezone(tz or UTC)
        monkeypatch.setattr(core_rules,'datetime',Frozen)
        with pytest.raises(ValueError,match='schedule_time_unavailable'):
            await manager.apply(CoreRuleApproval(plan_id=plan.id,approval_digest=plan.approval_digest))
        assert not calls and not manager.records()
    finally:await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_relative_task_uses_the_unmodified_background_clock_after_console_close(tmp_path):
    from tests.test_homeplane_task_composition import TaskRuntime,task_app,approve
    from tests.test_homeplane_home_flow import domain,request,turn
    model=TaskRuntime()
    def timed(draft,data):
        draft['trigger']=dict(type='after',seconds=3)
        return draft
    model.mutate=timed
    async with task_app(tmp_path,model)as(app,client,calls,session):
        shown=await turn(client,request(session,operation='task:one',text='3秒后先打开灯，等50毫秒，再打开插座。'))
        assert shown['phase']=='preview',shown
        saved=domain(await turn(client,approve(session,shown)))['plan']['new']
        due=datetime.fromisoformat(saved['rule']['trigger']['at'])
        await client.post('/api/home-flow/dispose',json={'session_id':session})
        runtime=app.state.core_rule_runtime
        await app.state.home_experience.household_mode.select('awake')
        while datetime.now(UTC)<due+timedelta(seconds=6)and(len(calls)<2 or runtime.pending):
            now=datetime.now(UTC);stream=app.state.core.adapter._stream
            stream.receipts={key:now for key in stream.rows};stream.published_receipts=dict(stream.receipts)
            await asyncio.sleep(.03)
        assert [c.url.path for c in calls]==['/api/services/light/turn_on','/api/services/switch/turn_on'],runtime.status()
        assert runtime.status()['clock']['one_off_tasks'][saved['id']]['state']=='completed'
        await runtime.tick();assert len(calls)==2 and len(model.calls)==1


def test_one_off_editor_and_original_status_owner():
    import subprocess
    result=subprocess.run(['node','--test','tests/once_automation.test.cjs','tests/automation_editor.test.cjs'],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
