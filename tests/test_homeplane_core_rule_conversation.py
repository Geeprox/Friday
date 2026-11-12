"""Actual HomeFlow -> Core configuration -> synthetic registered event -> mock IO.

These never use the Simulator rule writer or send household/device traffic.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC,datetime,timedelta
import json
from pathlib import Path
import subprocess

import pytest

from tests.test_homeplane_core_rules import enable,event,fixture,proposal
from tests.test_homeplane_home_flow import application,bootstrap,domain,request,turn
from tests.test_homeplane_home_conversation_widget import HARNESS,NODE


TEXT='这个习惯不要自动开灯'


@asynccontextmanager
async def live_app(tmp_path,operation='light.set',desired=None):
    core,_,calls,_,path,_,owner,_,runtime=await fixture(tmp_path)
    adapter=core.adapter
    await runtime.close();owner.close();core.actions.close();core.store.close()
    async with application(tmp_path,adapter=adapter,authority_path=path) as (app,client):
        manager=app.state.core_rules
        snapshot=await manager.core.snapshot()
        light=next(e for e in snapshot.entities if e.kind==operation.split('.')[0])
        switch=next(e for e in snapshot.entities if e.kind=='switch')
        actions=[{'id':'remove-light','type':'device.set','target':light.id,'operation':operation,'desired':desired or {'power':'on'}},
                 {'id':'retain-switch','type':'device.set','target':switch.id,'operation':'switch.set','desired':{'power':'on'}}]
        seed=await enable(manager,await proposal(manager.core,manager,actions=actions))
        session=(await bootstrap(client))['session_id']
        yield app,client,calls,session,seed


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,desired',[('climate.power',{'power':'on'}),('climate.mode',{'hvac_mode':'cool'})])
async def test_common_core_climate_instructions_offer_exact_removal(tmp_path,operation,desired):
    async with live_app(tmp_path,operation,desired) as (app,client,calls,session,seed):
        result=await turn(client,request(session,operation='climate:live',text='这个习惯不要自动开空调'))
        assert result['phase']=='select' and len(result['choices'])==1,result
        assert domain(result)['choices'][0]['record_id']==seed.new.id
        assert app.state.core_rules.read(seed.new.id).enabled and not calls


@pytest.mark.asyncio
async def test_scope_change_before_approval_leaves_the_original_rule_intact(tmp_path):
    async with live_app(tmp_path) as (app,client,calls,session,seed):
        shown=await preview(client,session)
        core=app.state.core_rules.core
        path=core.actions.authority_path
        grant,_=core.actions.authority()
        path.write_text(grant.model_copy(update={'expires_at':datetime.now(UTC)+timedelta(days=2)}).model_dump_json())
        result=await turn(client,approval(session,shown))
        assert result['phase'] in {'refused','unavailable','cancelled'},result
        assert app.state.core_rules.read(seed.new.id).enabled
        assert app.state.core_rules.plan_get(shown['plan_id']).status=='awaiting_approval' and not calls


async def preview(client,session,operation='rules:live'):
    offered=await turn(client,request(session,operation=operation,text=TEXT))
    assert offered['phase']=='select',offered
    result=await turn(client,request(session,operation=operation,text='',
        candidate_id=offered['choices'][0]['id'],expected_context=offered['context_token']))
    assert result['phase']=='preview',result
    return result


def approval(session,plan,operation='rules:live'):
    return request(session,operation=operation,kind='approve',plan_id=plan['plan_id'],
        approval_digest=plan['approval_digest'],expected_context=plan['context_token'])


@pytest.mark.asyncio
async def test_live_text_edit_preserves_actions_reaches_original_runtime_and_rolls_back(tmp_path):
    async with live_app(tmp_path) as (app,client,calls,session,seed):
        manager=app.state.core_rules
        shown=await preview(client,session)
        plan=domain(shown)['plan']
        assert plan['old']==seed.new.model_dump(mode='json')
        assert [a['id'] for a in plan['new']['rule']['actions']]==['retain-switch']
        assert manager.read(seed.new.id).enabled and not calls
        done=await turn(client,approval(session,shown))
        applied=domain(done)
        assert applied['status']=='verified' and applied['simulated'] is False,done
        assert applied['real_write_attempted'] is False and applied['physically_confirmed'] is False
        assert not manager.read(seed.new.id).enabled and manager.read(applied['new']['id']).enabled
        assert not calls
        await app.state.home_experience.household_mode.select('awake')
        await app.state.core_rule_runtime.ingest(manager.core.adapter,event(manager.core))
        assert len(calls)==1 and calls[0].url.path=='/api/services/switch/turn_on'
        rolled=await turn(client,request(session,operation='rules:live',kind='rollback',
            plan_id=shown['plan_id'],expected_context=done['context_token']))
        assert domain(rolled)['status']=='rolled_back',rolled
        assert manager.read(seed.new.id).enabled and len(calls)==1
        assert not manager.read(applied['new']['id']).enabled


@pytest.mark.asyncio
async def test_changed_rule_after_preview_cannot_commit_the_old_choice(tmp_path):
    async with live_app(tmp_path) as (app,client,calls,session,seed):
        shown=await preview(client,session)
        await app.state.core_rules.rollback(seed.id)
        done=await turn(client,approval(session,shown))
        assert done['phase'] in {'refused','unavailable'},done
        assert app.state.core_rules.plan_get(shown['plan_id']).status=='awaiting_approval'
        assert not any(r.enabled for r in app.state.core_rules.records()) and not calls


@pytest.mark.asyncio
async def test_cancelled_noncooperative_precommit_work_cannot_enable_a_rule(tmp_path,monkeypatch):
    async with live_app(tmp_path) as (app,client,calls,session,seed):
        shown=await preview(client,session)
        manager=app.state.core_rules
        entered,cancelled,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        original=manager._check
        async def late_check(plan):
            entered.set()
            try:await release.wait()
            except asyncio.CancelledError:
                cancelled.set();await release.wait()
            return await original(plan)
        monkeypatch.setattr(manager,'_check',late_check)
        pending=asyncio.create_task(turn(client,approval(session,shown)))
        stop=None
        try:
            await asyncio.wait_for(entered.wait(),2)
            stop=asyncio.create_task(client.post('/api/home-flow/cancel',json=request(session,
                operation='rules:live',kind='cancel',expected_context=shown['context_token'])))
            await asyncio.wait_for(cancelled.wait(),2)
            assert manager.read(seed.new.id).enabled
            release.set()
            await asyncio.wait_for(asyncio.gather(pending,stop),3)
            assert manager.read(seed.new.id).enabled
            assert manager.plan_get(shown['plan_id']).status=='awaiting_approval'
            assert not calls
        finally:
            release.set()
            await asyncio.gather(pending,*([stop] if stop else []),return_exceptions=True)


CORE_WIDGET = HARNESS.split('const config = JSON.parse(process.argv[3]);')[0] + r"""
const config=JSON.parse(process.argv[3]);
globalThis.matchMedia=()=>({matches:true,addEventListener(){},removeEventListener(){}});globalThis.innerHeight=844;
const root=document.createElement('main');document.body.append(root);
let projected,sequence=0;const calls=[];
const client={async send(request){calls.push(request);return {...projected,session_id:request.session_id,turn_id:request.turn_id,operation_id:request.operation_id};},async cancel(request){return {...config.bootstrap,...request,phase:'cancelled',code:'projection_cancelled'};}};
const widget=HomeConversation.mount(root,{sourceToken:'SIMULATED-core-projection',client});
root.querySelector('form').getBoundingClientRect=()=>({top:400,bottom:620,height:220});
async function show(response){projected=response;widget.update({sourceToken:`SIMULATED-core:${++sequence}`,response:config.bootstrap});root.querySelector('[data-hc="input"]').value='Explicit synthetic Core projection';root.querySelector('form').fire('submit');await new Promise(resolve=>setImmediate(resolve));}
try{
 await show(config.preview);
 assert.equal(root.querySelector('.hc-root').getAttribute('data-hc-sticky'),'false','a confirmation never sits behind a sticky composer even when it would otherwise fit');
 assert.equal(root.querySelectorAll('.hc-remove').length,1,'only the selected Core action is removed');
 assert.equal(root.querySelectorAll('.hc-retain').length,1,'Core new.rule retains its other action');
 assert(root.querySelector('[data-hc="approve"]')&&!root.querySelector('[data-hc="approve"]').disabled);
 assert(!root.textContent.includes('undefined'));
 projected=config.done;root.querySelector('[data-hc="approve"]').fire('click');await new Promise(resolve=>setImmediate(resolve));
 const removed=[...root.querySelectorAll('.hc-remove')].at(-1);assert(removed.textContent.includes('SIMULATED light')&&!removed.textContent.includes('ha:'),'approved result preserves the exact preview labels');
 const rollback=root.querySelector('[data-hc="rollback"]');assert(rollback,'the original Core result offers restore');
 projected=config.rolled;
 rollback.fire('click');await new Promise(resolve=>setImmediate(resolve));
 const sent=calls.find(r=>r.kind==='rollback');assert.equal(sent.plan_id,config.done.plan_id);assert.equal(sent.expected_context,config.done.context_token);
 assert.equal(root.querySelector('[data-hc="rollback"]'),null,'restored plan cannot offer the same restore again');
 assert([...root.querySelectorAll('pre')].at(-1).textContent.includes('rolled_back'),'the actual domain receipt remains available');
 assert([...root.querySelectorAll('.hc-retain')].at(-1).textContent.startsWith('恢复'),'restored actions are not presented as still removed');
}finally{widget.dispose();}
"""


@pytest.mark.asyncio
@pytest.mark.skipif(NODE is None,reason='Existing Node unavailable for offline DOM check')
async def test_actual_core_http_receipts_render_exact_diff_and_restore_control(tmp_path):
    async with live_app(tmp_path) as (app,client,calls,session,seed):
        shown=await preview(client,session)
        done=await turn(client,approval(session,shown))
        assert domain(done)['status']=='verified'
        rolled=await turn(client,request(session,operation='rules:live',kind='rollback',
            plan_id=shown['plan_id'],expected_context=done['context_token']))
        assert domain(rolled)['status']=='rolled_back' and not calls
        ready=(await client.post('/api/home-flow/context',json={'session_id':session})).json()
        config={'bootstrap':ready,'preview':shown,'done':done,'rolled':rolled}
        script=tmp_path/'core-widget.mjs';script.write_text(CORE_WIDGET)
        result=subprocess.run([NODE,str(script),str(Path(__file__).parents[1]/'homeplane/static/home-conversation.js'),json.dumps(config)],
            text=True,capture_output=True,timeout=5)
        assert result.returncode==0,result.stderr
