"""Real People module over synthetic provider batches and private metadata IPC."""
import ast
import asyncio
import hashlib
import importlib.abc
import json
import math
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pytest
from test_homeplane_recognition_consumer import launch, owner, settings, zero_rss

from homeplane.api import create_app
from homeplane.media_probe import RecognitionProbe
from homeplane.people.catalog import PeopleCatalog, PeopleError
from homeplane.people.contracts import Correct, Name, Split
from homeplane.people_feature_bridge import SyntheticPeopleBatchOwner, qualify_people_catalog
from homeplane.recognition_consumer import _OWNED_RECOGNITION_CALLS, RecognitionConsumer
from homeplane.recognition_contracts import RecognitionStatus, observation_id
from homeplane.recognition_feature_handoff import FeatureHandoffError
from homeplane.recognition_feature_protocol import (
    FeatureMetadataMessage,
    RecognitionWorkerResult,
    encode_worker_output,
    parse_worker_output,
)


class NativeFence(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'cv2', 'sherpa_onnx', 'miot', 'torch', 'onnxruntime'}:
            raise AssertionError('real_native_import_forbidden')


@pytest.fixture(autouse=True)
def fence():
    value = NativeFence()
    sys.meta_path.insert(0, value)
    yield
    sys.meta_path.remove(value)


class FixturePartition:
    """Declared fixture partitions at the existing optional library API boundary."""
    def __init__(self, **kwargs):
        assert kwargs['metric'] == 'cosine' and kwargs['linkage'] == 'complete'
        assert kwargs['n_clusters'] is None and kwargs['compute_full_tree'] is True
    def fit_predict(self, vectors):
        return [0 if v[0] > .5 else 1 for v in vectors]


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now


def catalog(path=':memory:'):
    return PeopleCatalog(sqlite3.connect(path, check_same_thread=False))


def configure(tmp_path, people, *, clock=None, factory=FixturePartition):
    context, cv = owner(tmp_path, candidates=False)
    if clock is not None:
        context = replace(context, session=context.session.model_copy(update={'deadline_monotonic': 4.0}))
    plan = SyntheticPeopleBatchOwner.from_catalog(people, context.session, context.provenance, context.policy,
        _synthetic_consent=True, _cluster_factory=factory, clock=clock or time.monotonic)
    angles = iter([0.0, .04, 1.40, 1.44])
    def feature(image):
        angle = next(angles)
        return np.array([(math.cos(angle), math.sin(angle))+(0.0,)*126], np.float32)
    cv.recognizer.feature = feature
    return replace(context, _people_owner=plan), cv


async def batch(tmp_path, people, *, count=4, factory=FixturePartition):
    clock = Clock()
    context, cv = configure(tmp_path, people, clock=clock, factory=factory)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    for index in range(count):
        clock.now = index*.25
        await consumer.video(context.session.camera_id, np.full((200, 320, 3), 127, np.uint8), 0,
            source_timestamp=index+1)
    result = await consumer.finish()
    message = consumer._feature_message
    await consumer.close()
    return context, cv, consumer, result, message


@pytest.mark.asyncio
async def test_real_people_module_candidates_then_labels_and_corrections_after_discard(tmp_path, monkeypatch):
    path = tmp_path/'synthetic-people.sqlite'
    people = catalog(path)
    context, cv, consumer, result, message = await batch(tmp_path, people)
    assert cv.recognizer.calls == 4 and result.observation.observation_id == observation_id(context.session)
    assert message is not None and sorted(len(g.observation_ids) for g in message.metadata.groups) == [2, 2]
    assert all(g.state == 'candidate' for g in message.metadata.groups)
    assert not consumer._feature_sink._features and consumer._executor is None
    assert not _OWNED_RECOGNITION_CALLS
    def forbidden(*args, **kwargs):
        raise AssertionError('parent_must_not_receive_vectors_or_recluster')
    monkeypatch.setattr(people, 'reindex', forbidden)
    page = context._people_owner.apply(people, message)
    assert page.last_run.backend == 'synthetic-contract-fake' and page.last_run.input_count == 4
    assert page.tracking_dependency == page.active_speaker_dependency == 'unavailable'
    first, second = page.clusters
    page = people.name(Name(expected_revision=page.revision, cluster_id=first.cluster_id, name='合成人物 A'))
    first = next(c for c in page.clusters if c.person_id is not None)
    pid = first.person_id
    page = people.split(Split(expected_revision=page.revision, cluster_id=first.cluster_id,
        sample_ids=first.sample_ids[:1]))
    moved = second.sample_ids[0]
    page = people.correct(Correct(expected_revision=page.revision, sample_id=moved, operation='exclude'))
    assert next(s for s in page.samples if s.sample_id == moved).state == 'excluded'
    expected = page.model_dump()
    bodies = '\n'.join(row[0] for table in ('people_state', 'people_undo')
        for row in people.connection.execute(f'SELECT body FROM {table}'))
    assert all(token not in bodies for token in ('"values"', '"embedding"', '"template"', '"pixels"'))
    assert context.session.camera_id not in page.model_dump_json()
    assert all(s.track_ref is None for s in page.samples)
    people.connection.close()
    reopened = catalog(path)
    assert reopened.page().model_dump() == expected
    assert next(p for p in reopened.page().people if p.person_id == pid).name == '合成人物 A'
    assert not reopened.page().action_authorized and not reopened.page().embeddings_persisted
    reopened.connection.close()


@pytest.mark.asyncio
async def test_sample_spacing_cap_singleton_and_public_result_schema_unchanged(tmp_path):
    people = catalog()
    clock = Clock()
    context, cv = configure(tmp_path, people, clock=clock)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    image = np.full((200, 320, 3), 127, np.uint8)
    for value in (0.0, .01, .1, .25, .5, .75, 1.0, 1.5):
        clock.now = value
        await consumer.video(context.session.camera_id, image, 0)
    report = await consumer.finish()
    assert cv.recognizer.calls == 4 and len(consumer._feature_message.observations) == 4
    assert set(report.model_dump()) == {'schema_version', 'session', 'status', 'observation', 'media_persisted'}
    assert report.observation.track_id is None and report.observation.tracking == 'unavailable'
    wire = encode_worker_output(RecognitionWorkerResult(report, consumer._feature_message)).encode()
    assert len(wire) <= 8192 and len(wire.split(b'\n')) == 2
    assert b'"values"' not in wire and b'"embedding"' not in wire
    await consumer.close()
    people = catalog()
    context, _, _, _, message = await batch(tmp_path, people, count=1)
    assert message.metadata.groups[0].state == 'unknown'
    page = context._people_owner.apply(people, message)
    with pytest.raises(PeopleError, match='sample_review_required'):
        people.name(Name(expected_revision=page.revision, cluster_id=page.clusters[0].cluster_id, name='不应命名'))


@pytest.mark.asyncio
async def test_sampling_window_no_fabricated_track_and_model_namespace_separation(tmp_path):
    people = catalog()
    clock = Clock()
    context, cv = configure(tmp_path, people, clock=clock)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    image = np.full((200, 320, 3), 127, np.uint8)
    await consumer.video(context.session.camera_id, image, 0)
    clock.now = 2.1
    await consumer.video(context.session.camera_id, image, 0)
    await consumer.finish()
    assert cv.recognizer.calls == 1
    message = consumer._feature_message
    raw = message.model_dump(mode='json')
    raw['observations'][0]['track_id'] = 'not-a-qualified-track'
    with pytest.raises(ValueError):
        FeatureMetadataMessage.model_validate_json(json.dumps(raw))
    from homeplane.people_feature_bridge import observation_refs
    observation = message.observations[0]
    changed = observation.model_copy(update={'provenance': observation.provenance.model_copy(
        update={'embedding_model_sha256': '0'*64})})
    assert observation_refs(context.session, observation)[3] != observation_refs(context.session, changed)[3]
    await consumer.close()


@pytest.mark.asyncio
async def test_wrong_owner_consent_quality_model_and_simulation_have_no_features(tmp_path):
    people = catalog()
    context, cv = configure(tmp_path, people)
    with pytest.raises(FeatureHandoffError):
        SyntheticPeopleBatchOwner.from_catalog(people, context.session, context.provenance, context.policy)
    bad = replace(context, _people_owner=SyntheticPeopleBatchOwner.from_catalog(people,
        context.session.model_copy(update={'owner_operation': 'wrong'}), context.provenance, context.policy,
        _synthetic_consent=True, _cluster_factory=FixturePartition))
    consumer = RecognitionConsumer(_owner_context=bad)
    assert not consumer.active and consumer._feature_sink is None
    await consumer.close()
    context = replace(context, quality=type(context.quality)())
    consumer = RecognitionConsumer(_owner_context=context)
    await consumer.video(context.session.camera_id, np.full((200, 320, 3), 127, np.uint8), 0)
    assert cv.recognizer.calls == 0 and not consumer._feature_sink._features
    await consumer.finish()
    await consumer.close()
    assert people.page().revision == 0


@pytest.mark.asyncio
async def test_metadata_parser_and_catalog_fences_reject_wrong_binding_revision_and_exports(tmp_path):
    people = catalog()
    context, _, _, report, message = await batch(tmp_path, people)
    wire = encode_worker_output(RecognitionWorkerResult(report, message)).encode()
    assert parse_worker_output(wire, context.session, context.provenance, allow_features=True).features == message
    with pytest.raises(ValueError):
        parse_worker_output(wire, context.session, context.provenance)
    for field, value in [('generation', 2), ('owner_operation', 'wrong'), ('camera_id', 'wrong')]:
        raw = message.model_dump(mode='json')
        raw['session'][field] = value
        altered = report.model_dump_json().encode()+b'\n'+json.dumps(raw).encode()
        with pytest.raises(ValueError):
            parse_worker_output(altered, context.session, context.provenance, allow_features=True)
    for field in ('embedding', 'values', 'template', 'model_path'):
        raw = message.model_dump(mode='json')
        raw[field] = [1.0]*128
        with pytest.raises(ValueError):
            FeatureMetadataMessage.model_validate_json(json.dumps(raw))
    raw = message.model_dump(mode='json')
    raw['observations'][1]['timing']['observed_monotonic'] = raw['observations'][0]['timing']['observed_monotonic']
    with pytest.raises(ValueError, match='sampling_interval'):
        FeatureMetadataMessage.model_validate_json(json.dumps(raw))
    with pytest.raises(FeatureHandoffError, match='catalog_required'):
        context._people_owner.apply(catalog(), message)
    changed = message.model_copy(update={'metadata': message.metadata.model_copy(update={'base_people_revision': 1})})
    with pytest.raises(FeatureHandoffError):
        context._people_owner.apply(people, changed)
    assert people.page().revision == 0
    people.connection.set_authorizer(lambda action, *args:
        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE else sqlite3.SQLITE_OK)
    with pytest.raises(PeopleError, match='people_storage_unavailable'):
        context._people_owner.apply(people, message)
    people.connection.set_authorizer(None)
    assert people.page().revision == 0
    page = context._people_owner.apply(people, message)
    with pytest.raises(FeatureHandoffError):
        context._people_owner.apply(people, message)
    assert people.page().revision == page.revision


@pytest.mark.asyncio
async def test_actual_people_api_shared_guard_and_disabled_composition(tmp_path):
    people = catalog()
    context, _, _, _, message = await batch(tmp_path, people)
    page = context._people_owner.apply(people, message)
    app = create_app(db_path=tmp_path/'synthetic-traces.sqlite', settings_service=settings(tmp_path), people_catalog=people)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            before = await client.get('/api/people')
            assert before.json()['recognition_state'] == 'disabled'
            request = {'expected_revision': page.revision, 'cluster_id': page.clusters[0].cluster_id, 'name': '合成名字'}
            for headers in ({}, {'origin': 'http://evil.example'}, {'origin': 'http://testserver', 'sec-fetch-site':'cross-site'}):
                assert (await client.post('/api/people/name', json=request, headers=headers)).status_code == 403
            saved = await client.post('/api/people/name', json=request, headers={'origin':'http://testserver'})
            assert saved.status_code == 200 and saved.json()['people'][0]['name'] == '合成名字'
            assert not saved.json()['action_authorized']
            assert (await client.get('/api/settings/camera/recognition')).json() == RecognitionStatus().model_dump()
            assert app.state.camera_recognition.task is None and not app.state.core.adapter.managed_actions
            for path in ('/api/people/features', '/api/people/enroll', '/api/people/start'):
                assert (await client.post(path, json={}, headers={'origin':'http://testserver'})).status_code in {404, 405}
            asset = await client.get('/people-static/people.js')
            assert asset.status_code == 200 and 'mountPeople' in asset.text
            assert 'frame-ancestors' in asset.headers['content-security-policy']
    with pytest.raises(sqlite3.ProgrammingError):
        people.page()


def test_owner_catalog_dedicated_local_connection_and_ceiling(tmp_path):
    wrong_thread = PeopleCatalog(sqlite3.connect(':memory:'))
    with pytest.raises(PeopleError, match='cross_thread_connection'):
        qualify_people_catalog(wrong_thread)
    wrong_thread.connection.close()
    memory = catalog()
    qualify_people_catalog(memory)
    assert memory.connection.execute('PRAGMA max_page_count').fetchone()[0] == 4096
    mixed = catalog()
    mixed.connection.execute('CREATE TABLE family_media_items (id TEXT)')
    mixed.connection.commit()
    with pytest.raises(PeopleError, match='dedicated_connection'):
        qualify_people_catalog(mixed)
    path = tmp_path/'synthetic-private.sqlite'
    private = catalog(path)
    path.chmod(0o644)
    with pytest.raises(PeopleError, match='private_database'):
        qualify_people_catalog(private)
    path.chmod(0o600)
    qualify_people_catalog(private)
    assert private.connection.execute('PRAGMA journal_size_limit').fetchone()[0] == 1048576
    large_page = sqlite3.connect(':memory:', check_same_thread=False)
    large_page.execute('PRAGMA page_size=65536')
    large = PeopleCatalog(large_page)
    qualify_people_catalog(large)
    assert large.connection.execute('PRAGMA max_page_count').fetchone()[0] == 256


@pytest.mark.asyncio
async def test_source_consent_revoke_discards_retained_features_and_blocks_more_inference(tmp_path):
    people = catalog()
    clock = Clock()
    context, cv = configure(tmp_path, people, clock=clock)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    image = np.full((200, 320, 3), 127, np.uint8)
    await consumer.video(context.session.camera_id, image, 0)
    assert consumer._feature_sink._features and cv.recognizer.calls == 1
    context._people_owner.revoke()
    assert not consumer._feature_sink._features
    clock.now = .25
    await consumer.video(context.session.camera_id, image, 0)
    report = await consumer.finish()
    await consumer.close()
    assert cv.recognizer.calls == 1 and consumer._feature_message is None
    assert report.status.reason == 'stale_generation' and people.page().revision == 0


@pytest.mark.asyncio
async def test_revoke_while_real_people_adapter_runs_drops_late_metadata_and_retains_native_owner(tmp_path):
    started, release = threading.Event(), threading.Event()
    class Blocked(FixturePartition):
        def fit_predict(self, vectors):
            started.set()
            assert release.wait(3)
            return super().fit_predict(vectors)
    people = catalog()
    clock = Clock()
    context, _ = configure(tmp_path, people, clock=clock, factory=Blocked)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    for i in range(2):
        clock.now = i*.25
        await consumer.video(context.session.camera_id, np.full((200, 320, 3), 127, np.uint8), 0)
    task = asyncio.create_task(consumer.finish())
    while not started.is_set():
        await asyncio.sleep(.001)
    consumer.invalidate()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(consumer.close(), .02)
        assert id(consumer) in _OWNED_RECOGNITION_CALLS and not consumer._feature_sink._features
    finally:
        release.set()
        await consumer.close()
    assert consumer._feature_message is None and people.page().revision == 0


@pytest.mark.asyncio
async def test_absent_clustering_library_is_explicit_and_submits_no_catalog(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name.startswith('sklearn'):
            raise ImportError()
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', blocked)
    people = catalog()
    _, _, consumer, report, message = await batch(tmp_path, people, factory=None)
    assert report.status.state == 'unavailable' and message is None
    assert consumer._feature_sink._revoked and not consumer._feature_sink._features
    assert people.page().revision == 0


def test_shared_quality_ast_and_boundary_only_adds_people_prefix():
    # Exact approved AST fingerprints; independent of later repository history.
    expected = {'record_media_quality': '613ed905c7cd6741c748a456054ed7dd5e79c14ab11497edec88586e9dbd018e', 'local_boundary': '2de1eb50a4defc7b4360eb07cbb694c6bb7d495aa54b92342c6624c4253feb28'}
    current = Path('homeplane/api.py').read_text()
    for name, fingerprint in expected.items():
        node = next(n for n in ast.walk(ast.parse(current))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        assert hashlib.sha256(ast.dump(node, include_attributes=False).replace(', type_params=[]', '').encode()).hexdigest() == fingerprint
    assert hashlib.sha256(Path('homeplane/recognition_contracts.py').read_bytes()).hexdigest() == '649f45c667f15bccbcb6af5bc33a9d5adc1577d7a705779f16a7cfc13959b4ba'


def batch_child(*, hang=False):
    project = Path(__file__).parents[1]
    prefix = 'import sys\nsys.path[:0]=' + repr([str(project), str(project/'tests')]) + '\n'
    code = prefix + '''
import asyncio,importlib.abc,os,tempfile,time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
class Fence(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name.split('.')[0] in {'cv2','sherpa_onnx','miot','torch','onnxruntime'}: raise AssertionError('native forbidden')
sys.meta_path.insert(0,Fence())
import numpy as np
from test_people_feature_integration import configure,catalog
from test_homeplane_native_media import SimulatedSDK,immediate
from homeplane.recognition_contracts import RecognitionSession
from homeplane.people_feature_bridge import SyntheticPeopleBatchOwner
import homeplane.native_media_worker as worker
request=RecognitionSession.model_validate_json(sys.stdin.buffer.read(16385))
async def run(root):
 people=catalog()
 context,backend=configure(root,people)
 context=replace(context,session=request)
 plan=SyntheticPeopleBatchOwner.from_catalog(people,request,context.provenance,context.policy,
  _synthetic_consent=True,_cluster_factory=context._people_owner._factory)
 context=replace(context,_people_owner=plan)
 sdk=SimulatedSDK()
 async def start(**options):
  did=request.camera_id
  for index in range(4):
   if index: await asyncio.sleep(.26)
   await sdk.instance.callbacks['raw_video'](did,b'SIMULATED',index+1,1,0)
   await sdk.instance.callbacks['raw_audio'](did,b'SIMULATED',index+1,1,0,SimpleNamespace(name='AUDIO_OPUS'))
   await sdk.instance.callbacks['video'](did,np.full((200,320,3),127,np.uint8),index+1,0,1,1)
   await sdk.instance.callbacks['audio'](did,np.full(320,1000,np.int16),index+1,0,1,1)
 sdk.instance.start_async=start
 result=await worker.read_camera_recognition(request,sdk,codec_quality='SIMULATED-LOW',_owner_context=context,sleep=immediate)
 assert sdk.calls[-1]=='deinit'
 return result
with tempfile.TemporaryDirectory(prefix='SIMULATED-feature-pipeline-') as directory:
 result=asyncio.run(run(Path(directory).resolve()))
worker.publish_worker_result(result,os.dup(sys.stdout.fileno()))
'''
    if hang:
        code = code.replace('from test_people_feature_integration import configure,catalog', '''from test_people_feature_integration import configure,catalog,FixturePartition
import threading
release=threading.Event()
class Blocked(FixturePartition):
 def fit_predict(self,vectors):
  release.wait()
  return super().fit_predict(vectors)''')
        code = code.replace('configure(root,people)', 'configure(root,people,factory=Blocked)')
        # Run the same v2 signal/cancellation/uncertain-exit wrapper, not a second runtime.
        code = code.replace(" assert sdk.calls[-1]=='deinit'", "")
        old = '''with tempfile.TemporaryDirectory(prefix='SIMULATED-feature-pipeline-') as directory:
 result=asyncio.run(run(Path(directory).resolve()))
worker.publish_worker_result(result,os.dup(sys.stdout.fileno()))'''
        replacement = '''
from homeplane.media_contracts import NativeMediaConnection
from homeplane.recognition_consumer import _OWNED_RECOGNITION_CALLS
# A parent-owned synthetic directory is supplied by the test; os._exit intentionally skips finalizers.
root=Path(OWNED_ROOT).resolve()
async def synthetic_probe(config):
 return await run(root)
worker.probe=synthetic_probe
real_exit=os._exit
def checked_exit(code):
 pending=any(c._call is not None and not c._call.done() and c._feature_sink._revoked
  for c in _OWNED_RECOGNITION_CALLS.values())
 real_exit(77 if pending else 78)
worker.os._exit=checked_exit
asyncio.run(worker.worker_probe(NativeMediaConnection(camera_id=request.camera_id),os.dup(sys.stdout.fileno())))
real_exit(79)'''
        assert old in code
        code = code.replace(old, replacement)
    return code


@pytest.mark.asyncio
async def test_full_real_child_callbacks_actual_people_module_private_ipc_and_parent_catalog(tmp_path):
    people = catalog()
    context, _ = configure(tmp_path, people)
    processes = []
    async def spawn():
        process = await launch(batch_child())
        processes.append(process)
        return process
    probe = RecognitionProbe(settings(tmp_path), launcher=spawn, rss_reader=zero_rss, people_catalog=people)
    assert (await probe.start_owner(context)).state == 'running'
    await probe.task
    assert probe.report.state == 'ready' and probe.feature_message is not None
    assert probe.result.observation.observation_id == observation_id(context.session)
    assert sorted(len(c.sample_ids) for c in people.page().clusters) == [2, 2]
    assert people.page().last_run.backend == 'synthetic-contract-fake'
    assert processes[0].returncode == 0 and probe.process is None
    assert all(not p.action_authorized for p in people.page().people)
    page = people.page()
    named = people.name(Name(expected_revision=page.revision, cluster_id=page.clusters[0].cluster_id, name='子进程合成人物'))
    await probe.stop()
    assert people.page() == named and probe.status().state == 'disabled'


@pytest.mark.asyncio
async def test_real_child_catalog_revision_conflict_discards_late_partition(tmp_path):
    people = catalog()
    old_context, _, _, _, initial = await batch(tmp_path, people)
    page = old_context._people_owner.apply(people, initial)
    context, _ = configure(tmp_path, people)
    # Worker fixture starts from an empty metadata catalog; this intentionally fails the parent revision fence.
    probe = RecognitionProbe(settings(tmp_path), launcher=lambda: launch(batch_child()), rss_reader=zero_rss,
        people_catalog=people)
    await probe.start_owner(context)
    newer = people.name(Name(expected_revision=page.revision, cluster_id=page.clusters[0].cluster_id, name='保留纠正'))
    await probe.task
    assert probe.report.reason == 'worker_failed' and probe.feature_message is None
    assert people.page() == newer


@pytest.mark.parametrize('edit', [False, True])
@pytest.mark.asyncio
async def test_real_child_stop_or_settings_edit_revokes_and_reaps_without_catalog_write(tmp_path, edit):
    people = catalog()
    context, _ = configure(tmp_path, people)
    config = settings(tmp_path)
    processes = []
    async def spawn():
        process = await launch(batch_child())
        processes.append(process)
        return process
    probe = RecognitionProbe(config, launcher=spawn, rss_reader=zero_rss, people_catalog=people)
    await probe.start_owner(context)
    while probe.process is None:
        await asyncio.sleep(.001)
    if edit:
        config.update({'camera_id':'SIMULATED-other'})
        await probe.task
        assert probe.status().reason == 'configuration_changed'
    else:
        await probe.stop()
        assert probe.status().state == 'disabled'
    assert context._people_owner._revoked and people.page().revision == 0
    assert processes[0].returncode is not None and probe.process is None


@pytest.mark.asyncio
async def test_real_child_clustering_hang_uses_v2_uncertain_exit_and_reap(tmp_path):
    people = catalog()
    context, _ = configure(tmp_path, people)
    context = replace(context, session=context.session.model_copy(update={'deadline_monotonic':time.monotonic()+2.5}))
    context = replace(context, _people_owner=SyntheticPeopleBatchOwner.from_catalog(people, context.session,
        context.provenance, context.policy, _synthetic_consent=True, _cluster_factory=FixturePartition))
    root = tmp_path/'SIMULATED-hung-cluster'
    root.mkdir()
    script = 'OWNED_ROOT=' + repr(str(root)) + '\n' + batch_child(hang=True)
    processes = []
    async def spawn():
        process = await launch(script)
        processes.append(process)
        return process
    probe = RecognitionProbe(settings(tmp_path), launcher=spawn, rss_reader=zero_rss, people_catalog=people)
    await probe.start_owner(context)
    await asyncio.wait_for(probe.task, 7)
    assert processes[0].returncode == 77 and probe.process is None
    assert probe.report.reason == 'worker_timeout' and people.page().revision == 0
    assert probe.feature_message is None
