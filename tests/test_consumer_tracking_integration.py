"""Actual consumer/executor, tracker, People modules, IPC and parent catalog.

Only box/vector/clock fixtures are supplied. No SDK/provider/model inference.
The People partition API is an explicitly declared synthetic fixture
all SciPy
assignment calls delegate to the approved installed extension.
"""

import asyncio
import json
import math
import sqlite3
import threading
from pathlib import Path

import pytest
from tracking_fixtures import detection, quality, vector
from tracking_fixtures import scope as scope

from homeplane.people.catalog import PeopleCatalog, PeopleError
from homeplane.people_feature_bridge import SyntheticPeopleBatchOwner
from homeplane.recognition.contracts import Embedding, VerificationPolicy
from homeplane.recognition.face import FaceQuality
from homeplane.recognition_consumer import (
    _OWNED_RECOGNITION_CALLS,
    RecognitionConsumer,
    SuppliedFaceBatch,
    SyntheticOwnerContext,
)
from homeplane.recognition_contracts import RecognitionResult, RecognitionStatus
from homeplane.recognition_feature_protocol import (
    FeatureMetadataMessage,
    RecognitionWorkerResult,
    encode_worker_output,
    parse_worker_output,
)
from homeplane.recognition_timing import CaptureStamp


class NoInference:
    def __init__(self):
        self.closed = 0
        self.inference_calls = 0

    def detect(self, *args):
        self.inference_calls += 1
        raise AssertionError("inference_forbidden")

    embed = detect

    def close(self):
        self.closed += 1


class FixturePartition:
    """The previously declared optional clustering API fixture, no library fit."""
    def __init__(self, **kwargs):
        assert kwargs['metric'] == 'cosine' and kwargs['linkage'] == 'complete'

    def fit_predict(self, vectors):
        return [0 if v[0] > .5 else 1 for v in vectors]


class Clock:
    def __init__(self):
        self.now = 10.

    def __call__(self):
        return self.now


def configure(scope, *, threshold=.9, session=None):
    session = session or scope[0]
    clock = Clock()
    catalog = PeopleCatalog(sqlite3.connect(':memory:', check_same_thread=False))
    policy = VerificationPolicy('face', scope[1].embedding_model_sha256, threshold, .04,
                                True, 'synthetic_tracking_fixture')
    owner = SyntheticPeopleBatchOwner.from_catalog(catalog, session, scope[1], policy,
        _synthetic_consent=True, _cluster_factory=FixturePartition, clock=clock)
    provider = NoInference()
    context = SyntheticOwnerContext(session, provider, FaceQuality(True, True, True), scope[1], policy,
        _people_owner=owner, _supplied_faces=True)
    consumer = RecognitionConsumer(_owner_context=context, clock=clock)
    assert consumer.active
    return consumer, catalog, owner, clock, provider


async def send(consumer, clock, t=0., *, values=None, capture=True, source=1790000000000,
               shot_id=None, did=None, channel=0):
    clock.now = 10.+t
    values = values if values is not None else (detection(.2, embedding=vector()),
                                               detection(.6, embedding=vector(1)))
    stamp = CaptureStamp(500.+t, 'synthetic_capture_A') if capture else None
    await consumer.video(did or consumer.session.camera_id, SuppliedFaceBatch(tuple(values), stamp, shot_id), channel,
                         source_timestamp=source)


def test_composed_consumer_scipy_people_ipc_catalog_with_multiface_and_truth(scope):
    async def scenario():
        consumer, catalog, owner, clock, provider = configure(scope)
        try:
            await send(consumer, clock)
            executor = consumer._executor
            await send(consumer, clock, .3, values=(detection(.61, embedding=vector(1)),
                                                   detection(.21, embedding=vector())))
            assert consumer._executor is executor and len(_OWNED_RECOGNITION_CALLS) == 1
            observations = consumer._feature_sink.metadata_observations()
            assert len(observations) == 4 and len({o.observation_id for o in observations}) == 4
            assert {(o.frame_sequence, o.face_slot) for o in observations} == {(0, 0), (0, 1), (1, 0), (1, 1)}
            by_frame_slot = {(o.frame_sequence, o.face_slot): o for o in observations}
            assert by_frame_slot[0, 0].track_id == by_frame_slot[1, 1].track_id
            assert by_frame_slot[0, 1].track_id == by_frame_slot[1, 0].track_id
            assert {o.tracking for o in observations} == {'tentative', 'confirmed'}
            result = await consumer.finish()
            message = consumer._feature_message
            assert result.schema_version == message.schema_version == 2
            wire = encode_worker_output(RecognitionWorkerResult(result, message)).encode()
            assert len(wire) <= 8192 and len(wire.split(b'\n')) == 2
            assert all(token not in wire for token in (b'"values"', b'"embedding"', b'"box"', b'"similarity"'))
            parsed = parse_worker_output(wire, *scope, allow_features=True)
            page = owner.apply(catalog, parsed.features)
            assert len(page.samples) == 4 and sorted(len(c.sample_ids) for c in page.clusters) == [2, 2]
            assert len({s.track_ref for s in page.samples}) == 2 and all(s.track_ref for s in page.samples)
            assert {s.face_slot for s in page.samples} == {0, 1}
            assert {s.observed_monotonic for s in page.samples} == {10., 10.3}
            assert all(s.source_clock_domain == 'unverified' for s in page.samples)
            assert not page.people and not page.voice_links and all(c.person_id is None for c in page.clusters)
            assert page.action_authorized is False and page.security_authentication is False
            assert page.active_speaker_dependency == page.tracking_dependency == 'unavailable'
            assert all(o.timing.capture_evidence == 'synthetic_owner_fixture'
                       and o.timing.av_sync == 'unavailable' and o.timing.source_clock_domain == 'unverified'
                       for o in observations)
            assert {o.timing.capture_monotonic for o in observations} == {500., 500.3}
            bodies = catalog.connection.execute('SELECT body FROM people_state').fetchone()[0]
            assert all(token not in bodies for token in ('"embedding"', '"values"', '"box"', '1790000000000'))
            assert not consumer._feature_sink._features and not consumer._tracker._tracks
            output = Path(__file__).resolve().parents[1]/'.artifacts/test-evidence'/'composed-path.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps({'private_schema_version': 2, 'observations': len(observations),
                'distinct_track_refs': 2, 'face_slots': [0, 1], 'feature_output_bytes': len(wire),
                'same_existing_executor': True, 'consumer_provider_inference_calls': provider.inference_calls,
                'people_clustering_api': 'declared synthetic partition, actual People module validation',
                'catalog_samples': len(page.samples), 'catalog_candidate_groups': len(page.clusters),
                'named_people': len(page.people), 'voice_links': len(page.voice_links),
                'source_clock': 'unverified', 'capture_evidence': 'synthetic_owner_fixture',
                'av_sync': 'unavailable', 'action_authorized': False}, indent=2)+'\n')
        finally:
            await consumer.close()
            catalog.connection.close()
        assert not _OWNED_RECOGNITION_CALLS and provider.closed == 1 and provider.inference_calls == 0
    asyncio.run(scenario())


def test_tracking_continues_after_feature_cap_and_drops_too_soon_wrong_scope(scope):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            await send(consumer, clock, did='wrong-camera')
            await send(consumer, clock, channel=1)
            assert consumer._tracking_frames == 0 and consumer._executor is None
            await send(consumer, clock)
            await send(consumer, clock, .01)
            assert consumer._tracking_frames == 1
            await send(consumer, clock, .3)
            assert consumer._feature_index == 4
            await send(consumer, clock, .6)
            assert consumer._tracking_frames == 3 and consumer._tracker._frames == 3
            assert consumer._feature_index == 4 and len(consumer._feature_sink._features) == 4
            await consumer.finish()
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_three_sample_per_track_cap_and_unverified_capture_no_fabrication(scope):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            for t in (0., .3, .6, .9):
                await send(consumer, clock, t, values=(detection(embedding=vector()),))
            assert consumer._feature_index == 3 and consumer._tracking_frames == 4
            await consumer.finish()
            assert len(consumer._feature_message.observations) == 3
        finally:
            await consumer.close()
            catalog.connection.close()
        consumer, catalog, owner, clock, _ = configure(scope)
        try:
            await send(consumer, clock, capture=False)
            await send(consumer, clock, .3, capture=False)
            await consumer.finish()
            assert all(o.tracking == 'unavailable' and o.track_id is None
                and o.timing.capture_monotonic is None and o.timing.capture_evidence == 'unavailable'
                for o in consumer._feature_message.observations)
            page = owner.apply(catalog, consumer._feature_message)
            assert all(s.track_ref is None and s.source_clock_domain == 'unverified' for s in page.samples)
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_capture_loss_and_source_reset_break_ids_without_extending_budgets(scope):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            await send(consumer, clock, values=(detection(embedding=vector()),), source=9)
            old = consumer._tracker._tracks[0].track_id
            await send(consumer, clock, .1, capture=False, values=(detection(embedding=vector()),))
            assert not consumer._tracker._tracks and consumer._tracker._created == 1
            await send(consumer, clock, .3, values=(detection(embedding=vector()),), source=8)
            new = consumer._tracker._tracks[0].track_id
            assert old != new and consumer._tracker._created == 2
            await send(consumer, clock, .6, values=(detection(embedding=vector()),), source=7)
            assert consumer._tracker._tracks[0].track_id != new and consumer._tracker._created == 3
            assert consumer._tracking_frames == 4
            await consumer.finish()
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_box_continuity_does_not_name_or_force_people_identity(scope):
    async def scenario():
        consumer, catalog, owner, clock, _ = configure(scope, threshold=.99)
        try:
            await send(consumer, clock, values=(detection(embedding=vector()),))
            old = consumer._tracker._tracks[0].track_id
            angle = .4
            changed = Embedding('face', 'a'*64, (math.cos(angle), math.sin(angle))+(0.,)*126)
            await send(consumer, clock, .3, values=(detection(embedding=changed),))
            assert consumer._tracker._tracks[0].track_id == old
            await consumer.finish()
            page = owner.apply(catalog, consumer._feature_message)
            assert len({s.track_ref for s in page.samples}) == 1
            assert len(page.clusters) == 2 and all(c.state == 'ambiguous' for c in page.clusters)
            assert not page.people and not page.voice_links and not page.action_authorized
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_geometric_tracking_without_vectors_has_no_people_features(scope):
    async def scenario():
        consumer, catalog, _, clock, provider = configure(scope)
        try:
            await send(consumer, clock, values=(detection(),))
            await send(consumer, clock, .3, values=(detection(.21),))
            assert consumer._tracker._tracks[0].hits == 2
            result = await consumer.finish()
            assert result.status.state == 'unknown' and consumer._feature_message is None
            assert result.observation.embedding_produced is False
            assert not catalog.page().samples and not catalog.page().people and provider.inference_calls == 0
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_disabled_consumer_and_wrong_model_quality_never_activate_inference(scope):
    async def scenario():
        disabled = RecognitionConsumer()
        await disabled.video('synthetic-camera', SuppliedFaceBatch((detection(),)), 0)
        assert disabled.status().state == 'disabled' and disabled._executor is None
        await disabled.close()
        consumer, catalog, _, clock, provider = configure(scope)
        try:
            bad = (detection(embedding=vector(sha='c'*64)),
                   detection(.6, embedding=vector(1), q=quality(qualified=False, pose_ok=False)))
            await send(consumer, clock, values=bad)
            assert consumer._feature_index == 0
            result = await consumer.finish()
            assert result.status.state == 'unknown' and consumer._feature_message is None
            assert result.observation.embedding_produced is False
            assert not catalog.page().samples and provider.inference_calls == 0
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cause', ['stopped', 'configuration_changed', 'stale_generation'])
def test_revocation_before_finish_has_no_feature_publication(scope, cause):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            await send(consumer, clock)
            consumer.invalidate(cause)
            result = await consumer.finish()
            assert result.status.reason == cause and consumer._feature_message is None
            assert not consumer._feature_sink._features and not consumer._tracker._tracks
            assert not catalog.page().samples
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_single_finite_window_and_frame_cap(scope):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope,
            session=scope[0].model_copy(update={'deadline_monotonic': 100., 'seconds': 2.}))
        try:
            await send(consumer, clock)
            await send(consumer, clock, 2.1)
            assert consumer.status().state == 'expired' and not consumer._tracker._tracks
        finally:
            await consumer.close()
            catalog.connection.close()
        consumer, catalog, _, clock, _ = configure(scope,
            session=scope[0].model_copy(update={'deadline_monotonic': 100., 'seconds': 2.}))
        try:
            await send(consumer, clock)
            clock.now = 12.1
            result = await consumer.finish()
            assert result.status.state == 'expired' and consumer._feature_message is None
            assert not consumer._tracker._tracks and not consumer._feature_sink._features
        finally:
            await consumer.close()
            catalog.connection.close()
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            for i in range(129):
                await send(consumer, clock, i*.06, values=(detection(embedding=vector()),))
            assert consumer._tracking_frames == 128 and consumer.status().state == 'expired'
            assert consumer._feature_index == 3 and not consumer._tracker._tracks
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_cancelled_waiter_retains_call_until_completion_no_late_admission(scope, monkeypatch):
    import scipy.optimize
    real = scipy.optimize.linear_sum_assignment
    entered, release = threading.Event(), threading.Event()

    def blocked(matrix):
        entered.set()
        assert release.wait(2.)
        return real(matrix)

    async def scenario():
        consumer, catalog, _, clock, provider = configure(scope)
        try:
            await send(consumer, clock)
            monkeypatch.setattr(scipy.optimize, 'linear_sum_assignment', blocked)
            task = asyncio.create_task(send(consumer, clock, .3))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            owned = consumer._call
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not owned.done() and id(consumer) in _OWNED_RECOGNITION_CALLS
            consumer.invalidate('stopped')
            close_task = asyncio.create_task(consumer.close())
            await asyncio.sleep(.01)
            assert not close_task.done() and provider.closed == 0
            release.set()
            await close_task
            assert owned.done() and provider.closed == 1 and not consumer._feature_sink._features
            assert not consumer._tracker._tracks and consumer._feature_message is None
        finally:
            release.set()
            await consumer.close()
            catalog.connection.close()
        assert not _OWNED_RECOGNITION_CALLS
    asyncio.run(scenario())


def test_parent_people_revision_fence_and_v1_rejection(scope):
    async def scenario():
        consumer, catalog, owner, clock, _ = configure(scope)
        try:
            await send(consumer, clock)
            await send(consumer, clock, .3)
            result = await consumer.finish()
            message = consumer._feature_message
            raw_result = result.model_dump(mode='json')
            raw_result['schema_version'] = 1
            with pytest.raises(ValueError):
                RecognitionResult.model_validate_json(json.dumps(raw_result))
            raw_result.pop('schema_version')
            with pytest.raises(ValueError, match='private_recognition_version_unavailable'):
                parse_worker_output(json.dumps(raw_result).encode(), *scope)
            raw_message = message.model_dump(mode='json')
            raw_message.pop('schema_version')
            with pytest.raises(ValueError):
                FeatureMetadataMessage.model_validate_json(json.dumps(raw_message))
            raw_message['schema_version'] = 1
            with pytest.raises(ValueError):
                FeatureMetadataMessage.model_validate_json(json.dumps(raw_message))
            catalog.connection.execute("UPDATE people_state SET body=json_set(body,'$.revision',1)")
            catalog.connection.commit()
            with pytest.raises(PeopleError, match='people_revision_conflict'):
                owner.apply(catalog, message)
            assert not catalog.page().samples
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_multiface_ids_timing_and_capture_provenance_forgery_rejected(scope):
    async def scenario():
        consumer, catalog, _, clock, _ = configure(scope)
        try:
            await send(consumer, clock)
            await consumer.finish()
            original = consumer._feature_message.model_dump(mode='json')
            for field, bad in [('face_slot', 7), ('frame_sequence', 127)]:
                changed = json.loads(json.dumps(original))
                changed['observations'][0][field] = bad
                with pytest.raises(ValueError):
                    FeatureMetadataMessage.model_validate_json(json.dumps(changed))
            changed = json.loads(json.dumps(original))
            changed['observations'][0]['timing']['observed_monotonic'] += .01
            with pytest.raises(ValueError, match='feature_frame_timing_mismatch'):
                FeatureMetadataMessage.model_validate_json(json.dumps(changed))
            changed = json.loads(json.dumps(original))
            changed['observations'][0]['timing']['capture_evidence'] = 'unavailable'
            with pytest.raises(ValueError):
                FeatureMetadataMessage.model_validate_json(json.dumps(changed))
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())


def test_schema_payloads_and_public_contract_semantics_unchanged():
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root/'docs/recognition-private-result.schema.json').read_text()) == RecognitionResult.model_json_schema()
    assert json.loads((root/'docs/people-feature-message.schema.json').read_text()) == FeatureMetadataMessage.model_json_schema()
    original = root / 'docs/recognition-public-status.schema.json'
    assert json.loads(original.read_text()) == RecognitionStatus.model_json_schema()
