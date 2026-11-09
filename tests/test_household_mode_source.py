"""Durable explicit intent over synthetic HA ingress and MockTransport effects."""
import asyncio
from datetime import UTC, datetime, timedelta
import time

import pytest

from homeplane.core_rules import CoreRuleRuntime
from homeplane.household_mode import HouseholdMode
from tests.test_homeplane_core_rules import cleanup, enable, event, fixture, proposal


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,bits', [('awake',(False,False)),('sleep',(True,False)),('manual',(False,True))])
async def test_explicit_choice_survives_runtime_recreation_without_replaying_work(tmp_path,mode,bits):
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    renewed=None
    try:
        source=HouseholdMode(core,runtime)
        assert source.status()['state']=='unconfigured' and not runtime.modes
        await source.maintain()
        assert not runtime.modes
        result=await source.select(mode,expected_context=source.status()['context'])
        assert result['mode_intent']['persistent'] and result['mode_intent']['mode']==mode
        renewed=CoreRuleRuntime(manager);renewed.start=lambda:None
        next_source=HouseholdMode(core,renewed)
        await next_source.maintain()
        assert tuple(renewed.modes[key].value for key in ('home.sleep','home.manual'))==bits
        assert next_source.valid() and not renewed.queue and not renewed.pending and not calls
        assert source.status()['context']!=next_source.status()['context']
    finally:
        if renewed is not None:await renewed.close()
        await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_newly_approved_rule_uses_current_intent_and_only_new_events(tmp_path):
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        source=HouseholdMode(core,runtime)
        await source.select('awake')
        old=event(core)
        await enable(manager,await proposal(core,manager))
        await source.maintain()
        await runtime.ingest(core.adapter,old)
        assert not calls
        await runtime.ingest(core.adapter,event(core))
        assert len(calls)==1 and runtime.status()['mode_window_current']
        assert source.status()['state']=='current'
    finally:await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_renewal_preserves_a_real_pending_delayed_effect(tmp_path):
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        target=next(e.id for e in (await core.snapshot()).entities if e.kind=='light')
        actions=[{'type':'delay','id':'pause','milliseconds':50},
                 {'type':'device.set','id':'light','target':target,'operation':'light.set','desired':{'power':'on'}}]
        await enable(manager,await proposal(core,manager,actions=actions))
        source=HouseholdMode(core,runtime);await source.select('awake')
        issued=runtime.modes['home.sleep'].issued_at
        await runtime.ingest(core.adapter,event(core))
        assert runtime.pending and not calls
        pending=dict(runtime.pending)
        runtime.mode_deadline=time.monotonic()+10
        await source.maintain()
        assert runtime.pending==pending and runtime.modes['home.sleep'].issued_at==issued
        await asyncio.sleep(.07)
        await runtime.tick()
        assert len(calls)==1
    finally:await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_manual_priority_survives_restart_and_never_changes_the_selected_mode(tmp_path):
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    renewed=None
    try:
        await enable(manager,await proposal(core,manager))
        source=HouseholdMode(core,runtime);await source.select('awake')
        await runtime.hold_for_manual(60)
        assert source.status()['mode']=='awake'
        renewed=CoreRuleRuntime(manager);renewed.start=lambda:None
        next_source=HouseholdMode(core,renewed);await next_source.maintain()
        assert renewed.manual_until>time.monotonic()+55
        await renewed.ingest(core.adapter,event(core))
        assert not calls and renewed.status()['manual_hold_active']
        assert next_source.status()['mode']=='awake'
    finally:
        if renewed is not None:await renewed.close()
        await cleanup(core,owner,runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['grant','disconnect','clear','expiry','restart_window'])
async def test_unknown_or_unqualified_intent_never_promotes_itself_to_awake(tmp_path,change):
    core,_,calls,_,path,grant,owner,manager,runtime=await fixture(tmp_path)
    renewed=None
    try:
        source=HouseholdMode(core,runtime)
        await source.select('sleep',ttl_seconds=30 if change in {'expiry','restart_window'} else None)
        if change=='grant':
            path.write_text(grant.model_copy(update={'expires_at':datetime.now(UTC)+timedelta(days=2)}).model_dump_json())
        elif change=='disconnect':
            core.adapter._stream.ready=False
        elif change=='clear':
            await source.select('unknown')
        elif change=='expiry':
            source.wall_now=lambda:datetime.now(UTC)+timedelta(minutes=1)
        elif change=='restart_window':
            renewed=CoreRuleRuntime(manager);renewed.start=lambda:None
            source=HouseholdMode(core,renewed)
        active=renewed or runtime
        await source.maintain()
        assert not active.modes and not source.valid() and not calls
        assert source.status()['state'] in {'changed','unavailable','unconfigured','expired'}
    finally:
        if renewed is not None:await renewed.close()
        await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_persist_or_automatically_restore_the_old_choice(tmp_path):
    core,_,calls,_,_,_,owner,manager,runtime=await fixture(tmp_path)
    try:
        source=HouseholdMode(core,runtime);await source.select('awake')
        original=source.status()['revision'];current=[True]
        await manager.lock.acquire()
        pending=asyncio.create_task(source.select('sleep',guard=lambda:current[0]))
        await asyncio.sleep(0)
        current[0]=False;manager.lock.release()
        with pytest.raises(ValueError,match='household_mode_changed'):await pending
        assert source.status()['revision']==original and not runtime.modes
        await runtime.hold_for_manual(60)
        await source.maintain()
        assert not runtime.modes and not calls
        await source.select('manual')
        assert source.valid() and runtime.modes['home.manual'].value
    finally:await cleanup(core,owner,runtime)


@pytest.mark.asyncio
async def test_real_console_mode_and_pause_use_the_same_durable_owner(tmp_path):
    from tests.test_homeplane_home_flow import application,bootstrap,domain,request,turn
    core,_,calls,_,path,_,owner,_,runtime=await fixture(tmp_path)
    adapter=core.adapter
    await runtime.close();owner.close();core.actions.close();core.store.close()
    async with application(tmp_path,adapter=adapter,authority_path=path) as (app,client):
        ready=await bootstrap(client);session=ready['session_id']
        changed=await turn(client,request(session,operation='mode:live',text='进入睡眠模式'))
        assert changed['phase']=='result',changed
        effective=domain(changed)['effective']
        assert effective['mode']=='sleep' and effective['persistent'] is True
        assert '保持到你更改' in changed['caption']
        report=(await client.get('/api/home/automatic-status')).json()
        assert report['mode_source']['mode']=='sleep' and report['mode_source']['persistent']
        paused=await turn(client,request(session,operation='mode:pause',text='暂停自动化'))
        assert domain(paused)['pause']['status']=='paused'
        assert not app.state.home_experience.preferences['automatic_running']
        assert app.state.core_rule_runtime.execution_enabled is False and not calls


@pytest.mark.asyncio
async def test_public_mode_route_fences_stale_choice_and_long_rest_needs_confirmation(tmp_path):
    from tests.test_homeplane_home_flow import application,bootstrap,domain,request,turn
    core,_,calls,_,path,_,owner,_,runtime=await fixture(tmp_path)
    adapter=core.adapter
    await runtime.close();owner.close();core.actions.close();core.store.close()
    async with application(tmp_path,adapter=adapter,authority_path=path) as (app,client):
        source=app.state.home_experience.household_mode
        original=(await client.get('/api/home/automatic-status')).json()['mode_source']
        selected=await client.post('/api/home/mode',json={'mode':'awake','expected_context':original['context']})
        assert selected.status_code==200
        stale=await client.post('/api/home/mode',json={'mode':'manual','expected_context':original['context']})
        assert stale.status_code==409 and source.status()['mode']=='awake'
        # The public persistent-mode route never silently accepts a finite window.
        bad=await client.post('/api/home/mode',json={'mode':'sleep','expected_context':original['context'],'ttl_seconds':60})
        assert bad.status_code==422
        session=(await bootstrap(client))['session_id']
        offered=await turn(client,request(session,operation='rest:choice',text='我要睡觉一整晚'))
        assert offered['phase']=='clarify' and offered['choices']
        assert source.status()['mode']=='awake'
        chosen=await turn(client,request(session,operation='rest:choice',text='',
            candidate_id=offered['choices'][0]['id'],expected_context=offered['context_token']))
        assert chosen['phase']=='result',chosen
        effective=domain(chosen)['effective']
        assert effective['mode']=='sleep' and effective['persistent']
        assert effective['remaining_seconds'] is None and effective['valid_until'] is None
        assert not calls
