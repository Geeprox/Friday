"""Application registration and receipt boundaries; no household/model requests."""
import asyncio
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import time

import httpx
import pytest

from homeplane.arl import ARLError, ARLRuntime
from homeplane.arl_application import APP_ID, SCENARIOS, FridayApplication


def test_actual_app_icon_preserves_original_pixels_and_truthful_icns_source():
    import base64
    import struct
    import zlib
    from homeplane.arl_icon import application_icon
    source = Path(__file__).resolve().parents[1] / 'native/FridayDesktop/Resources/AppIcon/Friday.icns'
    raw = source.read_bytes()
    payload = application_icon(source)
    assert payload['source_resource'] == 'CFBundleIconFile:icp6'
    assert payload['source_sha256'] == hashlib.sha256(raw).hexdigest()
    png = base64.b64decode(payload['png_base64'], validate=True)
    assert len(png) == payload['byte_count'] <= 8192
    assert hashlib.sha256(png).hexdigest() == payload['sha256']
    def pixels(image):
        at, compressed = 8, bytearray()
        while at < len(image):
            size = struct.unpack('>I', image[at:at+4])[0]
            if image[at+4:at+8] == b'IDAT':
                compressed.extend(image[at+8:at+8+size])
            at += size + 12
        return zlib.decompress(compressed)
    at = 8
    while raw[at:at+4] != b'ic12':
        at += struct.unpack('>I', raw[at+4:at+8])[0]
    original = raw[at+8:at+struct.unpack('>I', raw[at+4:at+8])[0]]
    assert pixels(png) == pixels(original)


class Session:
    def __init__(self, home):
        self.client = SimpleNamespace(home=home)
        self.connected = False
        self.accepted = []
        self.error = None
        self.starts = self.closes = 0
        self.submissions, self.cancelled, self.jobs = [], [], {}
        self.running = False
        self.mutate = lambda value: None
        self.lose_ack = False

    def start(self):
        self.starts += 1
        self.connected = True
        self.accepted = ['codex.text.plan.v1']

    def close(self):
        self.closes += 1
        self.connected = False

    def submit(self, scenario, text, *, request_id):
        self.connected = True
        self.submissions.append(request_id)
        if request_id not in self.jobs:
            definition = next(s for s in SCENARIOS if s['id'] == scenario)
            self.jobs[request_id] = dict(id=request_id, state='queued', tool='codex.text.plan.v1',
                scenario_id=scenario, package_id=definition['package_id'],
                resolved=dict(model='synthetic-model', reasoning_effort='high', service_tier='default', priority=definition['priority']),
                input=text)
        if self.lose_ack:
            self.lose_ack = False
            raise ARLError('disconnected')
        return copy.deepcopy(self.jobs[request_id])

    def get(self, job_id):
        admitted = self.jobs[job_id]
        if self.running:
            return dict(id=job_id, state='running', tool='codex.text.plan.v1')
        text = 'synthetic private response'
        result = dict(schema_version=1, simulated=True, provider='synthetic-provider', model=admitted['resolved']['model'],
            execution_preferences=dict(reasoning_effort='high', service_tier='default', source='test_double'),
            request_sha256=hashlib.sha256(admitted['input'].encode()).hexdigest(),
            output_sha256=hashlib.sha256(text.encode()).hexdigest(), text=text, thread_id='synthetic-thread', turn_id='synthetic-turn')
        reply = dict(id=job_id, state='succeeded', tool='codex.text.plan.v1', result=result,
                     progress=dict(native=dict(thread_id='synthetic-thread', turn_id='synthetic-turn', terminal='completed')))
        self.mutate(reply)
        return reply

    def cancel(self, job_id):
        self.cancelled.append(job_id)


def application(tmp_path, *, allow_simulated=True):
    session = Session(tmp_path.resolve())
    app = FridayApplication(lambda: tmp_path.resolve(), lambda: tmp_path.resolve(),
                            session_factory=lambda _: session, allow_simulated=allow_simulated)
    return app, session


@pytest.mark.asyncio
async def test_lifecycle_registers_once_and_metadata_never_submits(tmp_path):
    app, session = application(tmp_path)
    assert not app.status()['connected'] and session.starts == 0
    app.start(); app.start()
    report = app.status()
    assert session.starts == 1 and report['connected'] and report['app_id'] == APP_ID
    assert report['model_service_accepted'] and not report['device_authority']
    assert report['heartbeat_seconds'] == 10 and report['lease_seconds'] == 30
    assert session.submissions == []
    await app.close()
    assert not app.status()['connected'] and session.closes == 1


@pytest.mark.asyncio
async def test_missing_platform_sdk_is_explicit_and_does_not_launch(tmp_path):
    def missing(_):
        raise ImportError('synthetic absent SDK')
    app = FridayApplication(lambda: tmp_path, lambda: tmp_path, session_factory=missing)
    app.start()
    assert app.status()['state'] == 'upgrade_required'
    assert not app.status()['connected']
    with pytest.raises(ARLError, match='application_sdk_upgrade_required'):
        await app.complete('voice_intent', 'synthetic input', request_id='request-1', deadline=time.monotonic()+1)
    await app.close()


@pytest.mark.asyncio
async def test_persisted_admission_reuses_job_and_contains_no_prompt_or_result(tmp_path):
    app, session = application(tmp_path)
    prompt = 'synthetic private prompt'
    first = await app.complete('voice_intent', prompt, request_id='request-1', deadline=time.monotonic()+2)
    again = await app.complete('voice_intent', prompt, request_id='request-1', deadline=time.monotonic()+2)
    assert first == again and session.submissions == ['request-1'] and len(session.jobs) == 1
    assert first['application_admission']['package_id'] == 'low'
    assert first['native_acknowledgements_verified'] and not first['device_authority']
    raw = (tmp_path / 'arl-application/receipts.sqlite').read_bytes()
    assert prompt.encode() not in raw and first['text'].encode() not in raw
    with pytest.raises(ARLError, match='model_request_conflict'):
        await app.complete('voice_intent', 'changed input', request_id='request-1', deadline=time.monotonic()+2)
    assert session.submissions == ['request-1']
    await app.close()


@pytest.mark.asyncio
async def test_lost_ack_reuses_original_request_id_and_never_creates_another_job(tmp_path):
    app, session = application(tmp_path)
    session.lose_ack = True
    with pytest.raises(ARLError, match='disconnected'):
        await app.complete('voice_intent', 'synthetic', request_id='request-1', deadline=time.monotonic()+2)
    assert len(session.jobs) == 1
    await app.complete('voice_intent', 'synthetic', request_id='request-1', deadline=time.monotonic()+2)
    assert session.submissions == ['request-1', 'request-1'] and len(session.jobs) == 1
    await app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['model', 'effort', 'tier', 'input_hash', 'output_hash', 'native', 'simulated'])
async def test_unverified_model_or_native_receipts_cannot_escape(tmp_path, tamper):
    app, session = application(tmp_path, allow_simulated=tamper != 'simulated')
    def mutate(job):
        result = job['result']
        if tamper == 'model': result['model'] = 'wrong-model'
        if tamper == 'effort': result['execution_preferences']['reasoning_effort'] = 'wrong'
        if tamper == 'tier': result['execution_preferences']['service_tier'] = 'wrong'
        if tamper == 'input_hash': result['request_sha256'] = '0'*64
        if tamper == 'output_hash': result['output_sha256'] = '0'*64
        if tamper == 'native': job['progress']['native']['terminal'] = 'unknown'
    session.mutate = mutate
    with pytest.raises(ARLError, match='model_result_unverified'):
        await app.complete('voice_intent', 'synthetic', request_id='request-1', deadline=time.monotonic()+2)
    assert session.cancelled == ['request-1']
    await app.close()


@pytest.mark.asyncio
async def test_cancel_and_changed_business_context_never_deliver_late_model_output(tmp_path):
    app, session = application(tmp_path)
    session.running = True
    task = asyncio.create_task(app.complete('voice_intent', 'synthetic', request_id='request-1', deadline=time.monotonic()+2))
    while not session.submissions:
        await asyncio.sleep(.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await app.close()
    assert session.cancelled == ['request-1']
    app, session = application(tmp_path)
    with pytest.raises(ARLError, match='discarded_model_task_changed'):
        await app.complete('voice_intent', 'synthetic', request_id='request-2', deadline=time.monotonic()+2, current=lambda:False)
    assert not session.submissions
    await app.close()


@pytest.mark.asyncio
async def test_worker_model_port_requires_existing_capability_and_preserves_privacy_pause(tmp_path, monkeypatch):
    from homeplane.api import create_app
    import homeplane.embodied.api as api
    from homeplane.arl import current_model_task
    monkeypatch.setattr(api, 'PRIVATE', tmp_path.resolve())
    key = 'synthetic-control-capability-'*2
    (tmp_path / 'control-key').write_text(key)
    calls = []
    class Model:
        async def complete_scenario(self, scenario, text, **kwargs):
            calls.append((scenario, kwargs['request_id'], current_model_task().task_kind))
            return {'synthetic_only':True}
    app = create_app(db_path=tmp_path.resolve()/'traces.sqlite', settings_path=tmp_path/'settings.json', arl_runtime=Model())
    app.state.infrastructure.policy = app.state.infrastructure.policy.model_copy(update={'enabled': True})
    app.state.embodied.capture_enabled = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', headers={'Origin':'http://testserver'}) as client:
        body = dict(request_id='request-1', input='synthetic prompt')
        assert (await client.post('/api/embodied/model', json=body)).status_code == 403
        headers = {'X-Friday-Worker':key}
        assert (await client.post('/api/embodied/model', headers=headers, json={**body,'model':'forbidden'})).status_code == 422
        assert (await client.post('/api/embodied/model', headers=headers, json=body)).status_code == 200
        (tmp_path / 'voice-input-paused').touch()
        assert (await client.post('/api/embodied/model', headers=headers, json=body)).status_code == 409
    assert calls == [('voice_intent','request-1','household.voice.intent')]
    async with app.router.lifespan_context(app):
        pass


def test_voice_worker_uses_only_owner_bridge_and_does_not_select_models():
    from homeplane.embodied.arl_intent import IntentRuntime
    source = Path(__file__).resolve().parents[1]
    text = (source/'homeplane/embodied/voice_worker.py').read_text()
    bridge = (source/'homeplane/embodied/arl_intent.py').read_text()
    assert 'compose_codex_service' not in bridge and 'CodexService' not in bridge and 'TemporaryDirectory' not in bridge
    assert 'provider["models"]' not in text and 'gpt-' not in bridge
    assert IntentRuntime is not None


@pytest.mark.asyncio
async def test_changed_runtime_home_closes_old_session_and_registers_one_new_owner(tmp_path):
    first, second = tmp_path/'first', tmp_path/'second'
    first.mkdir(); second.mkdir()
    selected = [first]
    sessions = []
    def factory(home):
        session = Session(home)
        sessions.append(session)
        return session
    runtime = ARLRuntime(lambda:selected[0], application_factory=factory, state_directory=lambda:tmp_path)
    runtime.application.allow_simulated = True
    await runtime.start_application()
    selected[0] = second
    await asyncio.gather(runtime.complete_scenario('voice_intent', 'synthetic input',
        request_id='home-switch', deadline=time.monotonic()+2), runtime.start_application())
    assert len(sessions) == 2 and sessions[0].closes == 1 and sessions[1].starts == 1
    assert sessions[0].submissions == [] and sessions[1].submissions == ['home-switch']
    assert runtime.application.status()['connected']
    await runtime.close()
    assert sessions[1].closes == 1
