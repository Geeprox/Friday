"""Original task outcomes -> exact local acknowledgement; synthetic I/O only."""
from datetime import UTC, datetime, timedelta
import json
import subprocess
import pytest

from homeplane.automation_actions import AttentionAcknowledgement
from homeplane.core_rules import CoreRuleRuntime
from homeplane.rule_contracts import OnceTrigger
from tests.test_homeplane_core_rules import fixture, proposal, enable, event, cleanup


def acknowledgement(item):
    return AttentionAcknowledgement(record_id=item['record_id'],expected_fingerprint=item['fingerprint'],run_id=item['run_id'])


@pytest.mark.asyncio
async def test_missed_once_is_visible_before_clock_receipt_and_seen_state_survives_restart(tmp_path,monkeypatch):
    from homeplane import core_rules,core_schedule,automation_attention
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        proposed=await proposal(core,manager);due=datetime.now(UTC)+timedelta(seconds=5)
        proposed=proposed.model_copy(update={'rule':proposed.rule.model_copy(update={'trigger':OnceTrigger(at=due.isoformat(),timezone='UTC')})})
        saved=(await enable(manager,proposed)).new
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return(due+timedelta(seconds=60)).astimezone(tz or UTC)
        for module in (core_rules,core_schedule,automation_attention):monkeypatch.setattr(module,'datetime',Frozen)
        first=runtime.status()['task_attention']['items'];assert len(first)==1 and first[0]['kind']=='missed'
        item=first[0];assert item['title']==saved.rule.intent and item['record_current']
        assert runtime.attention.acknowledge(item['id'],acknowledgement(item))['device_actions'] is False
        await runtime.tick();assert not runtime.status()['task_attention']['items'] and not calls
        await runtime.close();runtime=CoreRuleRuntime(manager);runtime.start=lambda:None
        assert not runtime.status()['task_attention']['items']
        assert runtime.attention.acknowledge(item['id'],acknowledgement(item))['status']=='acknowledged'
        assert not calls
    finally:await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_unknown_delivery_keeps_its_original_title_after_edit_and_new_failures_are_not_hidden(tmp_path):
    core,_,calls,fault,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        old=(await enable(manager,await proposal(core,manager))).new
        await runtime.set_mode('awake');fault['send']='timeout'
        await runtime.ingest(core.adapter,event(core))
        [first]=runtime.status()['task_attention']['items']
        assert first['kind']=='unconfirmed' and first['title']==old.rule.intent and len(calls)==1
        edited=await proposal(core,manager,old=old)
        edited=edited.model_copy(update={'rule':edited.rule.model_copy(update={'intent':'SYNTHETIC changed task name'})})
        await enable(manager,edited)
        [history]=runtime.status()['task_attention']['items']
        assert history['id']==first['id'] and history['title']==old.rule.intent and not history['record_current']
        runtime.attention.acknowledge(first['id'],acknowledgement(first))
        assert not runtime.status()['task_attention']['items']
        await runtime.set_mode('awake');await runtime.ingest(core.adapter,event(core))
        [second]=runtime.status()['task_attention']['items']
        assert second['id']!=first['id'] and second['title']=='SYNTHETIC changed task name' and len(calls)==2
        with pytest.raises(ValueError):runtime.attention.acknowledge(second['id'],acknowledgement(first))
        assert len(runtime.status()['task_attention']['items'])==1
        before=len(calls);await runtime.close();runtime=CoreRuleRuntime(manager);runtime.start=lambda:None
        assert runtime.status()['task_attention']['items'][0]['id']==second['id'] and len(calls)==before
    finally:await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_console_review_acknowledges_the_exact_task_without_restarting_it(tmp_path,monkeypatch):
    import asyncio
    from homeplane import core_rules,core_schedule,automation_attention
    from tests.test_homeplane_task_composition import TaskRuntime,task_app,approve
    from tests.test_homeplane_home_flow import domain,request,turn
    due=datetime.now(UTC)+timedelta(days=1);model=TaskRuntime()
    def timed(draft,data):draft['trigger']=dict(type='once',at=due.strftime('%Y-%m-%dT%H:%M:%S'));return draft
    model.mutate=timed
    async with task_app(tmp_path,model,household_timezone='UTC')as(app,client,calls,session):
        runtime=app.state.core_rule_runtime
        if runtime.worker:runtime.worker.cancel();await asyncio.gather(runtime.worker,return_exceptions=True);runtime.worker=None
        runtime.start=lambda:None
        shown=await turn(client,request(session,operation='task:one',text='创建任务：明天7点先开灯，等待50毫秒，再打开插座。'))
        assert shown['phase']=='preview',shown
        saved=domain(await turn(client,approve(session,shown)))['plan']['new']
        await client.post('/api/home-flow/dispose',json={'session_id':session})
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return(due+timedelta(seconds=60)).astimezone(tz or UTC)
        for module in(core_rules,core_schedule,automation_attention):monkeypatch.setattr(module,'datetime',Frozen)
        report=(await client.get('/api/rules/core/runtime')).json()
        [item]=report['task_attention']['items'];assert item['record_id']==saved['id']
        path='/api/home/automations/records/attention/'+item['id']+'/acknowledge'
        ui=subprocess.run(['node','tests/task_attention_journey.cjs'],input=json.dumps(dict(record=saved,runtime=report,id=item['id'])),capture_output=True,text=True,timeout=15)
        assert ui.returncode==0,ui.stdout+ui.stderr
        clicked=json.loads(ui.stdout);assert clicked['path']==path
        body=clicked['body'];assert body==acknowledgement(item).model_dump(mode='json')
        assert (await client.post(path,json=body,headers={'Origin':'https://example.invalid'})).status_code==403
        assert (await client.get('/api/rules/core/runtime')).json()['task_attention']['items'][0]['id']==item['id']
        assert (await client.post(path,json={**body,'run':True})).status_code==422
        assert (await client.post(path,json={**body,'expected_fingerprint':'f'*64})).status_code==409
        done=await client.post(path,json=body);assert done.status_code==200 and done.json()['device_actions'] is False
        assert (await client.post(path,json=body)).json()==done.json()
        updated=(await client.get('/api/rules/core/runtime')).json()
        assert not updated['task_attention']['items']
        ui=subprocess.run(['node','tests/task_attention_journey.cjs'],input=json.dumps(dict(record=saved,runtime=updated,id=item['id'],cleared=True)),capture_output=True,text=True,timeout=15)
        assert ui.returncode==0,ui.stdout+ui.stderr
        assert app.state.core_rules.read(saved['id']).enabled and not calls


def test_original_home_and_automation_attention_journey():
    result=subprocess.run(['node','--test','tests/task_attention.test.cjs','tests/home_status.test.mjs','tests/navigation_viewport.test.cjs','tests/automation_editor.test.cjs','tests/once_automation.test.cjs','tests/home_experience_lifecycle.test.cjs'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['offline','condition'])
async def test_unavailable_once_is_actionable_while_an_unmet_condition_stays_quiet(tmp_path,monkeypatch,failure):
    from homeplane import core_rules,core_schedule,automation_attention
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        due=datetime.now(UTC)+timedelta(seconds=5)
        conditions=[] if failure=='offline'else[dict(type='time_window',start=(due+timedelta(hours=2)).strftime('%H:%M'),end=(due+timedelta(hours=3)).strftime('%H:%M'),timezone='UTC')]
        proposed=await proposal(core,manager,conditions=conditions)
        proposed=proposed.model_copy(update={'rule':proposed.rule.model_copy(update={'trigger':OnceTrigger(at=due.isoformat(),timezone='UTC')})})
        await enable(manager,proposed);await runtime.set_mode('awake')
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return(due+timedelta(seconds=1)).astimezone(tz or UTC)
        for module in(core_rules,core_schedule,automation_attention):monkeypatch.setattr(module,'datetime',Frozen)
        if failure=='offline':
            async def unavailable():raise OSError('SYNTHETIC-private-path-not-for-the-interface')
            monkeypatch.setattr(core,'snapshot',unavailable)
        await runtime.tick();report=runtime.status()
        if failure=='offline':
            [item]=report['task_attention']['items'];assert item['kind']=='not_executed'
            assert 'private-path'not in json.dumps(report['task_attention'])
            runtime.attention.acknowledge(item['id'],acknowledgement(item))
        else:assert not report['task_attention']['items']
        assert not calls
    finally:await cleanup(core,owner,runtime)
