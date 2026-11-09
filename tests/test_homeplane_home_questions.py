"""Original Console→scoped query; model/HA inputs are explicitly synthetic."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from homeplane.api import create_app
from homeplane.arl import current_model_task
from homeplane.simulator import Simulator
from tests.test_homeplane_home_flow import bootstrap, domain, request, turn
from tests.test_household_timezone_wiring import OfflineARL


def model_result(selection):
    return dict(text=json.dumps(selection, ensure_ascii=False), simulated=False,
        native_acknowledgements_verified=True, device_authority=False,
        application_admission=dict(scenario_id='home_planning', package_id='high'))


class QuestionRuntime(OfflineARL):
    def __init__(self, selection=None):
        self.selection = selection or dict(kind='state', rooms=['客厅'], capabilities=['temperature'])
        self.calls = []

    async def complete_scenario(self, scenario, prompt, **arguments):
        task = current_model_task()
        self.calls.append((scenario, prompt, arguments, task))
        assert task.task_kind == 'household.plan' and task.requested_class == 'interactive'
        assert task.current() is True
        return model_result(self.selection)


def make_adapter(tmp_path, *, fixture=None, scenario=None):
    # Explicit Chinese labels in this synthetic input, never production aliases.
    fixture=copy.deepcopy(fixture or Simulator().fixture)
    for entity in fixture['entities']:
        entity['room']={'living-room':'客厅','hall':'玄关'}.get(entity.get('room'),entity.get('room'))
        entity['name']={'Living thermometer':'客厅温度计','Living air conditioner':'客厅空调',
                        'Hall light':'玄关灯'}.get(entity['name'],entity['name'])
    source=tmp_path/'question-simulated-input.json'
    source.write_text(json.dumps(fixture,ensure_ascii=False))
    adapter=Simulator(source)
    if scenario:adapter.set_scenario(scenario)
    return adapter


@asynccontextmanager
async def application(tmp_path, *, runtime=None, adapter=None):
    root=tmp_path.resolve()
    app=create_app(adapter=adapter or make_adapter(tmp_path), db_path=root/'questions.sqlite', settings_path=root/'settings.json',
                   arl_runtime=runtime or OfflineARL(), background_enabled=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='http://testserver', headers={'Origin':'http://testserver'}) as client:
            yield app, client


async def ask(client, text, *, operation='question:one'):
    ready=await bootstrap(client)
    report=await turn(client, request(ready['session_id'], operation=operation, text=text))
    return ready, report


@pytest.mark.asyncio
@pytest.mark.parametrize('text',['客厅温度多少','请问客厅现在多少度？','查询客厅温度'])
async def test_local_question_uses_original_query_reading_without_model_or_action(tmp_path,text):
    runtime=QuestionRuntime()
    async with application(tmp_path,runtime=runtime) as (app,client):
        _,report=await ask(client,text)
        assert report['phase']=='result',json.dumps(report,ensure_ascii=False)
        result=domain(report)['query']
        assert result['kind']=='state' and result['simulated'] is True
        assert result['items'] and {i['capability'] for i in result['items']}=={'temperature'}
        assert {i['entity_id']:i['value'] for i in result['items']}=={
            'sensor.living.temperature':24.5,'climate.living':26.5}
        assert result['execution_authority'] is False
        assert app.state.home_flow.questions.ports.queries is app.state.queries
        assert not runtime.calls
        assert app.state.core.store.connection.execute('SELECT count(*) FROM light_actions').fetchone()[0]==0


@pytest.mark.asyncio
async def test_arl_explains_query_then_original_reader_supplies_all_values(tmp_path):
    runtime=QuestionRuntime()
    async with application(tmp_path,runtime=runtime) as (app,client):
        ready,report=await ask(client,'客厅的温度能查一下吗？')
        assert report['phase']=='result',report
        result=domain(report)
        assert result['interpretation']==dict(provider='arl_application_scenario',read_only=True)
        assert next(i for i in result['query']['items'] if i['entity_id']=='sensor.living.temperature')['value']==24.5
        assert len(runtime.calls)==1
        scenario,prompt,args,_=runtime.calls[0]
        assert scenario=='home_planning' and len(args['request_id'])==64
        context=json.loads(prompt.split('\n',1)[1])
        assert set(context)=={'question','catalogue'}
        assert set(context['catalogue'])=={'rooms','capabilities','device_kinds'}
        assert '24.5' not in prompt and 'scope' not in context
        refreshed=await turn(client,request(ready['session_id'],operation='question:one',
            expected_context=report['context_token'],candidate_id=result['refresh_ref']))
        assert refreshed['phase']=='result' and len(runtime.calls)==1
        assert app.state.core.store.connection.execute('SELECT count(*) FROM light_actions').fetchone()[0]==0


@pytest.mark.asyncio
async def test_missing_room_selection_is_exact_and_never_runs_the_model_twice(tmp_path):
    runtime=QuestionRuntime()
    async with application(tmp_path,runtime=runtime) as (_,client):
        ready,report=await ask(client,'温度多少？')
        assert report['phase']=='clarify' and report['code']=='question_room_selection_required',report
        choice=next(c for c in report['choices'] if c['label']=='客厅')
        selected=await turn(client, request(ready['session_id'], operation='question:one',
            expected_context=report['context_token'],candidate_id=choice['id']))
        assert selected['phase']=='result',selected
        assert {i['room'] for i in domain(selected)['query']['items']}=={'客厅'}
        refreshed=await turn(client,request(ready['session_id'],operation='question:one',
            expected_context=selected['context_token'],candidate_id=domain(selected)['refresh_ref']))
        assert refreshed['phase']=='result' and not refreshed['choices']
        assert domain(refreshed)['query']['permission_scope']['requested_rooms']==['客厅']
        assert not runtime.calls
        stale=await turn(client,request(ready['session_id'],operation='question:one',
            expected_context=report['context_token'],candidate_id=choice['id']))
        assert stale['code']=='flow_context_changed'


@pytest.mark.asyncio
async def test_expired_measurement_is_not_replaced_with_a_model_guess(tmp_path):
    adapter=make_adapter(tmp_path,scenario='stale-temperature')
    async with application(tmp_path,adapter=adapter) as (_,client):
        _,report=await ask(client,'客厅温度多少')
        assert report['phase']=='result',report
        items=domain(report)['query']['items']
        stale=next(i for i in items if i['entity_id']=='sensor.living.temperature')
        assert stale['value'] is None and stale['quality']!='healthy'
        fresh=next(i for i in items if i['entity_id']=='climate.living')
        assert fresh['value']==26.5 and fresh['quality']=='healthy'


@pytest.mark.asyncio
async def test_device_question_returns_original_inventory_and_never_implies_online(tmp_path):
    async with application(tmp_path) as (_,client):
        _,report=await ask(client,'客厅有哪些设备')
        assert report['phase']=='result',report
        result=domain(report)['query']
        assert result['kind']=='devices' and result['items']
        assert all(i['kind']=='device' and i['room']=='客厅' and 'online' not in i for i in result['items'])


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[
    {'simulated':True},{'native_acknowledgements_verified':False},
    {'device_authority':True}, {'application_admission':{'scenario_id':'voice_intent','package_id':'low'}},
    {'text':'{"kind":"state","rooms":["不存在的房间"]}'},
    {'text':'{"kind":"history","rooms":["客厅"]}'},
    {'text':'{"kind":"state","rooms":["客厅"],"value":99}'},
    {'text':'{"kind":"state","kind":"devices","rooms":["客厅"]}'},
    {'text':'{"kind":"state","whole_home":true}'},
])
async def test_unverified_or_expanded_model_result_cannot_reach_query_or_actions(tmp_path,monkeypatch,change):
    runtime=QuestionRuntime()
    async def complete(*args,**kwargs):return {**model_result(runtime.selection),**change}
    runtime.complete_scenario=complete
    async with application(tmp_path,runtime=runtime) as (app,client):
        reads=[]
        original=app.state.queries.read
        async def read(*args,**kwargs):
            reads.append(args)
            return await original(*args,**kwargs)
        monkeypatch.setattr(app.state.queries,'read',read)
        _,report=await ask(client,'客厅的温度能查一下吗？')
        assert report['phase'] in {'unavailable','clarify'},report
        assert not reads
        assert app.state.core.store.connection.execute('SELECT count(*) FROM light_actions').fetchone()[0]==0


@pytest.mark.asyncio
async def test_pending_interpretation_leaves_app_responsive_and_cancel_fences_before_join(tmp_path,monkeypatch):
    runtime=QuestionRuntime()
    entered,release=asyncio.Event(),asyncio.Event()
    intent=[]
    async def noncooperative(*args,**kwargs):
        intent.append(current_model_task())
        entered.set()
        while not release.is_set():
            try:await release.wait()
            except asyncio.CancelledError:pass
        return model_result(runtime.selection)
    runtime.complete_scenario=noncooperative
    async with application(tmp_path,runtime=runtime) as (app,client):
        ready=await bootstrap(client)
        reads=[]
        original=app.state.queries.read
        async def read(*args,**kwargs):reads.append(args);return await original(*args,**kwargs)
        monkeypatch.setattr(app.state.queries,'read',read)
        sending=asyncio.create_task(turn(client,request(ready['session_id'],operation='question:held',
            text='客厅的温度能查一下吗？')))
        await asyncio.wait_for(entered.wait(),2)
        # This takes the original global app lock; a model cannot occupy it.
        newer=await asyncio.wait_for(bootstrap(client),1)
        assert newer['session_id']!=ready['session_id']
        cancelling=asyncio.create_task(client.post('/api/home-flow/cancel',json=request(
            ready['session_id'],operation='question:held',kind='cancel')))
        for _ in range(100):
            if not intent[0].current():break
            await asyncio.sleep(.001)
        assert intent[0].current() is False
        assert not cancelling.done(), 'Cancellation owns and joins the still-running child'
        release.set()
        report=await asyncio.wait_for(sending,2)
        stopped=await asyncio.wait_for(cancelling,2)
        assert report['phase']=='cancelled' and stopped.json()['phase']=='cancelled'
        assert not reads
        assert not app.state.home_flow.questions.tasks


@pytest.mark.asyncio
async def test_source_switch_discards_noncooperative_interpretation_before_query(tmp_path,monkeypatch):
    runtime=QuestionRuntime()
    original_complete=runtime.complete_scenario
    async with application(tmp_path,runtime=runtime) as (app,client):
        async def switching(*args,**kwargs):
            result=await original_complete(*args,**kwargs)
            app.state.home_flow.invalidate()
            return result
        runtime.complete_scenario=switching
        reads=[]
        async def forbidden(*args,**kwargs):reads.append(args);raise AssertionError('stale query')
        monkeypatch.setattr(app.state.queries,'read',forbidden)
        _,report=await ask(client,'客厅的温度能查一下吗？')
        assert report['phase']=='cancelled',report
        assert not reads


@pytest.mark.asyncio
async def test_pagination_uses_original_cursor_exact_scope_and_no_model_replay(tmp_path):
    fixture=copy.deepcopy(Simulator().fixture)
    prototype=next(e for e in fixture['entities'] if e['id']=='sensor.living.temperature')
    for index in range(25):
        value=copy.deepcopy(prototype)
        value.update(id=f'sensor.qa.temperature_{index}',name=f'合成温度计{index}',physical_source_id=f'sim:qa-temperature-{index}')
        fixture['entities'].append(value)
    runtime=QuestionRuntime()
    async with application(tmp_path,adapter=make_adapter(tmp_path,fixture=fixture),runtime=runtime) as (_,client):
        ready,first=await ask(client,'客厅温度多少')
        assert first['phase']=='result' and first['choices'],first
        seen={i['id'] for i in domain(first)['query']['items']}
        second=await turn(client,request(ready['session_id'],operation='question:one',
            expected_context=first['context_token'],candidate_id=first['choices'][0]['id']))
        assert second['phase']=='result',second
        more={i['id'] for i in domain(second)['query']['items']}
        assert not seen & more and len(seen|more)==27
        assert not second['choices'] and not runtime.calls


@pytest.mark.asyncio
async def test_cancelling_one_console_read_preserves_a_peers_original_cursor(tmp_path):
    fixture=copy.deepcopy(Simulator().fixture)
    prototype=next(e for e in fixture['entities'] if e['id']=='sensor.living.temperature')
    for index in range(25):
        value=copy.deepcopy(prototype)
        value.update(id=f'sensor.peer.temperature_{index}',physical_source_id=f'sim:peer-temperature-{index}')
        fixture['entities'].append(value)
    async with application(tmp_path,adapter=make_adapter(tmp_path,fixture=fixture)) as (app,client):
        first,a=await ask(client,'客厅温度多少',operation='question:a')
        second,b=await ask(client,'客厅温度多少',operation='question:b')
        assert a['choices'] and b['choices'] and len(app.state.queries.pages)==2
        assert domain(a)['query']['permission_scope']['subject']=='console'
        assert domain(b)['query']['permission_scope']['subject']=='console'
        stopped=await client.post('/api/home-flow/cancel',json=request(first['session_id'],
            operation='question:a',kind='cancel',expected_context=a['context_token']))
        assert stopped.json()['phase']=='cancelled' and len(app.state.queries.pages)==1
        continued=await turn(client,request(second['session_id'],operation='question:b',
            expected_context=b['context_token'],candidate_id=b['choices'][0]['id']))
        assert continued['phase']=='result' and domain(continued)['query']['items']


@pytest.mark.asyncio
async def test_same_consumer_reads_original_ha_adapter_without_a_device_write(tmp_path):
    from tests.test_homeplane_core_rule_conversation import live_app
    async with live_app(tmp_path) as (app,client,calls,session,_):
        before=len(calls)
        snapshot=await app.state.core.snapshot()
        room=next(e.room for e in snapshot.entities if e.room)
        result=await turn(client,request(session,operation='question:ha',text=f'{room}设备状态怎么样'))
        assert result['phase']=='result',result
        wire=domain(result)['query']
        assert wire['items'] and wire['simulated'] is False
        assert wire['execution_authority'] is False and wire['permission_scope']['subject']=='console'
        assert len(calls)==before
        assert app.state.home_flow.questions.ports.queries.core.adapter is app.state.core.adapter


@pytest.mark.asyncio
@pytest.mark.parametrize('stale,select_room',[(False,False),(True,False),(False,True)])
async def test_component_consumes_original_http_readings_and_refreshes_only_the_query(tmp_path,stale,select_room):
    from tests.test_homeplane_home_conversation_widget import HARNESS,NODE
    if NODE is None:pytest.skip('Existing Node required for actual component contract')
    marker='\ntry {\n'
    prefix=HARNESS[:HARNESS.index(marker)]
    script=tmp_path/'question-component.mjs'
    script.write_text(prefix+r'''
try {
  widget.update({response: config.bootstrap[0]});
  submit(config.phrase);
  if(config.select_room){await until(()=>actionable('choice').length===2);actionable('choice').find(n=>n.textContent==='客厅').fire('click');}
  await until(() => !!root().querySelector('.hc-readings'));
  assert(!root().querySelector('img'), 'registry names are text, never HTML');
  assert.equal(root().querySelector('.hc-reading-simulated').textContent,'示例数据','Simulation remains visible within the answer when the page header scrolls away');
  const readings=root().querySelector('.hc-readings');
  assert(readings.textContent.includes(config.malicious));
  assert(readings.textContent.includes('26.5'));
  assert.equal(readings.textContent.includes('24.5'), !config.stale);
  assert.equal(readings.textContent.includes('暂不可用'), config.stale);
  assert(!actionable('approve').length);
  const first=JSON.parse(currentPre());
  const firstQuery=first.report.query;
  actionable('refresh-readings')[0].fire('click');
  await until(() => requests.filter(r=>r.kind==='turn').length===(config.select_room?3:2) && actionable('refresh-readings').length===1);
  const latest=JSON.parse(currentPre());
  const refreshed=latest.report.query;
  assert.equal(root().querySelectorAll('.hc-readings').length,1,'Refresh replaces the prior reading instead of duplicating the question');
  assert.notEqual(firstQuery.snapshot_ref,refreshed.snapshot_ref);
  const turns=requests.filter(r=>r.kind==='turn');
  assert.equal(turns[0].operation_id,turns.at(-1).operation_id);
  assert.notEqual(turns[0].idempotency_key,turns.at(-1).idempotency_key);
  assert(turns.at(-1).candidate_id.startsWith('refresh:'));
  assert.equal(turns.at(-1).text,undefined);
  assert.equal(turns.at(-1).lane,'query');
  assert.deepEqual(refreshed.permission_scope.requested_rooms,firstQuery.permission_scope.requested_rooms);
  assert(!requests.some(r=>['approve','rollback','reconcile','playback'].includes(r.kind)));
  widget.dispose();
  await until(() => outstanding.size === 0);
  emit({type:'done',checked:['original_http_readings','per_source_quality','safe_registry_labels','original_typed_query_refresh','no_action_or_media_request'],html_writes:writes});
} catch(error) {
  widget.dispose();emit({type:'failure',error:error.stack});process.exitCode=1;
} finally { input.close(); }
''')
    fixture=copy.deepcopy(Simulator().fixture)
    malicious='<img src=x onerror="SYNTHETIC-registry-label">'
    next(e for e in fixture['entities'] if e['id']=='sensor.living.temperature')['name']=malicious
    adapter=make_adapter(tmp_path,fixture=fixture,scenario='stale-temperature' if stale else 'baseline')
    async with application(tmp_path,adapter=adapter) as (app,client):
        ready=await bootstrap(client)
        config=dict(bootstrap=[ready],phrase='温度多少' if select_room else '客厅温度多少',stale=stale,malicious=malicious,select_room=select_room)
        process=await asyncio.create_subprocess_exec(NODE,str(script),
            str(Path(__file__).parents[1]/'homeplane/static/home-conversation.js'),json.dumps(config),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        result=None;turns=0
        try:
            async with asyncio.timeout(12):
                while line:=await process.stdout.readline():
                    message=json.loads(line)
                    if message['type']=='failure':pytest.fail(message['error'])
                    if message['type']=='done':result=message;break
                    body=message['request']
                    if body['kind']=='turn':
                        turns+=1
                        if body.get('candidate_id','').startswith('refresh:'):
                            adapter.set_scenario('stale-temperature' if stale else 'baseline')
                    response=await client.post('/api/home-flow/cancel' if body['kind']=='cancel'
                        else '/api/home-flow/turn',json=body)
                    assert response.status_code==200,response.text
                    process.stdin.write((json.dumps({'bridge_id':message['bridge_id'],'response':response.json()})+'\n').encode())
                    try:
                        await process.stdin.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        pytest.fail('Component process closed during response: '+json.dumps({
                            'request':body,'response_code':response.json().get('code'),
                            'remaining':(await process.stdout.read()).decode(),
                            'stderr':(await process.stderr.read()).decode()},ensure_ascii=False))
                assert result is not None
                assert await process.wait()==0,(await process.stderr.read()).decode()
            assert result['html_writes']==0
            assert app.state.core.store.connection.execute('SELECT count(*) FROM light_actions').fetchone()[0]==0
        finally:
            if process.returncode is None:
                process.terminate();await process.wait()


@pytest.mark.asyncio
async def test_query_selection_never_accepts_a_public_write_or_permission(tmp_path):
    async with application(tmp_path) as (app,client):
        ready,report=await ask(client,'客厅温度多少')
        injection=await client.post('/api/home-flow/turn',json=request(ready['session_id'],
            lane='query',text='客厅温度多少',authority={'all_home':True}))
        assert injection.status_code==422
        result=await turn(client,request(ready['session_id'],operation='question:one',kind='approve',
            expected_context=report['context_token'],plan_id='fake',approval_digest='a'*64))
        assert result['phase'] in {'unavailable','refused'}
        assert app.state.core.store.connection.execute('SELECT count(*) FROM light_actions').fetchone()[0]==0
