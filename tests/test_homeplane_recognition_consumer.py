"""Finite synthetic consumer/IPC tests. No SDK, weights or household media."""
import ast
import asyncio
import hashlib
import importlib.abc
import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import numpy as np
import pytest
from test_homeplane_native_media import SimulatedSDK, immediate
from test_recognition_packet import GOOD_METADATA, FakeCV, FakeSherpa, metadata_bytes

from homeplane.api import create_app
from homeplane.media_contracts import MediaProbeResult, NativeMediaConnection
from homeplane.media_probe import MAX_RSS, RecognitionProbe, native_worker_command
from homeplane.native_media_worker import NativeCounter, read_camera, read_camera_recognition
from homeplane.recognition.artifacts import LocalArtifact
from homeplane.recognition.contracts import Candidate, Embedding, EmbeddingResult, Status, VerificationPolicy
from homeplane.recognition.face import FaceQuality, OpenCVFaceProvider
from homeplane.recognition.speaker import SherpaSpeakerProvider, SpeakerQuality
from homeplane.recognition_consumer import (
    _OWNED_RECOGNITION_CALLS,
    RecognitionConsumer,
    SyntheticOwnerContext,
)
from homeplane.recognition_contracts import (
    RecognitionProvenance,
    RecognitionResult,
    RecognitionSession,
    RecognitionStatus,
)
from homeplane.settings import SettingsConfig, SettingsService


class NativeFence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cv2', 'sherpa_onnx', 'miot', 'torch', 'onnxruntime', 'transformers'}:
            raise AssertionError('real_native_import_forbidden')


@pytest.fixture(autouse=True)
def no_native_imports():
    fence = NativeFence()
    sys.meta_path.insert(0, fence)
    yield
    sys.meta_path.remove(fence)


def session(modality='face', **changes):
    return RecognitionSession(session_id=str(uuid4()), generation=1, owner_operation='synthetic-owner',
        camera_id='SIMULATED-camera', modality=modality, deadline_monotonic=time.monotonic()+6,
        simulated=True, **changes)


def artifact(tmp_path, name, contents):
    p = tmp_path / name
    p.write_bytes(contents)
    return LocalArtifact(p, hashlib.sha256(contents).hexdigest())


def owner(tmp_path, modality='face', *, candidates=True, quality=None):
    if modality == 'face':
        a = artifact(tmp_path, 'SIMULATED-not-a-face-model.onnx', b'SYNTHETIC-NOT-A-MODEL')
        backend = FakeCV()
        provider = OpenCVFaceProvider(a, a, enabled=True)
        quality = quality or FaceQuality(True, True, True)
    else:
        a = artifact(tmp_path, 'SIMULATED-not-a-speaker-model.onnx', metadata_bytes(GOOD_METADATA))
        backend = FakeSherpa()
        provider = SherpaSpeakerProvider(a, enabled=True)
        quality = quality or SpeakerQuality(True, True, True)
    assert provider.activate(_module=backend).state == 'ready'
    vector = Embedding(modality, a.sha256, (1.0,) * (128 if modality == 'face' else 256), True)
    policy = VerificationPolicy(modality, a.sha256, .9, .1, True, 'SIMULATED-calibration')
    provenance = RecognitionProvenance(provider='opencv_yunet_sface' if modality == 'face' else 'sherpa_wespeaker',
        embedding_model_sha256=a.sha256, detector_model_sha256=a.sha256 if modality == 'face' else None)
    context = SyntheticOwnerContext(session(modality), provider, quality, provenance, policy,
        (Candidate('SIMULATED-reference', vector),) if candidates else ())
    return context, backend


async def zero_rss(process):
    return 0


async def launch(code=None):
    command = [sys.executable, '-B', '-I', str(Path(__file__).parents[1] / 'homeplane/native_media_worker.py')]
    if code is not None:
        command = [sys.executable, '-B', '-c', code]
    return await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)


def settings(tmp_path):
    result = SettingsService(tmp_path / 'settings.json', token_path=tmp_path / 'absent-synthetic-token.json')
    result.update({'camera_id': 'SIMULATED-camera', 'camera_media_source': 'miot_sdk'})
    return result


@pytest.mark.asyncio
async def test_disabled_callback_status_and_close_do_zero_work(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('disabled_work')
    monkeypatch.setattr('homeplane.recognition_consumer.ThreadPoolExecutor', forbidden)
    monkeypatch.setattr(LocalArtifact, 'verified_path', forbidden)
    consumer = RecognitionConsumer()
    await consumer.video('SIMULATED-camera', object(), 0)
    await consumer.audio('SIMULATED-camera', object(), 0)
    assert await consumer.finish() is None
    await consumer.close()
    assert consumer.status() == RecognitionStatus()
    assert not _OWNED_RECOGNITION_CALLS


def test_fresh_disabled_composition_never_imports_optional_modules():
    code = '''
import asyncio,importlib.abc,sys
class Fence(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in {'numpy','cv2','sherpa_onnx','miot'}: raise AssertionError('native import')
sys.meta_path.insert(0,Fence())
from homeplane.recognition_consumer import RecognitionConsumer
from homeplane.media_probe import RecognitionProbe
from types import SimpleNamespace
async def run():
 c=RecognitionConsumer();p=RecognitionProbe(SimpleNamespace(revision=0))
 assert c.status().state==p.status().state=='disabled'
 await c.close();await p.stop()
asyncio.run(run())
'''
    run = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, timeout=4)
    assert run.returncode == 0, run.stderr.decode()


@pytest.mark.asyncio
async def test_actual_app_cached_status_save_off_and_forbidden_json(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('provider_or_media_work')
    monkeypatch.setattr(OpenCVFaceProvider, 'activate', forbidden)
    monkeypatch.setattr(SherpaSpeakerProvider, 'activate', forbidden)
    monkeypatch.setattr('homeplane.media_probe.launch_media_worker', forbidden)
    config = settings(tmp_path)
    app = create_app(db_path=tmp_path/'synthetic-traces.sqlite', settings_service=config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            status = await client.get('/api/settings/camera/recognition')
            assert status.json() == RecognitionStatus().model_dump()
            saved = await client.post('/api/settings/camera/recognition/off', json={'enabled': False},
                headers={'origin': 'http://testserver'})
            assert saved.status_code == 200 and saved.json() == status.json()
            for bad in ({'enabled': True}, {'enabled': 0}, {'enabled': False, '_module': 'fake'},
                        {'enabled': False, 'quality': True}, {'enabled': False, 'permit': True}):
                rejected = await client.post('/api/settings/camera/recognition/off', json=bad,
                    headers={'origin': 'http://testserver'})
                assert rejected.status_code == 422 and rejected.json() == {'code': 'invalid_request'}
            assert (await client.post('/api/settings/camera/recognition', json={},
                headers={'origin': 'http://testserver'})).status_code == 405
            diagnostic = MediaProbeResult(provider='miot_sdk', simulated=True, probe_id=str(uuid4()))
            app.state.camera_media.on_result(diagnostic)
            assert not diagnostic.model_invoked and diagnostic.source_av_alignment == 'unavailable'
            assert app.state.camera_recognition.task is None
            assert not app.state.core.adapter.managed_actions
            rows = app.state.core.store.recent_events(simulated=True)
            assert len(rows) == 1 and rows[0].payload['model_invoked'] is False
            assert rows[0].payload['trigger_eligible'] is False
    assert json.loads(config.path.read_text())['config']['camera_recognition_enabled'] is False


@pytest.mark.parametrize('value', [True, 0, 1, 'false', None])
def test_settings_cannot_issue_activation(value):
    with pytest.raises(ValueError):
        SettingsConfig(camera_recognition_enabled=value)


@pytest.mark.asyncio
async def test_real_decoded_face_callback_to_provider_and_private_serialization(tmp_path):
    context, backend = owner(tmp_path)
    consumer = RecognitionConsumer(_owner_context=context)
    counter = NativeCounter(context.session.camera_id, _recognition=consumer)
    image = np.full((200, 320, 3), 127, dtype=np.uint8)
    await counter.video(context.session.camera_id, image, 1, 0, 1, 1)
    result = await consumer.finish()
    assert result.status.state == 'matched' and backend.detector.calls == backend.recognizer.calls == 1
    assert np.all(image == 127), 'Only owned copy is cleared'
    payload = result.model_dump_json()
    assert RecognitionResult.model_validate_json(payload).require_session(context.session) == result
    assert result.observation.provenance == context.provenance and result.observation.quality.qualified
    assert result.observation.identity_claim == 'none' and result.observation.person_association == 'unavailable'
    assert result.observation.timing.source_timestamp == 1
    assert result.observation.timing.source_clock_domain == 'unverified'
    assert result.observation.track_id is None and result.observation.tracking == 'unavailable'
    for secret in ('values', 'frame_digest', '.onnx', 'SIMULATED-reference', 'similarity'):
        if secret == 'similarity':
            continue  # Fixed reason similarity_evidence_only is intentional.
        assert secret not in payload
    assert not result.status.action_authorized and not result.status.security_authentication
    assert result.status.av_association == 'unavailable'
    await consumer.close()


@pytest.mark.asyncio
async def test_ready_embedding_without_enrollment_and_serial_native_thread(tmp_path):
    context, backend = owner(tmp_path, candidates=False)
    threads = []
    detect = backend.detector.detect
    def measured(image):
        threads.append(threading.get_ident())
        return detect(image)
    backend.detector.detect = measured
    consumer = RecognitionConsumer(_owner_context=context)
    image = np.full((200, 320, 3), 127, dtype=np.uint8)[:, ::-1, :]
    await consumer.video(context.session.camera_id, image, 0)
    assert consumer.status().state == 'ready' and consumer.status().reason == 'embedding_only'
    assert len(threads) == 1 and threads[0] != threading.get_ident()
    await consumer.close()


@pytest.mark.asyncio
async def test_face_bounds_and_unknown_quality_prevent_native_call(tmp_path):
    for image, quality, reason in [(np.zeros((721, 20, 3), np.uint8), FaceQuality(True, True, True), 'invalid_video'),
                                 (np.zeros((200, 320, 3), np.uint8), FaceQuality(), 'quality_unqualified')]:
        context, backend = owner(tmp_path, quality=quality)
        consumer = RecognitionConsumer(_owner_context=context)
        await consumer.video(context.session.camera_id, image, 0)
        assert consumer.status().reason == reason and backend.detector.calls == 0
        await consumer.close()


@pytest.mark.asyncio
async def test_wrong_camera_channel_generation_and_expiry_never_call(tmp_path):
    context, backend = owner(tmp_path)
    now = [context.session.deadline_monotonic - 1]
    consumer = RecognitionConsumer(_owner_context=context, clock=lambda: now[0])
    image = np.zeros((200, 320, 3), np.uint8)
    await consumer.video('SIMULATED-other', image, 0)
    await consumer.video(context.session.camera_id, image, 1)
    assert backend.detector.calls == 0
    now[0] = context.session.deadline_monotonic
    await consumer.video(context.session.camera_id, image, 0)
    assert consumer.status().state == 'expired' and backend.detector.calls == 0
    await consumer.close()


@pytest.mark.asyncio
async def test_audio_bounded_owned_window_int16_normalization_and_clear(tmp_path):
    context, backend = owner(tmp_path, 'speaker')
    consumer = RecognitionConsumer(_owner_context=context)
    received = []
    compute = backend.extractor.compute
    def measured(stream):
        received.append(np.array(stream.samples, copy=True))
        return compute(stream)
    backend.extractor.compute = measured
    chunk = np.full(16000, 3277, dtype=np.int16)
    await consumer.audio(context.session.camera_id, chunk, 0)
    owned_chunk = consumer._audio[0]
    await consumer.audio(context.session.camera_id, chunk, 0)
    assert consumer.status().state == 'matched'
    stream = backend.extractor.streams[0]
    assert stream.samples.dtype == np.float32 and stream.samples.flags.c_contiguous and stream.samples.size == 32000
    assert not np.any(stream.samples) and not np.any(owned_chunk)
    assert np.allclose(received[0], np.float32(3277/32768))
    assert np.all(chunk == 3277) and not consumer._audio
    await consumer.close()


@pytest.mark.asyncio
async def test_audio_short_overlap_unknown_and_oversize_do_not_embed(tmp_path):
    for quality, size, reason in [(SpeakerQuality(True, True, True), 1000, 'short_audio'),
                                 (SpeakerQuality(True, False, True), 32000, 'quality_unqualified'),
                                 (SpeakerQuality(), 32000, 'quality_unqualified'),
                                 (SpeakerQuality(True, True, True), 128001, 'invalid_audio'),
                                 (SpeakerQuality(True, True, True), 32001, 'audio_window_overflow')]:
        context, backend = owner(tmp_path, 'speaker', quality=quality)
        consumer = RecognitionConsumer(_owner_context=context)
        await consumer.audio(context.session.camera_id, np.full(size, 1000, np.int16), 0)
        await consumer.finish()
        assert consumer.status().reason == reason and not backend.extractor.streams and not consumer._audio
        await consumer.close()


@pytest.mark.parametrize('change,reason', [({'model_sha256': '0'*64}, 'model_mismatch'),
                                          ({'simulated': False}, 'simulation_mismatch')])
@pytest.mark.asyncio
async def test_provider_model_and_simulation_binding_rejected(tmp_path, change, reason):
    context, _ = owner(tmp_path)
    observed = replace(context.candidates[0].embedding, **change)
    context = replace(context, provider=SimpleNamespace(
        detect=lambda image: None, embed=lambda *args: EmbeddingResult(Status('ready', 'fake'), observed), close=lambda: None))
    consumer = RecognitionConsumer(_owner_context=context)
    await consumer.video(context.session.camera_id, np.zeros((200, 320, 3), np.uint8), 0)
    assert consumer.status().state == 'unknown' and consumer.status().reason == reason
    await consumer.close()


@pytest.mark.asyncio
async def test_unqualified_policy_and_real_owner_rejected(tmp_path):
    context, backend = owner(tmp_path)
    consumer = RecognitionConsumer(_owner_context=replace(context, policy=replace(context.policy, calibrated=False)))
    await consumer.video(context.session.camera_id, np.zeros((200, 320, 3), np.uint8), 0)
    assert consumer.status().reason == 'policy_unqualified'
    await consumer.close()
    consumer = RecognitionConsumer(_owner_context=replace(context,
        session=context.session.model_copy(update={'simulated': False})))
    assert consumer.status().reason == 'owner_admission_unavailable'
    assert backend.detector.calls == 1


@pytest.mark.asyncio
async def test_late_native_result_cannot_publish_or_release_running_native(tmp_path):
    context, backend = owner(tmp_path)
    started, release = threading.Event(), threading.Event()
    detect = backend.detector.detect
    def hung(image):
        started.set()
        release.wait(3)
        return detect(image)
    backend.detector.detect = hung
    consumer = RecognitionConsumer(_owner_context=context)
    call = asyncio.create_task(consumer.video(context.session.camera_id, np.zeros((200, 320, 3), np.uint8), 0))
    while not started.is_set():
        await asyncio.sleep(.001)
    consumer.invalidate()
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(consumer.close(), .02)
        assert id(consumer) in _OWNED_RECOGNITION_CALLS and context.provider._detector is not None
    finally:
        release.set()
        await consumer.close()
    assert consumer.status().reason == 'stale_generation'
    assert id(consumer) not in _OWNED_RECOGNITION_CALLS


@pytest.mark.asyncio
async def test_actual_sdk_lifecycle_callback_owner_seam_with_synthetic_arrays(tmp_path):
    context, backend = owner(tmp_path)
    sdk = SimulatedSDK()
    async def start(**options):
        did = context.session.camera_id
        await sdk.instance.callbacks['raw_video'](did, b'SIMULATED', 1, 1, 0)
        await sdk.instance.callbacks['raw_audio'](did, b'SIMULATED', 1, 1, 0, SimpleNamespace(name='AUDIO_OPUS'))
        await sdk.instance.callbacks['video'](did, np.full((200, 320, 3), 127, np.uint8), 1, 0, 1, 1)
        await sdk.instance.callbacks['audio'](did, np.full(320, 1000, np.int16), 1, 0, 1, 1)
    sdk.instance.start_async = start
    result = await read_camera_recognition(context.session, sdk, codec_quality='SIMULATED-LOW',
        _owner_context=context, sleep=immediate)
    assert result.status.state == 'matched' and backend.recognizer.calls == 1
    assert sdk.calls[-1] == 'deinit' and not _OWNED_RECOGNITION_CALLS
    untouched = SimulatedSDK()
    unavailable = await read_camera_recognition(context.session, untouched, codec_quality='SIMULATED-LOW')
    assert unavailable.status.reason == 'owner_admission_unavailable' and not untouched.calls


@pytest.mark.asyncio
async def test_normal_diagnostic_never_constructs_recognition(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('recognition_construction')
    monkeypatch.setattr('homeplane.native_media_worker.RecognitionConsumer', forbidden)
    result = await read_camera(NativeMediaConnection(camera_id='SIMULATED-camera'), SimulatedSDK(),
        codec_quality='SIMULATED-LOW', sleep=immediate)
    assert result.status == 'decoded_sync_unknown' and result.model_invoked is False


@pytest.mark.parametrize('field,value', [('generation', 2), ('session_id', str(uuid4())),
    ('owner_operation', 'different-owner'), ('camera_id', 'other'), ('modality', 'speaker'),
    ('deadline_monotonic', 1.0), ('simulated', False)])
def test_private_result_rejects_every_stale_binding(field, value):
    expected = session()
    status = RecognitionStatus(state='unavailable', reason='owner_admission_unavailable', simulated=True)
    if field == 'simulated':
        status = status.model_copy(update={'simulated': False})
    raw = RecognitionResult(session=expected.model_copy(update={field: value}), status=status).model_dump_json()
    with pytest.raises(ValueError):
        RecognitionResult.model_validate_json(raw).require_session(expected)


@pytest.mark.parametrize('field,value', [('embedding', [1.0]), ('frame_digest', 'private'),
                                       ('template', 'private'), ('model_path', '/private')])
def test_wire_contract_rejects_media_and_biometric_exports(field, value):
    raw = RecognitionResult(session=session(), status=RecognitionStatus(simulated=True)).model_dump(mode='json')
    raw[field] = value
    with pytest.raises(ValueError):
        RecognitionResult.model_validate_json(json.dumps(raw))


@pytest.mark.asyncio
async def test_real_fixed_worker_ipc_unknown_admission_is_unavailable(tmp_path):
    context, _ = owner(tmp_path)
    probe = RecognitionProbe(settings(tmp_path), launcher=launch, rss_reader=zero_rss)
    assert (await probe.start_owner(context)).state == 'running'
    await probe.task
    assert probe.status().reason == 'owner_admission_unavailable'
    assert probe.process is None and probe.task.done()
    command = native_worker_command()
    assert command[-1].endswith('homeplane/native_media_worker.py') and command[1:3] == ['-B', '-I']
    await probe.stop()
    assert probe.status().state == 'disabled'


@pytest.mark.asyncio
async def test_real_child_fragmented_serialization_and_wrong_owner_rejected(tmp_path):
    context, _ = owner(tmp_path)
    consumer = RecognitionConsumer(_owner_context=context)
    await consumer.video(context.session.camera_id, np.zeros((200, 320, 3), np.uint8), 0)
    frozen = consumer.observation.model_dump(mode='json')
    await consumer.close()
    child = 'OBSERVATION=' + repr(frozen) + "\n" + "import json,sys,time" + '''
r=json.load(sys.stdin)
s={'state':'ready','reason':'embedding_only','simulated':True}
b=json.dumps({'schema_version':2,'session':r,'status':s,'media_persisted':False,'observation':OBSERVATION}).encode()
sys.stdout.buffer.write(b[:19]);sys.stdout.buffer.flush();time.sleep(.03)
sys.stdout.buffer.write(b[19:]);sys.stdout.buffer.flush()
'''
    probe = RecognitionProbe(settings(tmp_path), launcher=lambda: launch(child), rss_reader=zero_rss)
    await probe.start_owner(context)
    await probe.task
    assert probe.status().state == 'ready', probe.status().model_dump()
    assert probe.private_result(context).observation.observation_id == consumer.observation.observation_id
    assert probe.private_result(replace(context, session=context.session.model_copy(update={'owner_operation': 'wrong'}))) is None
    await probe.stop()
    context = replace(context, session=context.session.model_copy(update={'generation': probe.generation+1}))
    wrong = child.replace("r=json.load(sys.stdin)", "r=json.load(sys.stdin);r['owner_operation']='wrong'")
    probe.launcher = lambda: launch(wrong)
    await probe.start_owner(context)
    await probe.task
    assert probe.status().reason == 'worker_failed'


@pytest.mark.asyncio
async def test_real_child_timeout_is_killed_and_reaped(tmp_path):
    context, _ = owner(tmp_path)
    context = replace(context, session=context.session.model_copy(update={'deadline_monotonic': time.monotonic()+.2}))
    owned = []
    async def spawn():
        p = await launch('import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)')
        owned.append(p)
        return p
    probe = RecognitionProbe(settings(tmp_path), launcher=spawn, rss_reader=zero_rss)
    await probe.start_owner(context)
    await asyncio.wait_for(probe.task, 5)
    assert owned[0].returncode == -9 and probe.process is None
    assert probe.report.reason == 'worker_timeout'


@pytest.mark.asyncio
async def test_real_child_rss_guard_and_save_off_reap(tmp_path):
    context, _ = owner(tmp_path)
    owned = []
    async def spawn():
        p = await launch('import time;time.sleep(20)')
        owned.append(p)
        return p
    async def too_large(process):
        return MAX_RSS+1
    probe = RecognitionProbe(settings(tmp_path), launcher=spawn, rss_reader=too_large)
    await probe.start_owner(context)
    await probe.task
    assert probe.report.reason == 'worker_memory_limit' and owned[-1].returncode is not None
    probe.rss_reader = zero_rss
    context = replace(context, session=context.session.model_copy(update={'generation': 2}))
    await probe.start_owner(context)
    while probe.process is None:
        await asyncio.sleep(.001)
    await probe.stop()
    assert owned[-1].returncode is not None and probe.status().state == 'disabled'


@pytest.mark.asyncio
async def test_stale_configuration_cannot_publish_real_child_result(tmp_path):
    context, _ = owner(tmp_path)
    config = settings(tmp_path)
    started = asyncio.Event()
    async def spawn():
        p = await launch()
        started.set()
        return p
    probe = RecognitionProbe(config, launcher=spawn, rss_reader=zero_rss)
    await probe.start_owner(context)
    await started.wait()
    config.update({'camera_id': 'SIMULATED-other'})
    await probe.task
    assert probe.status().reason == 'configuration_changed'
    assert probe.report.state == 'running', 'Stale child report was not published'
    await probe.stop()


def test_protected_quality_callback_and_api_boundary_ast_unchanged():
    # Quality callback frozen; boundary adds only the approved strict-write People prefix.
    expected = {'record_media_quality': '613ed905c7cd6741c748a456054ed7dd5e79c14ab11497edec88586e9dbd018e', 'local_boundary': '2de1eb50a4defc7b4360eb07cbb694c6bb7d495aa54b92342c6624c4253feb28'}
    current = (Path(__file__).parents[1]/'homeplane/api.py').read_text()
    for name, fingerprint in expected.items():
        node = next(n for n in ast.walk(ast.parse(current))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        assert hashlib.sha256(ast.dump(node, include_attributes=False).replace(', type_params=[]', '').encode()).hexdigest() == fingerprint


@pytest.mark.asyncio
async def test_observation_id_stable_and_invalid_quality_or_provenance_rejected(tmp_path):
    context, _ = owner(tmp_path)
    consumer = RecognitionConsumer(_owner_context=context)
    await consumer.video(context.session.camera_id, np.zeros((200, 320, 3), np.uint8), 0)
    result = await consumer.finish()
    frozen = result.model_dump(mode='json')
    assert RecognitionResult.model_validate_json(json.dumps(frozen)).observation.observation_id == result.observation.observation_id
    for change in ('id', 'quality', 'modality', 'simulation', 'identity', 'association', 'missing', 'track', 'time'):
        raw = json.loads(json.dumps(frozen))
        observation = raw['observation']
        if change == 'id':
            observation['observation_id'] = str(uuid4())
        if change == 'quality':
            observation['quality']['qualified'] = False
        if change == 'modality':
            observation['modality'] = 'speaker'
        if change == 'simulation':
            observation['simulated'] = 1
        if change == 'identity':
            observation['identity_claim'] = 'person'
        if change == 'association':
            observation['person_association'] = 'same_person'
        if change == 'missing':
            raw.pop('observation')
        if change == 'track':
            observation['track_id'] = 'fabricated-anonymous-track'
        if change == 'time':
            observation['timing']['source_clock_domain'] = 'validated'
        with pytest.raises(ValueError):
            RecognitionResult.model_validate_json(json.dumps(raw))
    await consumer.close()


@pytest.mark.asyncio
async def test_parent_rejects_wrong_observation_model_provenance(tmp_path):
    context, _ = owner(tmp_path)
    consumer = RecognitionConsumer(_owner_context=context)
    await consumer.video(context.session.camera_id, np.zeros((200, 320, 3), np.uint8), 0)
    result = (await consumer.finish()).model_dump(mode='json')
    await consumer.close()
    result['observation']['provenance']['embedding_model_sha256'] = '0'*64
    child = 'import json,sys;json.load(sys.stdin);sys.stdout.write(' + repr(json.dumps(result)) + ')'
    probe = RecognitionProbe(settings(tmp_path), launcher=lambda: launch(child), rss_reader=zero_rss)
    await probe.start_owner(context)
    await probe.task
    assert probe.report.reason == 'worker_failed' and probe.result is None


@pytest.mark.asyncio
async def test_wrong_owner_and_late_launch_never_publish_and_late_child_is_reaped(tmp_path):
    context, _ = owner(tmp_path)
    spawned = asyncio.Event()
    processes = []
    async def slow_launch():
        await asyncio.sleep(.05)
        process = await launch('import time;time.sleep(20)')
        processes.append(process)
        spawned.set()
        return process
    probe = RecognitionProbe(settings(tmp_path), launcher=slow_launch, rss_reader=zero_rss)
    assert (await probe.start_owner({})).reason == 'owner_admission_unavailable'
    assert not processes
    await probe.start_owner(context)
    await asyncio.sleep(.005)
    await probe.stop()
    await asyncio.wait_for(spawned.wait(), 1)
    await asyncio.wait_for(processes[0].wait(), 4)
    assert processes[0].returncode is not None and probe.status().state == 'disabled' and probe.result is None


@pytest.mark.parametrize('modality', ['face', 'speaker'])
@pytest.mark.asyncio
async def test_full_real_child_sdk_callback_provider_serialization_parent_path(tmp_path, modality):
    context, _ = owner(tmp_path, modality)
    project = Path(__file__).parents[1]
    child = 'import sys\nsys.path[:0]=' + repr([str(project), str(project/'tests')]) + '\n' + '''
import asyncio,importlib.abc,tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
class Fence(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in {'cv2','sherpa_onnx','miot','torch','onnxruntime'}: raise AssertionError('native forbidden')
sys.meta_path.insert(0,Fence())
import numpy as np
from test_homeplane_recognition_consumer import owner
from test_homeplane_native_media import SimulatedSDK,immediate
from homeplane.recognition_contracts import RecognitionSession
from homeplane.native_media_worker import read_camera_recognition
request=RecognitionSession.model_validate_json(sys.stdin.buffer.read(16385))
async def run(root):
 context,_=owner(root,request.modality)
 context=replace(context,session=request)
 sdk=SimulatedSDK()
 async def start(**options):
  did=request.camera_id
  await sdk.instance.callbacks['raw_video'](did,b'SIMULATED',1,1,0)
  await sdk.instance.callbacks['raw_audio'](did,b'SIMULATED',1,1,0,SimpleNamespace(name='AUDIO_OPUS'))
  await sdk.instance.callbacks['video'](did,np.full((200,320,3),127,np.uint8),1,0,1,1)
  await sdk.instance.callbacks['audio'](did,np.full(32000,3277,np.int16),1,0,1,1)
 sdk.instance.start_async=start
 result=await read_camera_recognition(request,sdk,codec_quality='SIMULATED-LOW',_owner_context=context,sleep=immediate)
 assert sdk.calls[-1]=='deinit'
 return result
with tempfile.TemporaryDirectory(prefix='SIMULATED-recognition-') as directory:
 result=asyncio.run(run(Path(directory).resolve()))
sys.stdout.write(result.model_dump_json())
'''
    probe = RecognitionProbe(settings(tmp_path), launcher=lambda: launch(child), rss_reader=zero_rss)
    await probe.start_owner(context)
    await probe.task
    result = probe.private_result(context)
    assert result is not None and result.status.state == 'matched'
    assert result.observation.modality == modality and result.observation.embedding_produced
    assert result.observation.provenance == context.provenance and result.observation.quality.qualified
    assert result.observation.person_association == 'unavailable' and not result.status.action_authorized
    assert probe.process is None
    await probe.stop()


@pytest.mark.asyncio
async def test_real_child_pending_native_call_uses_uncertain_exit_and_parent_reaps(tmp_path):
    context, _ = owner(tmp_path)
    # Include cold NumPy/module startup in a fresh interpreter before the
    # synthetic native call begins; keep the child and parent deadlines finite.
    context = replace(context, session=context.session.model_copy(update={'deadline_monotonic': time.monotonic()+3.5}))
    project = Path(__file__).parents[1]
    owned_root = tmp_path/'SIMULATED-owned-native-hang'
    owned_root.mkdir()
    child = 'import sys\nsys.path[:0]=' + repr([str(project), str(project/'tests')]) + '\nROOT=' + repr(str(owned_root)) + '\n' + '''
import asyncio,importlib.abc,os,tempfile,threading
from dataclasses import replace
from pathlib import Path
class Fence(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in {'cv2','sherpa_onnx','miot','torch','onnxruntime'}: raise AssertionError('native forbidden')
sys.meta_path.insert(0,Fence())
import numpy as np
from test_homeplane_recognition_consumer import owner
from test_homeplane_native_media import SimulatedSDK,immediate
from homeplane.recognition_contracts import RecognitionSession
from homeplane.media_contracts import NativeMediaConnection
import homeplane.native_media_worker as worker
from homeplane.recognition_consumer import _OWNED_RECOGNITION_CALLS
request=RecognitionSession.model_validate_json(sys.stdin.buffer.read(16385))
context,backend=owner(Path(ROOT).resolve())
context=replace(context,session=request)
release=threading.Event()
def pending(image):
 release.wait()
backend.detector.detect=pending
sdk=SimulatedSDK()
async def start(**options):
 did=request.camera_id
 await sdk.instance.callbacks['raw_video'](did,b'SIMULATED',1,1,0)
 await sdk.instance.callbacks['video'](did,np.full((200,320,3),127,np.uint8),1,0,1,1)
sdk.instance.start_async=start
async def synthetic_probe(config):
 return await worker.read_camera_recognition(request,sdk,codec_quality='SIMULATED-LOW',_owner_context=context,sleep=immediate)
worker.probe=synthetic_probe
real_exit=os._exit
def checked_exit(code):
 pending=any(c._call is not None and not c._call.done() for c in _OWNED_RECOGNITION_CALLS.values())
 real_exit(77 if pending and 'deinit' not in sdk.calls else 78)
worker.os._exit=checked_exit
result=asyncio.run(worker.worker_probe(NativeMediaConnection(camera_id=request.camera_id),os.dup(sys.stdout.fileno())))
real_exit(79)
'''
    processes = []
    async def spawn():
        process = await launch(child)
        processes.append(process)
        return process
    probe = RecognitionProbe(settings(tmp_path), launcher=spawn, rss_reader=zero_rss)
    await probe.start_owner(context)
    await asyncio.wait_for(probe.task, 9)
    # Test-only exit code 77 confirms the existing os._exit branch saw a live
    # native future and retained SDK owner; no destructor/finalizer was run.
    assert processes[0].returncode == 77 and probe.process is None
    assert probe.report.reason == 'worker_timeout' and probe.result is None
