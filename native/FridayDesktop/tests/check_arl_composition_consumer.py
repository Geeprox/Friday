"""Actual desktop ARL composition; synthetic SDK/review/scope only, no inference."""
import asyncio
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('composition_fixture',Path(__file__).with_name('check_home_connection_consumer.py'))
base=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=base
spec.loader.exec_module(base)
fixture,backend=base.fixture,base.backend
from homeplane.arl_planning_priority import reviewed_priority_executor
from homeplane.arl_rule_planning import RuleAnalysisContext,RuleAnalysisRequest
from homeplane.rule_text import RuleTextChoice
from homeplane.query import HomeQueryAdoptionConfig
from homeplane.query_contracts import ReadScope,HomeQuery
from arl.identity import RegisteredPeerVerifier

class SyntheticPlanningPeer:
    """Runs the installed typed SDK against a generated provider receipt only."""
    def __init__(self):self.calls=[];self.submissions=0
    def connect(self,**_kwargs):self.calls.append('connect')
    def rpc(self,name,payload=None):
        from arl.friday_planning import FridayRemovalPlanner
        from types import SimpleNamespace
        self.calls.append(name)
        if name=='capabilities':
            return {'protocol':'arl/1','version':1,'tools':[FridayRemovalPlanner(object()).tool().descriptor()]}
        if name=='submit':
            import hashlib
            self.submissions+=1
            def generated(args,_context):
                text=json.dumps({'schema_version':1,'status':'unsupported','record_id':None,'action_id':None,'expected_fingerprint':None,'summary':'SYNTHETIC advisory abstention'})
                return {'schema_version':1,'format':'codex.appserver.text.v1','provider':'codex_app_server','model':args['model'],'request_sha256':hashlib.sha256(args['input'].encode()).hexdigest(),'text':text,'output_sha256':hashlib.sha256(text.encode()).hexdigest(),'simulated':False,'token_hard_cap_supported':False,'usage':{'tokens':None,'cost':None},'thread_id':'SYNTHETIC-thread','turn_id':'SYNTHETIC-turn'}
            context=SimpleNamespace(row={'owner':'friday','key':payload['key'],'id':'SYNTHETIC-job'},check_cancelled=lambda:None,remaining=lambda maximum=30:maximum)
            result=FridayRemovalPlanner(SimpleNamespace(run=generated)).run(payload['args'],context)
            return {'id':'SYNTHETIC-job','tool':'friday.rule-removal.plan.v1','state':'succeeded','result':result}
        if name=='cancel':return {}
        raise AssertionError('Unexpected synthetic SDK method')

class CompositionChecks(fixture.LocalConsumerChecks):
    async def compose(self,**kwargs):
        await self.client.aclose();await self.wrapper.close()
        self.wrapper=backend.DesktopService(**kwargs)
        self.client=fixture.httpx.AsyncClient(transport=fixture.httpx.ASGITransport(app=self.wrapper),base_url=fixture.ORIGIN,headers={'Origin':fixture.ORIGIN})
        await self.connect()
        return self.wrapper.inner

    async def test_actual_constructor_reuses_one_runtime_and_defaults_closed(self):
        inner=await self.connect()
        self.assertIs(inner.state.arl,self.wrapper.arl_runtime)
        self.assertIs(inner.state.rule_analysis.runtime,self.wrapper.arl_runtime)
        self.assertIs(inner.state.rule_text.runtime,self.wrapper.arl_runtime)
        self.assertIsNone(inner.state.rule_analysis.authorizer)
        self.assertIsNone(inner.state.query_adoption)
        rejected=await self.client.post('/api/rules/analysis/analyze',json={})
        self.assertEqual(rejected.status_code,409)
        self.assertEqual(rejected.json()['code'],'desktop_read_only')

    async def test_partial_policy_and_public_query_config_are_rejected(self):
        for kwargs in [{'arl_planning_authorizer':lambda _:True},{'arl_planning_executor':reviewed_priority_executor},{'query_adoption_config':{'all_home':True}}]:
            with self.assertRaises(ValueError):backend.DesktopService(**kwargs)

    async def test_runtime_ready_does_not_claim_codex_query_or_planning_ready(self):
        await self.connect()
        async def metadata(**kwargs):
            self.assertEqual(kwargs,{'start':False,'timeout':3})
            return {'ready':True,'providers':[{'name':'codex_app_server','availability':'disabled','models':[]}],'tools':[]}
        with patch.object(self.wrapper.arl_runtime,'status',metadata):
            response=await self.client.get('/desktop/capabilities')
        self.assertEqual(response.status_code,200)
        value=response.json()
        self.assertTrue(value['runtime_ready'])
        self.assertFalse(value['codex']['model_available'])
        self.assertEqual(value['planning']['status'],'policy_not_configured')
        self.assertFalse(value['planning']['action_authority'])
        self.assertFalse(value['query']['configured'])
        self.assertFalse(value['query']['cloud_egress'])
        self.assertFalse(value['model_inference_attempted'])
        denied=await self.client.get('/desktop/capabilities',headers={'Origin':'https://invalid.example'})
        self.assertEqual(denied.status_code,403)

    async def test_exact_review_approval_reaches_existing_sdk_once_without_effects(self):
        approved=[]
        def authorizer(review):
            from arl.friday_binding import FridayReviewBinding
            approved.append(FridayReviewBinding.from_review(review))
            return approved[-1].matches(review)
        inner=await self.compose(arl_planning_authorizer=authorizer,arl_planning_executor=reviewed_priority_executor)
        service=inner.state.rule_analysis
        self.assertIs(service.authorizer,authorizer)
        self.assertIs(service.planning_executor,reviewed_priority_executor)
        peer=SyntheticPlanningPeer()
        context=RuleAnalysisContext(fingerprint='a'*64,available=True,simulated=False,backend='home_core',execution_policy_configured=True,analysis_enabled=True)
        choice=RuleTextChoice(record_id='SYNTHETIC-record',action_id='SYNTHETIC-action',expected_fingerprint='b'*64,rule_label='SYNTHETIC-rule',action_label='SYNTHETIC-action',retained_actions=['SYNTHETIC-retained'])
        with patch.object(service,'context',lambda:context),patch.object(service,'_choices',lambda *_: [choice]),patch.object(self.wrapper.arl_runtime,'client_factory',lambda *_:peer):
            prepared=await self.client.post('/api/rules/analysis/reviews',json=RuleAnalysisRequest(text='SYNTHETIC finite advisory request',expected_context=context.fingerprint,idempotency_key='SYNTHETIC-key',expires_at=datetime.now(UTC)+timedelta(seconds=30)).model_dump(mode='json'))
            self.assertEqual(prepared.status_code,200,prepared.text)
            self.assertEqual(peer.calls,[])
            review=prepared.json();approval={'review_id':review['id'],'approval_digest':review['approval_digest']}
            wrong=await self.client.post('/api/rules/analysis/analyze',json={**approval,'approval_digest':'0'*64})
            self.assertEqual(wrong.json()['code'],'analysis_approval_mismatch');self.assertEqual(peer.calls,[])
            for body,headers,expected in [(approval,{'Origin':'https://invalid.example'},403),({},None,422)]:
                r=await self.client.post('/api/rules/analysis/analyze',json=body,headers=headers);self.assertEqual(r.status_code,expected)
            huge=await self.client.post('/api/rules/analysis/analyze',content=b'x'*8193);self.assertEqual(huge.status_code,413)
            result=await self.client.post('/api/rules/analysis/analyze',json=approval)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['status'],'unsupported')
            self.assertEqual(peer.submissions,1)
            self.assertEqual(len(approved),1)
            again=await self.client.post('/api/rules/analysis/analyze',json=approval)
            self.assertEqual(again.json(),result.json());self.assertEqual(peer.submissions,1)
            self.assertFalse(result.json()['real_write_attempted'])
            self.assertFalse(result.json()['physically_confirmed'])
        self.assertEqual((await self.client.post('/api/rules/core/plans/apply',json={})).status_code,409)
        self.assertEqual((await self.client.post('/api/actions/execute',json={})).status_code,409)

    async def test_planner_keeps_full_encoded_content_and_reports_sdk_budget_refusal(self):
        from friday.config import Settings
        from friday.providers import ARLPlanner, PLANNER_INSTRUCTIONS
        if base.APP:
            self.assertEqual(Path(sys.modules['friday.providers'].__file__).resolve(), base.APP/'Contents/Resources/product/app/friday/providers.py')
        from homeplane.arl import ARLError
        context={'selected':'SYNTHETIC-entity','content':'中\\"\n'*1000}
        schema={'type':'object','properties':{'selected':{'const':'SYNTHETIC-entity'}}}
        expected=PLANNER_INSTRUCTIONS+'\nSchema: '+json.dumps(schema)+'\nContext: '+json.dumps(context,ensure_ascii=False,allow_nan=False)
        class BudgetRuntime:
            def __init__(self,code):self.code=code;self.calls=[]
            async def complete(self,*args,**kwargs):
                self.calls.append((args,kwargs))
                raise ARLError(self.code)
        for code in ['invalid_arguments','model_budget_exceeded','invalid_frame']:
            runtime=BudgetRuntime(code)
            with self.assertRaisesRegex(ARLError,'arl_planner_context_contract_insufficient'):
                await ARLPlanner(Settings(codex_model='gpt-6.1-sol'),runtime).plan(context,schema)
            self.assertEqual(len(runtime.calls),1)
            self.assertEqual(runtime.calls[0][0][2],expected)
            self.assertGreater(len(expected.encode()),1024)
        denied=BudgetRuntime('cloud_not_enabled')
        with self.assertRaisesRegex(ARLError,'cloud_not_enabled'):
            await ARLPlanner(Settings(codex_model='gpt-6.1-sol'),denied).plan(context,schema)

    async def test_actual_sdk_checks_final_rpc_bytes_before_any_socket(self):
        import io
        from arl.client import Client
        from arl.contracts import Fault,encode
        home=self.data/'SYNTHETIC-sdk-home';home.mkdir(mode=0o700)
        client=Client(home,'friday')
        frames=[];sockets=[]
        class WireOnlySocket:
            def __enter__(self):sockets.append(self);return self
            def __exit__(self,*_args):return False
            def settimeout(self,_timeout):pass
            def connect(self,_path):pass
            def sendall(self,payload):frames.append(payload)
            def makefile(self,_mode):return io.BytesIO(encode({'protocol':'arl/1','ok':True,'result':{'SYNTHETIC':True}})+b'\n')
        def payload(text):return {'tool':'friday.rule-removal.plan.v1','args':{'model':'gpt-6.1-sol','request_json':text},'key':'SYNTHETIC-wire'}
        def frame(params):return encode({'protocol':'arl/1','app':'friday','method':'submit','params':params})+b'\n'
        overhead=len(frame(payload('')))
        exact=payload('x'*(16384-overhead))
        self.assertEqual(len(frame(exact)),16384)
        with patch('arl.client.socket.socket',lambda *_args:WireOnlySocket()):
            self.assertEqual(client.rpc('submit',exact),{'SYNTHETIC':True})
            self.assertEqual(frames[-1],frame(exact))
            count=len(sockets)
            for text in ['x'*(16385-overhead),'"'*9000,'中'*6000]:
                params=payload(text)
                self.assertGreater(len(frame(params)),16384)
                with self.assertRaisesRegex(Fault,'invalid_frame'):client.rpc('submit',params)
                self.assertEqual(len(sockets),count)
            intact=payload('中\\"\n'*100)
            client.rpc('submit',intact)
            self.assertEqual(json.loads(frames[-1])['params'],intact)
        # This isolates transport encoding, not planning input or model permission.
        self.assertEqual(len(sockets),2)

    async def test_existing_peer_scope_is_injected_without_registering_or_creating_grant(self):
        verifier=RegisteredPeerVerifier({'friday'})
        current={'registered':True}
        scope=ReadScope(subject='friday',revision='SYNTHETIC-revision',expires_at=datetime.now(UTC)+timedelta(seconds=30),boundary='registered_peer',all_home=True,permitted=True)
        with patch.object(verifier,'is_registered',lambda app,pid:current['registered'] and app=='friday' and pid==999999),patch.object(verifier,'register',side_effect=AssertionError('No registration authorized')):
            config=HomeQueryAdoptionConfig('friday',999999,verifier,lambda:scope)
            inner=await self.compose(query_adoption_config=config)
            adoption=inner.state.query_adoption
            self.assertIs(adoption.config,config)
            self.assertIs(adoption.queries,inner.state.queries)
            result=await adoption.read({'request_json':HomeQuery(kind='state',rooms=['SYNTHETIC-room']).model_dump_json()},deadline=time.monotonic()+1)
            self.assertEqual(result['permission_scope']['subject'],'friday')
            self.assertFalse(adoption.status()['cloud_egress'])
            current['registered']=False
            from arl.contracts import Fault
            with self.assertRaises(Fault):await adoption.read({'request_json':HomeQuery(kind='state',rooms=['SYNTHETIC-room']).model_dump_json()},deadline=time.monotonic()+1)
            await self.wrapper.close()
            self.assertTrue(adoption.closed)

if __name__=='__main__':
    names=['test_actual_constructor_reuses_one_runtime_and_defaults_closed','test_partial_policy_and_public_query_config_are_rejected','test_runtime_ready_does_not_claim_codex_query_or_planning_ready','test_exact_review_approval_reaches_existing_sdk_once_without_effects','test_existing_peer_scope_is_injected_without_registering_or_creating_grant','test_planner_keeps_full_encoded_content_and_reports_sdk_budget_refusal','test_actual_sdk_checks_final_rpc_bytes_before_any_socket']
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(CompositionChecks(name) for name in names))
    sys.exit(not result.wasSuccessful())
