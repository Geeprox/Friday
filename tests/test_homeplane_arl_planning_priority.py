"""Actual ASGI review/analyze to a generated ARL host; never run inference."""
import asyncio
import copy
import json
import os
import struct
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
pytest.importorskip('arl', reason='Optional shared ARL SDK')
from arl import PROTOCOL
from arl.client import Client
from arl.codex_server import CodexTextAdapter
from arl.contracts import AppPolicy, Fault
from arl.friday_planning import FridayRemovalPlanner, NAME
from arl.friday_profile import PlanningAdmission
from arl.identity import RegisteredPeerVerifier
from arl.priority import AppProfilePolicy, ResourcePolicy, TaskPolicy, release_lease
from arl.priority_host import PriorityHostConfig, RegisteredRouteProducer
from arl.providers import MockProvider
from arl.service import JobContext, Runtime
from arl.storage import Store
from homeplane.api import create_app
from homeplane.arl import ARLRuntime
from homeplane.arl_planning_priority import reviewed_priority_executor
from homeplane.arl_rule_planning import RuleAnalysisReview
from homeplane.simulator import Simulator


class Connection:
    def __init__(self, pid):
        self.pid = pid

    def getsockopt(self, level, option, size):
        assert (level, option, size) == (0, 2, 4)
        return struct.pack('i', self.pid)


class NoExternalTools:
    transport = None

    def tools(self):
        return []


class MemorySession:
    """Generated no-turn preflight and job acknowledgement; no external process."""
    def __init__(self, fixture, binary, workspace, **kwargs):
        self.fixture = fixture
        self.model = self.thread_id = self.turn_id = None
        self.process = SimpleNamespace(returncode=0)
        fixture.sessions_created += 1

    def initialize(self):
        return None

    def send(self, method, params):
        if method == 'thread/start':
            self.model = params['model']
            return 'generated-thread-request'
        if method == 'turn/start':
            self.fixture.stopped_turn_boundaries += 1
            raise Fault('synthetic_stopped_before_inference')
        raise AssertionError(method)

    def response(self, request):
        assert request == 'generated-thread-request'
        if self.fixture.preflighting:
            self.fixture.preflight_ack_count += 1
        else:
            if self.fixture.on_job_ack:
                self.fixture.on_job_ack()
            self.fixture.job_ack_count += 1
        return {'model': self.model, 'thread': {'id': 'generated-thread', 'ephemeral': True}}

    def cancel_turn(self):
        raise AssertionError('No inference turn was started')

    def close(self):
        self.fixture.sessions_closed += 1


class DirectClient(Client):
    """Replace only IPC; preserve real SDK and registered-identity dispatch."""
    def __init__(self, fixture):
        super().__init__(fixture.host.home, 'friday')
        self.fixture = fixture
        self.calls = []
        self.first_receipt = None

    def connect(self, timeout=10):
        assert 0 < timeout <= 2
        self.calls.append(('connect', timeout))
        return {'ready': self.fixture.host.ready.is_set()}

    def rpc(self, method, params=None):
        f = self.fixture
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        owner = f.verifier(Connection(f.pid), self.app)
        if method == 'get' and f.hold_get:
            f.get_entered.set()
            assert f.release_get.wait(3), 'Bounded fixture get must be released'
        elif method == 'get' and not f.driven:
            f.drive_once()
        result = f.host.dispatch({'protocol': PROTOCOL, 'app': self.app,
            'method': method, 'params': params}, verified_app=owner)
        if method == 'priority.submit.v1':
            self.first_receipt = copy.deepcopy(result)
        if method == 'priority.negotiate.v2' and f.on_negotiate:
            f.on_negotiate()
        return result


class Composition:
    @classmethod
    async def create(cls, home, monkeypatch):
        f = cls()
        f.home = home
        f.short_home = tempfile.TemporaryDirectory(prefix='fprio-', dir='/tmp')
        f.host_location = Path(f.short_home.name)
        f.pid = 61002
        f.binary = home / 'generated-non-executable-transport'
        f.binary.write_bytes(b'generated transport fixture bytes, never executed\n')
        f.identity = {'uid': os.geteuid(), 'birth': (2, 3), 'executable': str(f.binary.resolve())}
        monkeypatch.setattr('arl.identity.process_identity', lambda pid: dict(f.identity))

        def generated_mount_snapshot(argv, **kwargs):
            assert argv == ['/sbin/mount']
            assert kwargs == {'capture_output': True, 'text': True, 'check': True}
            return SimpleNamespace(stdout='/dev/generated-fixture on / (apfs, local)\n')

        monkeypatch.setattr('homeplane.store.subprocess', SimpleNamespace(run=generated_mount_snapshot))
        f.verifier = RegisteredPeerVerifier({'friday'})
        f.verifier.register('friday', f.pid, f.binary)
        f.generation = f.verifier.registration_generation('friday', f.pid)
        f.original_subject = f.verifier.registration_subject('friday')
        f.sessions_created = f.sessions_closed = f.preflight_ack_count = f.job_ack_count = 0
        f.stopped_turn_boundaries = 0
        f.preflighting = False
        f.on_negotiate = f.on_job_ack = None
        f.hold_get = f.driven = False
        f.get_entered, f.release_get = threading.Event(), threading.Event()
        f.row = f.adapter_failure = f.lease_after_adapter_return = None
        f.replacement_generation = f.old_cleanup_revoked_replacement = None
        holder, permit = {}, {}
        f.consumer_runtime = ARLRuntime(lambda: f.host_location,
            client_factory=lambda home, start: holder['client'])

        def authorize(review):
            return (review.id, review.approval_digest, review.request) == (
                permit.get('id'), permit.get('digest'), permit.get('request'))

        f.app = create_app(adapter=Simulator(), db_path=home/'generated-App.sqlite',
            settings_path=home/'generated-settings.json', arl_runtime=f.consumer_runtime,
            arl_planning_authorizer=authorize, arl_planning_executor=reviewed_priority_executor)
        assert f.app.state.rule_analysis.planning_executor is reviewed_priority_executor
        f.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url='http://testserver',
            headers={'Origin':'http://testserver'})
        context = await f.http.get('/api/rules/analysis/context')
        assert context.status_code == 200
        response = await f.http.post('/api/rules/analysis/reviews', json={
            'text': '以后我回家可以不开空调了', 'expected_context': context.json()['fingerprint'],
            'idempotency_key': 'generated-'+str(uuid4()),
            'expires_at': (datetime.now(UTC)+timedelta(seconds=30)).isoformat()})
        assert response.status_code == 200, response.text
        f.review = RuleAnalysisReview.model_validate(response.json())
        permit.update(id=f.review.id, digest=f.review.approval_digest, request=copy.deepcopy(f.review.request))
        f.approval = {'review_id':f.review.id, 'approval_digest':f.review.approval_digest}
        f.before_records = f.app.state.rules.backend.records()
        f.admission = PlanningAdmission(f.review.request)
        f.adapter = CodexTextAdapter(binary=f.binary, enabled=True, test_command=(str(f.binary),))
        f.planner = FridayRemovalPlanner(f.adapter)
        policy = AppPolicy(frozenset({NAME, 'runtime.ping'}),
            methods=frozenset({'health','ready','capabilities','providers','submit','get','cancel'}),
            max_pending=1, max_calls=1, max_deadline_ms=30000)
        f.host = Runtime(f.host_location, policies={'friday':policy}, extra_tools=(f.planner.tool(),),
            identity_verifier=f.verifier, admission=f.admission, providers={'mock':MockProvider()},
            apple=NoExternalTools(), travel=NoExternalTools(), execute_jobs=True)
        f.store = Store(home/'generated-ARL.sqlite')
        f.host.store = f.store
        f.store.publication_admission = f.admission
        config = PriorityHostConfig((ResourcePolicy('generated-model-pool', slots=1, realtime_reserved=0,
            background_start_limit_ms=12000, capacity_evidence='generated_source_fixture_only'),),
            (TaskPolicy(NAME, 'friday.reviewed.plan', 'generated-model-pool', owners=frozenset({'friday'}),
                maximum_class='interactive', estimate_floor_ms=5, execution_budget_ms=12000),),
            {'friday':AppProfilePolicy(frozenset({'background','interactive'}),
                burst_calls=10, max_deadline_ms=30000)}, 10000, True)
        f.producer = RegisteredRouteProducer(f.host, f.verifier, config)
        f.producer.register_planner(f.planner, task_kind='friday.reviewed.plan',
            resource_kind='generated-model-pool', backend='codex_app_server')
        f.host.bind_priority()
        f.host.ready.set()
        f.client = DirectClient(f)
        holder['client'] = f.client
        monkeypatch.setattr('arl.codex_server.Session', lambda *a, **kw: MemorySession(f, *a, **kw))
        assert f.producer.snapshot('friday') == ()
        f.preflighting = True
        try:
            f.producer.probe_planner('friday', f.review.model)
        finally:
            f.preflighting = False
        assert f.preflight_ack_count == 1 and f.stopped_turn_boundaries == 0
        assert len(f.producer.snapshot('friday')) == 1
        assert f.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        return f

    async def analyze(self):
        response = await self.http.post('/api/rules/analysis/analyze', json=self.approval)
        assert response.status_code == 200, response.text
        return response.json()

    def drive_once(self):
        self.driven = True
        self.row = self.store.claim()
        assert self.row and self.row['id'] == self.client.first_receipt['id']
        assert self.row['key'] == self.review.id and self.row['owner'] == 'friday'
        assert self.row['priority'] == 'interactive'
        assert self.store.db.execute('SELECT COUNT(*) FROM resource_leases').fetchone()[0] == 1
        try:
            self.planner.run(json.loads(self.row['args']), JobContext(self.host, self.row))
        except Fault as exc:
            self.adapter_failure = exc.code
        else:
            raise AssertionError('No successful model result may be manufactured')
        assert self.sessions_closed == self.sessions_created
        self.lease_after_adapter_return = self.store.db.execute('SELECT COUNT(*) FROM resource_leases').fetchone()[0]
        assert self.lease_after_adapter_return == 1
        assert self.store.finish(self.row, 'needs_review', error=self.adapter_failure)
        assert release_lease(self.store, self.row)

    def evidence(self, case, result=None):
        first = self.client.first_receipt
        job = dict(self.store.row('friday', first['id'])) if first else None
        return {'case':case, 'qualification':'generated actual ASGI typed first-v2 admission/claim; no inference',
            'ASGI_routes':['/api/rules/analysis/context','/api/rules/analysis/reviews','/api/rules/analysis/analyze'],
            'methods':[m for m,_ in self.client.calls], 'review_id':self.review.id,
            'first_job_id':first['id'] if first else None, 'first_scheduling':first.get('scheduling') if first else None,
            'job_key':job['key'] if job else None, 'job_state':job['state'] if job else None,
            'job_error':job['error'] if job else None, 'job_priority':job['priority'] if job else None,
            'job_attempts':job['attempts'] if job else 0,
            'stored_schedule':json.loads(job['schedule_v1']) if job else None,
            'jobs':self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],
            'preflight_ack_count':self.preflight_ack_count,'job_ack_count':self.job_ack_count,
            'stopped_in_memory_turn_boundaries':self.stopped_turn_boundaries,
            'lease_after_adapter_return':self.lease_after_adapter_return,
            'leases_after_release':self.store.db.execute('SELECT COUNT(*) FROM resource_leases').fetchone()[0],
            'producer_attempts':len(self.producer.attempts),'consumer_busy':self.consumer_runtime._busy.locked(),
            'App_result':result, 'model_result':None,'rules_unchanged':self.app.state.rules.backend.records()==self.before_records,
            'original_generation':self.generation,'replacement_generation':self.replacement_generation,
            'old_cleanup_revoked_replacement':self.old_cleanup_revoked_replacement,
            'actual_network_native_service_model_device_effects':0,'product_or_physical_go':False}

    async def close(self):
        self.release_get.set()
        assert not self.consumer_runtime._busy.locked()
        assert self.host.server is None and self.host.worker is None and self.host.workers == []
        assert not self.producer.attempts
        assert self.store.db.execute('SELECT COUNT(*) FROM resource_leases').fetchone()[0] == 0
        assert self.app.state.rules.backend.records() == self.before_records
        s = self.app.state
        await self.http.aclose()
        await s.home_flow.close()
        s.queries.close()
        await s.family_media.close()
        if s.nas_connection:
            await s.nas_connection.close()
        s.rule_analysis.close()
        await s.camera_media.stop()
        await s.camera_recognition.stop()
        await s.geek_session.release()
        await s.archive_player.close()
        await s.archive.close()
        s.core.measurements.enrollment.close()
        await s.rule_runtime.close()
        await s.core_rule_runtime.close()
        await s.core.adapter.close()
        s.core.actions.close()
        s.rules.close()
        s.core.store.close()
        self.store.close()
        self.short_home.cleanup()


@pytest.mark.asyncio
async def test_V1_actual_ASGI_first_v2_claim_without_legacy_bootstrap(tmp_path, monkeypatch):
    f = await Composition.create(tmp_path, monkeypatch)
    try:
        result = await f.analyze()
        assert result['status'] == 'needs_review' and result['plan'] is None and not result['real_write_attempted']
        assert f.adapter_failure == 'synthetic_stopped_before_inference'
        submits = [(m,p) for m,p in f.client.calls if m in {'submit','priority.submit.v1'}]
        assert len(submits) == 1 and submits[0][0] == 'priority.submit.v1'
        request = submits[0][1]
        assert request['key'] == f.review.id == f.review.request['execution_id']
        assert json.loads(request['args']['request_json']) == f.review.request
        assert request['scheduling']['deadline_at_ms'] <= f.review.request['expires_at_ms']
        assert request['scheduling']['requested_class'] == 'interactive'
        assert f.row['attempts'] == 1 and f.preflight_ack_count == f.job_ack_count == f.stopped_turn_boundaries == 1
        facts = copy.deepcopy(f.client.first_receipt['scheduling'])
        replay = f.client.rpc('priority.submit.v1', copy.deepcopy(request))
        assert replay['id'] == f.row['id'] and replay['scheduling'] == facts
        assert f.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert (await f.analyze())['code'] == 'analysis_previous_attempt_not_replayed'
        assert sum(m == 'priority.negotiate.v2' for m,_ in f.client.calls) == 1
        (tmp_path/'EVIDENCE.json').write_text(json.dumps(f.evidence('V1',result),indent=2)+'\n')
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_V2_revoke_after_real_negotiation_holds_before_submit(tmp_path, monkeypatch):
    f = await Composition.create(tmp_path, monkeypatch)
    try:
        f.on_negotiate = lambda: f.verifier.revoke(f.pid, expected_generation=f.generation)
        result = await f.analyze()
        assert result['status'] == 'needs_review' and result['plan'] is None
        assert f.client.first_receipt is None and f.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert f.job_ack_count == f.stopped_turn_boundaries == 0
        assert f.producer.snapshot('friday') == ()
        assert (await f.analyze())['code'] == 'analysis_previous_attempt_not_replayed'
        assert sum(m == 'priority.negotiate.v2' for m,_ in f.client.calls) == 1
        assert sum(m == 'priority.submit.v1' for m,_ in f.client.calls) == 1
        (tmp_path/'EVIDENCE.json').write_text(json.dumps(f.evidence('V2',result),indent=2)+'\n')
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_V3_cancel_actual_ASGI_caller_keeps_gate_until_late_get_returns(tmp_path, monkeypatch):
    f = await Composition.create(tmp_path, monkeypatch)
    try:
        f.hold_get = True
        task = asyncio.create_task(f.analyze())
        assert await asyncio.to_thread(f.get_entered.wait, 2)
        assert f.consumer_runtime._busy.locked()
        assert f.store.row('friday',f.client.first_receipt['id'])['state'] == 'queued'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert f.consumer_runtime._busy.locked(), 'Cancelled caller cannot release a still-running SDK worker'
        f.release_get.set()
        end = time.monotonic()+2
        while f.consumer_runtime._busy.locked() and time.monotonic()<end:
            await asyncio.sleep(.005)
        assert not f.consumer_runtime._busy.locked()
        job = f.store.row('friday',f.client.first_receipt['id'])
        assert job['state'] == 'cancelled' and job['result'] is None and job['attempts'] == 0
        assert [p['id'] for m,p in f.client.calls if m == 'cancel'] == [f.client.first_receipt['id']]
        assert f.job_ack_count == f.stopped_turn_boundaries == 0
        result = await f.analyze()
        assert result['code'] == 'analysis_previous_attempt_not_replayed'
        assert sum(m == 'priority.submit.v1' for m,_ in f.client.calls) == 1
        (tmp_path/'EVIDENCE.json').write_text(json.dumps(f.evidence('V3',result),indent=2)+'\n')
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_V4_replace_during_actual_late_job_ack_preserves_new_generation(tmp_path, monkeypatch):
    f = await Composition.create(tmp_path, monkeypatch)
    try:
        def replace():
            f.verifier.register('friday',f.pid,f.binary)
            f.replacement_generation = f.verifier.registration_generation('friday',f.pid)
        f.on_job_ack = replace
        result = await f.analyze()
        assert result['status'] == 'needs_review' and result['plan'] is None
        assert f.adapter_failure == 'priority_identity_binding_changed'
        assert f.job_ack_count == 1 and f.stopped_turn_boundaries == 0
        assert f.store.row('friday',f.client.first_receipt['id'])['result'] is None
        assert f.producer.snapshot('friday') == ()
        f.old_cleanup_revoked_replacement = f.verifier.revoke(f.pid, expected_generation=f.generation)
        assert f.old_cleanup_revoked_replacement is False and f.replacement_generation>f.generation
        assert f.verifier.registration_generation('friday',f.pid) == f.replacement_generation
        assert f.verifier.registration_subject('friday') != f.original_subject
        assert (await f.analyze())['code'] == 'analysis_previous_attempt_not_replayed'
        assert sum(m == 'priority.submit.v1' for m,_ in f.client.calls) == 1
        (tmp_path/'EVIDENCE.json').write_text(json.dumps(f.evidence('V4',result),indent=2)+'\n')
    finally:
        await f.close()
