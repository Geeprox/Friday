"""Independent-review effective-deadline and same-frame provenance regressions."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from test_consumer_tracking_integration import configure, send
from tracking_fixtures import scope as scope

from homeplane.people.clustering import digest
from homeplane.people_feature_bridge import observation_refs, worker_face_input
from homeplane.recognition_consumer import _OWNED_RECOGNITION_CALLS
from homeplane.recognition_feature_handoff import feature_observation_id


def assert_revoked(consumer):
    assert consumer._tracker._closed and not consumer._tracker._tracks and consumer._tracker._latest is None
    assert consumer._tracker._timing._previous is None
    assert consumer._feature_sink._revoked and not consumer._feature_sink._features
    assert consumer._feature_message is None


def test_people_finish_beyond_effective_window_cannot_publish(scope):
    async def scenario():
        session = scope[0].model_copy(update={'deadline_monotonic': 100., 'seconds': 2.})
        consumer, catalog, owner, clock, provider = configure(scope, session=session)
        try:
            await send(consumer, clock)
            original = owner.operation

            def finish_late(features):
                result = original(features)
                clock.now = 12.1
                return result

            owner.operation = finish_late
            result = await consumer.finish()
            assert result.status.state == result.status.reason == 'expired'
            assert result.schema_version == 2 and consumer._call.done()
            assert_revoked(consumer)
            assert not catalog.page().samples and provider.inference_calls == 0
            output = Path(__file__).resolve().parents[1]/'.artifacts/test-evidence/finish-window-after.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps({'effective_deadline': 12., 'operation_completed_at': clock.now,
                'session_deadline': 100., 'result_state': result.status.state,
                'feature_message_published': False, 'parent_catalog_samples': 0,
                'tracking_and_feature_state_cleared': True, 'owned_operation_done': consumer._call.done(),
                'private_schema_version': result.schema_version, 'inference_calls': 0}, indent=2)+'\n')
        finally:
            await consumer.close()
            catalog.connection.close()
        assert not _OWNED_RECOGNITION_CALLS
    asyncio.run(scenario())


def test_cancelled_video_waiter_finish_rechecks_window_after_actual_drain(scope, monkeypatch):
    import scipy.optimize
    real = scipy.optimize.linear_sum_assignment
    entered, release = threading.Event(), threading.Event()

    async def scenario():
        session = scope[0].model_copy(update={'deadline_monotonic': 100., 'seconds': 2.})
        consumer, catalog, owner, clock, provider = configure(scope, session=session)
        operation_calls = []

        def blocked(matrix):
            entered.set()
            assert release.wait(2.)
            output = real(matrix)
            clock.now = 12.1
            return output

        def forbidden_finish(features):
            operation_calls.append(True)
            raise AssertionError('expired_work_must_not_submit_people_finish')

        try:
            await send(consumer, clock)
            owner.operation = forbidden_finish
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
            finishing = asyncio.create_task(consumer.finish())
            await asyncio.sleep(.01)
            assert consumer._call is owned and not owned.done() and not finishing.done()
            assert provider.closed == 0 and id(consumer) in _OWNED_RECOGNITION_CALLS
            release.set()
            result = await finishing
            assert owned.done() and result.status.state == 'expired' and not operation_calls
            assert_revoked(consumer)
            assert not catalog.page().samples
            output = Path(__file__).resolve().parents[1]/'.artifacts/test-evidence/finish-drain-after.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps({'effective_deadline': 12., 'drain_completed_at': clock.now,
                'session_deadline': 100., 'result_state': result.status.state,
                'same_pending_call_owned_until_done': True, 'people_finish_submissions': len(operation_calls),
                'feature_message_published': False, 'parent_catalog_samples': 0,
                'tracking_and_feature_state_cleared': True, 'actual_scipy_assignment': True,
                'private_schema_version': result.schema_version}, indent=2)+'\n')
        finally:
            release.set()
            await consumer.close()
            catalog.connection.close()
        assert not _OWNED_RECOGNITION_CALLS and provider.closed == 1
    asyncio.run(scenario())


def test_same_frame_slots_share_refs_and_worker_parent_agree(scope):
    async def scenario():
        consumer, catalog, owner, clock, _ = configure(scope)
        try:
            await send(consumer, clock)
            await send(consumer, clock, .3)
            features = tuple(consumer._feature_sink._features.values())
            inputs = {f.observation.observation_id: worker_face_input(consumer.session, f) for f in features}
            refs = {(f.observation.frame_sequence, f.observation.face_slot): observation_refs(consumer.session,
                    f.observation) for f in features}
            for seq in (0, 1):
                assert refs[seq, 0][0] == refs[seq, 1][0] and refs[seq, 0][1] == refs[seq, 1][1]
                assert refs[seq, 0][3] != refs[seq, 1][3]
                old_slot_zero = digest(['friday-feature-frame/1', consumer.session.camera_id, '0',
                    consumer.session.owner_operation, feature_observation_id(consumer.session, seq, 0),
                    consumer.context.provenance.model_dump_json()])
                assert refs[seq, 0][1] == old_slot_zero
            assert refs[0, 0][1] != refs[1, 0][1]
            await consumer.finish()
            page = owner.apply(catalog, consumer._feature_message)
            assert len({s.frame_ref for s in page.samples}) == len({s.source_digest for s in page.samples}) == 2
            assert len({s.sample_id for s in page.samples}) == 4
            for sample in page.samples:
                incoming = inputs[sample.observation_ref]
                assert (sample.source_digest, sample.frame_ref, sample.sample_id, sample.face_slot,
                        sample.camera_ref, sample.track_ref) == (incoming.source_digest, incoming.frame_ref,
                        incoming.sample_id, incoming.face_slot, incoming.camera_ref, incoming.track_ref)
            assert not page.people and not page.voice_links and not page.action_authorized
            output = Path(__file__).resolve().parents[1]/'.artifacts/test-evidence/consumer-review-after.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps({'same_frame_shared_refs': True, 'slot_zero_frame_path_preserved': True,
                'next_frame_distinct': True, 'worker_parent_agree': True, 'distinct_frame_refs': 2,
                'distinct_source_digests': 2, 'distinct_sample_ids': 4, 'private_schema_version': 2,
                'named_people': 0, 'action_authorized': False}, indent=2)+'\n')
        finally:
            await consumer.close()
            catalog.connection.close()
    asyncio.run(scenario())
