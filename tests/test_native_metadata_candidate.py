"""V3 synthetic protocol and actual constructors, never native/model acceptance."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from native_fixtures import qualified_slots, row, send, setup

from homeplane.media_probe import RecognitionProbe
from homeplane.native_media_worker import NativeCounter, denied_native_wire_request, read_camera_recognition
from homeplane.recognition.face import FaceQuality
from homeplane.recognition_consumer import (
    _OWNED_RECOGNITION_CALLS,
    NativeMetadataOwnerContext,
    RecognitionConsumer,
)
from homeplane.recognition_contracts import (
    FaceFrameProvenance,
    FaceSlotQuality,
    NativeFaceRequest,
    NativeFaceResult,
    ReceivedFrameTiming,
    RecognitionResult,
)
from homeplane.recognition_feature_protocol import (
    RecognitionWorkerResult,
    encode_worker_output,
    parse_worker_output,
)


def test_actual_opencv_slot_method_reuses_digest_quality_and_existing_single_face_guard():
    consumer, context, _, detector, recognizer = setup()
    image = np.full((240, 320, 3), 7, dtype=np.uint8)
    try:
        detections = context.provider.detect(image)
        assert context.provider.embed(image, detections, FaceQuality(True, True, True)).status.reason == 'single_face_required'
        first = context.provider.embed_slot(image, detections, 1, FaceQuality(True, True, True))
        assert first.embedding.simulated is True and first.status.state == 'ready'
        assert recognizer.rows == [180.] and detector.calls == 1
        image[0, 0, 0] = 8
        assert context.provider.embed_slot(image, detections, 0, FaceQuality(True, True, True)).status.reason == 'detection_frame_mismatch'
        assert recognizer.calls == 1
        for slot in (-1, 2, True, 0.5):
            assert context.provider.embed_slot(image, detections, slot, FaceQuality()).status.reason == 'invalid_face_slot'
    finally:
        asyncio.run(consumer.close())


def test_v3_actual_callback_consumer_slot_output_and_exact_roundtrip():
    async def scenario():
        consumer, context, clock, detector, recognizer = setup()
        try:
            counter = NativeCounter('synthetic-camera', clock=clock, _recognition=consumer)
            image = np.full((240, 320, 3), 7, dtype=np.uint8)
            await counter.video('synthetic-camera', image, 1234, 0, 1790000000300, 1790000000305)
            result = await consumer.finish()
            assert result.schema_version == 3 and result.session.simulated is True
            assert result.provenance.evidence == result.admission_kind == 'synthetic_contract_fixture'
            assert result.production_admission == result.face_to_voice == 'unavailable'
            assert result.status.state == 'ready' and recognizer.rows == [16., 180.]
            assert [s.face_slot for s in result.frame.slots] == [0, 1]
            assert len({s.observation_id for s in result.frame.slots}) == 2
            assert result.frame.slots[0].box == (.05, 16/240, .25, 80/240)
            assert result.frame.timing.received_monotonic == result.frame.timing.observation_monotonic == 10.
            assert result.frame.timing.received_clock_domain == 'worker_monotonic'
            assert result.frame.timing.source_timestamp == 1234
            assert result.frame.timing.source_clock_domain == 'unverified'
            assert result.frame.timing.capture_monotonic is None and result.frame.timing.av_sync == 'unavailable'
            assert all(s.tracking == 'unavailable' and s.track_id is None for s in result.frame.slots)
            assert np.all(detector.last_owned == 0) and np.all(image == 7)
            assert consumer._feature_sink is None and consumer._feature_message is None
            wire = encode_worker_output(RecognitionWorkerResult(result)).encode()
            parsed = parse_worker_output(wire, context.session, context.provenance)
            assert parsed.result == result and parsed.features is None and len(wire) <= 8192
            assert all(token not in wire for token in (b'"values"', b'"frame_digest"', b'synthetic-no-file'))
            assert result.status.action_authorized is result.status.security_authentication is False
            output = Path(__file__).resolve().parents[1]/'.artifacts/test-evidence/native-v3-composed-fixture.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps({'schema_version': 3, 'execution_evidence': 'synthetic_contract_fixture',
                'slots': 2, 'embedding_api_fixture_calls': recognizer.calls, 'source_clock': 'unverified',
                'received_at': 'decoded_callback_entry', 'received_clock_domain': 'worker_monotonic',
                'capture_clock': None, 'wire_bytes': len(wire),
                'vectors_exported': False, 'same_consumer_executor': True, 'owned_copy_zeroed': True,
                'native_model_inference': False, 'production_admission': 'unavailable',
                'real_camera_or_native_tracking_validated': False}, indent=2)+'\n')
        finally:
            await consumer.close()
        assert not _OWNED_RECOGNITION_CALLS and context.provider._detector is None
    asyncio.run(scenario())


def test_unknown_quality_still_emits_detection_slots_without_embedding_or_capture():
    async def scenario():
        consumer, _, clock, _, recognizer = setup(qualities=None)
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            assert result.status.state == 'unknown' and len(result.frame.slots) == 2
            assert all(s.quality.evidence == 'unavailable' and s.embedding_state == 'quality_unqualified'
                for s in result.frame.slots)
            assert recognizer.calls == consumer._native_attempts == 0
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_per_slot_budget_spacing_duplicate_and_source_regression_do_not_reset_budget():
    async def scenario():
        consumer, _, clock, _, recognizer = setup()
        try:
            await send(consumer, clock, source=9)
            await send(consumer, clock, .1, value=8, source=10)
            assert {s.embedding_state for s in consumer._native_frame.slots} == {'budget_exhausted'}
            await send(consumer, clock, .3, source=8)
            assert consumer._native_frame.timing.source_discontinuity
            assert {s.embedding_state for s in consumer._native_frame.slots} == {'duplicate'}
            await send(consumer, clock, .6, value=8, source=11)
            assert recognizer.calls == consumer._native_attempts == len(consumer._native_seen) == 4
            await send(consumer, clock, .9, value=9, source=12)
            assert {s.embedding_state for s in consumer._native_frame.slots} == {'budget_exhausted'}
            assert recognizer.calls == 4 and consumer._tracking_frames == 5
            await consumer.finish()
            assert not consumer._native_seen
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_eight_rows_produce_eight_slots_with_at_most_four_embeddings():
    async def scenario():
        consumer, _, clock, _, recognizer = setup(rows=tuple(row(10.+i*95., 10.) for i in range(8)))
        try:
            await send(consumer, clock, shape=(160, 800, 3))
            result = await consumer.finish()
            assert len(result.frame.slots) == 8 and recognizer.calls == 4
            assert [s.embedding_state for s in result.frame.slots] == ['produced']*4+['budget_exhausted']*4
            assert len(encode_worker_output(result).encode()) <= 8192
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_native_request_constructor_and_all_wire_owner_paths_deny_before_sdk():
    async def scenario():
        fixture, context, _, _, _ = setup()
        await fixture.close()
        session = context.session.model_copy(update={'simulated': False})
        provenance = FaceFrameProvenance(detector_model_sha256='b'*64, embedding_model_sha256='a'*64,
            evidence='native_unverified', artifact_evidence='configured_only')
        owner = NativeMetadataOwnerContext(session, context.provider, provenance, _fixture_consent=True)
        assert not owner.admits(session)
        consumer = RecognitionConsumer(_owner_context=owner)
        await consumer.video('synthetic-camera', np.zeros((1, 1, 3), np.uint8), 0, received_monotonic=10.)
        result = await consumer.finish()
        assert result.session.simulated is False and result.frame is None
        assert result.status.reason == 'owner_admission_unavailable' and consumer._executor is None
        request = NativeFaceRequest(schema_version=3, session=session, provenance=provenance)
        denied = denied_native_wire_request(request)
        assert parse_worker_output(encode_worker_output(denied).encode(), session, provenance).result == denied
        class NoSDK:
            def __getattr__(self, name):
                raise AssertionError('sdk_must_not_be_touched')
        returned = await read_camera_recognition(session, NoSDK(), codec_quality=None, _owner_context=owner)
        assert returned.status.reason == 'owner_admission_unavailable' and returned.schema_version == 3
        settings = SimpleNamespace(revision=1, config=SimpleNamespace(camera_id='synthetic-camera', camera_media_source='miot_sdk'))
        probe = RecognitionProbe(settings, clock=lambda: 10.)
        assert (await probe.start_owner(owner)).reason == 'owner_admission_unavailable' and probe.task is None
        await consumer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('version', [None, 1, 2, 3.0, True, '3'])
def test_v3_reader_and_request_reject_missing_or_wrong_version(version):
    async def scenario():
        consumer, context, clock, _, _ = setup(qualities=None)
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            data = result.model_dump(mode='json')
            request = NativeFaceRequest(schema_version=3, session=context.session, provenance=context.provenance).model_dump(mode='json')
            for value in (data, request):
                if version is None:
                    value.pop('schema_version')
                else:
                    value['schema_version'] = version
            with pytest.raises(ValueError):
                parse_worker_output(json.dumps(data).encode(), context.session, context.provenance)
            with pytest.raises(ValueError):
                NativeFaceRequest.model_validate_json(json.dumps(request))
            if version is not None:
                with pytest.raises(ValueError):
                    NativeFaceResult.model_validate_json(json.dumps(data))
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_v2_v3_and_fixture_native_labels_cannot_cross_or_export_features():
    async def scenario():
        consumer, context, clock, _, _ = setup()
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            wire = encode_worker_output(result).encode()
            from homeplane.recognition_contracts import RecognitionProvenance
            legacy = RecognitionProvenance(provider='opencv_yunet_sface', embedding_model_sha256='a'*64)
            with pytest.raises(ValueError):
                parse_worker_output(wire, context.session, legacy)
            with pytest.raises(ValueError):
                RecognitionResult.model_validate_json(wire)
            with pytest.raises(ValueError, match='native_people_features_unavailable'):
                encode_worker_output(RecognitionWorkerResult(result, object()))
            with pytest.raises(ValueError):
                parse_worker_output(wire+b'\n{}', context.session, context.provenance, allow_features=True)
            data = result.model_dump(mode='json')
            data['session']['simulated'] = data['status']['simulated'] = False
            with pytest.raises(ValueError):
                NativeFaceResult.model_validate_json(json.dumps(data))
            data = result.model_dump(mode='json')
            data['frame']['timing']['capture_monotonic'] = 10.
            with pytest.raises(ValueError):
                NativeFaceResult.model_validate_json(json.dumps(data))
            data = result.model_dump(mode='json')
            data['frame']['slots'][0]['track_id'] = 'invented_capture_track'
            with pytest.raises(ValueError):
                NativeFaceResult.model_validate_json(json.dumps(data))
        finally:
            await consumer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad_received', [None, True, 10**1000, 10.1, 9.5])
def test_bad_receive_metadata_closes_without_detector_work(bad_received):
    async def scenario():
        consumer, _, _, detector, _ = setup()
        try:
            await consumer.video('synthetic-camera', np.zeros((240, 320, 3), np.uint8), 0,
                received_monotonic=bad_received)
            assert not consumer.active and detector.calls == 0 and consumer._native_frame is None
            assert consumer._executor is None
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_duplicate_receive_and_invalid_quality_or_provider_errors_clear_private_state():
    async def scenario():
        for cause in ('duplicate_receive', 'quality_count', 'quality_failure'):
            consumer, context, clock, detector, _ = setup()
            try:
                await send(consumer, clock)
                if cause == 'duplicate_receive':
                    clock.now = 10.3
                    await consumer.video('synthetic-camera', np.zeros((240, 320, 3), np.uint8), 0, received_monotonic=10.)
                else:
                    def invalid_quality(image, detections):
                        if cause == 'quality_failure':
                            raise ValueError('synthetic_private_quality_detail')
                        return (FaceSlotQuality(),)
                    # Frozen private context replacement, never public wire injection.
                    from dataclasses import replace
                    consumer.context = replace(context, quality_source=invalid_quality)
                    await send(consumer, clock, .3, value=8)
                assert consumer.status().reason == 'native_failed' and consumer._native_frame is None
                assert not consumer._native_seen
                if detector.last_owned is not None:
                    assert np.all(detector.last_owned == 0)
                assert 'synthetic_private_quality_detail' not in encode_worker_output(await consumer.finish())
            finally:
                await consumer.close()
    asyncio.run(scenario())


def test_busy_cancelled_waiter_retains_single_call_zeroes_copy_and_never_publishes_late():
    async def scenario():
        consumer, context, clock, detector, recognizer = setup()
        entered, release = threading.Event(), threading.Event()
        recognizer.entered, recognizer.release = entered, release
        try:
            task = asyncio.create_task(send(consumer, clock))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            owned = consumer._call
            await send(consumer, clock, .1, value=8)
            assert consumer._call is owned and consumer._tracking_frames == detector.calls == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert id(consumer) in _OWNED_RECOGNITION_CALLS and not owned.done()
            closing = asyncio.create_task(consumer.close())
            await asyncio.sleep(.01)
            assert not closing.done() and context.provider._recognizer is recognizer
            release.set()
            await closing
            assert owned.done() and np.all(detector.last_owned == 0)
            assert consumer._native_frame is None and not consumer._native_seen
            assert recognizer.calls == 1 and context.provider._recognizer is None
        finally:
            release.set()
            await consumer.close()
        assert not _OWNED_RECOGNITION_CALLS
    asyncio.run(scenario())


def test_deadline_after_first_slot_stops_next_slot_and_finish_clears_output():
    async def scenario():
        consumer, _, clock, detector, recognizer = setup()
        recognizer.after = lambda: setattr(clock, 'now', 12.1)
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            assert result.status.state == 'expired' and result.frame is None
            assert recognizer.calls == 1 and consumer._native_attempts == 1
            assert not consumer._native_seen and np.all(detector.last_owned == 0)
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_frame_cap_and_too_soon_callback_do_not_extend_window():
    async def scenario():
        consumer, _, clock, detector, recognizer = setup(qualities=None, seconds=8.)
        try:
            await send(consumer, clock)
            await send(consumer, clock, .01)
            assert consumer._tracking_frames == 1
            for i in range(1, 129):
                await send(consumer, clock, i*.06)
            assert consumer._tracking_frames == detector.calls == 128 and recognizer.calls == 0
            result = await consumer.finish()
            assert result.status.state == 'expired' and result.frame is None
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_native_quality_and_timing_constructor_safety():
    assert not FaceSlotQuality().qualified
    with pytest.raises(ValueError):
        FaceSlotQuality(qualified=True, pose_ok=True, sharpness_ok=True, occlusion_ok=True)
    with pytest.raises(ValueError):
        FaceSlotQuality(evidence='native_measured', qualified=True, pose_ok=True, sharpness_ok=True, occlusion_ok=True)
    with pytest.raises(ValueError):
        ReceivedFrameTiming(received_monotonic=10., observation_monotonic=10.3)
    with pytest.raises(ValueError):
        FaceFrameProvenance(detector_model_sha256='b'*64, evidence='native_verified', artifact_evidence='configured_only')
    with pytest.raises(ValueError):
        FaceFrameProvenance(detector_model_sha256='b'*64, evidence='native_verified',
            artifact_evidence='verified_local_artifacts', runtime_version='synthetic_contract_fixture')


def test_both_v3_authoritative_schemas_match_actual_classes():
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root/'docs/native-face-result-v3.schema.json').read_text()) == NativeFaceResult.model_json_schema()
    assert json.loads((root/'docs/native-face-request-v3.schema.json').read_text()) == NativeFaceRequest.model_json_schema()


def test_slot_quality_is_independent_and_unavailable_slot_never_aligns():
    def qualities(image, detections):
        return qualified_slots(image, detections)[:1]+(FaceSlotQuality(),)
    async def scenario():
        consumer, _, clock, _, recognizer = setup(qualities=qualities)
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            assert [s.embedding_state for s in result.frame.slots] == ['produced', 'quality_unqualified']
            assert recognizer.rows == [16.]
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_provider_busy_and_bad_alignment_fail_before_feature():
    consumer, context, _, _, recognizer = setup()
    image = np.zeros((240, 320, 3), np.uint8)
    try:
        detections = context.provider.detect(image)
        context.provider._gate.acquire()
        try:
            assert context.provider.embed_slot(image, detections, 0, FaceQuality(True, True, True)).status.reason == 'provider_busy'
        finally:
            context.provider._gate.release()
        recognizer.alignCrop = lambda image, row: np.zeros((240, 240, 3), np.uint8)
        recognizer.feature = lambda aligned: (_ for _ in ()).throw(AssertionError('bad_crop_must_not_extract'))
        assert context.provider.embed_slot(image, detections, 0, FaceQuality(True, True, True)).status.reason == 'invalid_alignment_output'
    finally:
        asyncio.run(consumer.close())


def test_pending_finish_blocks_callbacks_and_rechecks_effective_deadline_after_drain():
    async def scenario():
        consumer, _, clock, detector, recognizer = setup()
        entered, release = threading.Event(), threading.Event()
        recognizer.entered, recognizer.release = entered, release
        try:
            video = asyncio.create_task(send(consumer, clock))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            owned = consumer._call
            finishing = asyncio.create_task(consumer.finish())
            await asyncio.sleep(.01)
            await send(consumer, clock, .1, value=8)
            assert consumer._call is owned and detector.calls == 1 and not finishing.done()
            clock.now = 12.1
            release.set()
            result = await finishing
            await video
            assert owned.done() and result.status.state == 'expired' and result.frame is None
            assert np.all(detector.last_owned == 0) and not consumer._native_seen
            assert recognizer.calls == 1
        finally:
            release.set()
            await consumer.close()
    asyncio.run(scenario())


def test_cancelled_finish_waiter_does_not_drop_failed_owned_operation():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def blocked_quality(image, detections):
            entered.set()
            assert release.wait(2.)
            raise ValueError('synthetic_late_quality_fault')
        consumer, context, clock, detector, _ = setup(qualities=blocked_quality)
        loop = asyncio.get_running_loop()
        failures = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, message: failures.append(message))
        try:
            video = asyncio.create_task(send(consumer, clock))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set()
            owned = consumer._call
            finishing = asyncio.create_task(consumer.finish())
            await asyncio.sleep(.01)
            finishing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await finishing
            assert not owned.done() and context.provider._detector is detector
            release.set()
            await video
            await consumer.close()
            await asyncio.sleep(0)
            assert owned.done() and not failures and not _OWNED_RECOGNITION_CALLS
            assert np.all(detector.last_owned == 0) and consumer._native_frame is None
        finally:
            release.set()
            await consumer.close()
            loop.set_exception_handler(previous)
    asyncio.run(scenario())


def test_frame_processing_age_is_bounded_even_before_window_deadline():
    async def scenario():
        consumer, _, clock, detector, recognizer = setup()
        recognizer.after = lambda: setattr(clock, 'now', 10.4)
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            assert result.status.reason == 'native_failed' and result.frame is None
            assert recognizer.calls == 1 and not consumer._native_seen
            assert np.all(detector.last_owned == 0)
        finally:
            await consumer.close()
    asyncio.run(scenario())


def test_native_schema_can_express_unknown_metadata_constructor_without_execution():
    async def scenario():
        fixture, context, clock, _, _ = setup(qualities=None)
        try:
            await send(fixture, clock)
            constructed_fixture = await fixture.finish()
            data = constructed_fixture.model_dump(mode='json')
            data['session']['simulated'] = data['status']['simulated'] = False
            data['admission_kind'] = 'native_source_metadata'
            data['provenance'].update(evidence='native_unverified', artifact_evidence='configured_only', runtime_version=None)
            native_constructor = NativeFaceResult.model_validate_json(json.dumps(data))
            assert native_constructor.provenance.evidence == 'native_unverified'
            assert native_constructor.production_admission == 'unavailable'
            assert native_constructor.frame.timing.capture_monotonic is None
            parsed = parse_worker_output(encode_worker_output(native_constructor).encode(),
                native_constructor.session, native_constructor.provenance)
            assert parsed.result == native_constructor
            owner = NativeMetadataOwnerContext(native_constructor.session, context.provider, native_constructor.provenance,
                _fixture_consent=True)
            assert not owner.admits(owner.session)  # Manual schema construction confers no admission.
            output = Path(__file__).resolve().parents[1]/'.artifacts/test-evidence/v3-native-constructors-only.json'
            output.write_text(json.dumps({'schema_version': 3, 'constructor_only': True, 'native_execution': False,
                'provenance_evidence': 'native_unverified', 'source_clock': 'unverified',
                'capture_evidence': 'unavailable', 'native_owner_admission': False,
                'production_admission': 'unavailable', 'fixture_metadata_retyped_only_for_schema_validation': True,
                'not_native_acceptance_evidence': True}, indent=2)+'\n')
        finally:
            await fixture.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('late', [False, True])
def test_actual_private_parent_writes_v3_request_parses_v3_fixture_and_fences_late_window(late):
    from dataclasses import replace

    async def scenario():
        fixture, context, clock, _, _ = setup(qualities=None)
        try:
            await send(fixture, clock)
            produced = await fixture.finish()
            session = context.session.model_copy(update={'deadline_monotonic': 18.})
            data = produced.model_dump(mode='json')
            data['session']['deadline_monotonic'] = 18.
            response = NativeFaceResult.model_validate_json(json.dumps(data))
            wire = encode_worker_output(response).encode()
            class Input:
                def write(self, value):
                    self.written = value
                async def drain(self):
                    await asyncio.sleep(0)
                def close(self):
                    pass
            class Output:
                async def read(self, count):
                    if self.sent:
                        return b''
                    self.sent = True
                    if late:
                        clock.now = 12.1
                    return wire
                sent = False
            class ProcessProtocolFixture:
                def __init__(self):
                    self.stdin, self.stdout = Input(), Output()
                    self.returncode = None
                async def wait(self):
                    self.returncode = 0
                    return 0
                def terminate(self):
                    self.returncode = 0
                kill = terminate
            process = ProcessProtocolFixture()
            async def launcher():
                return process  # Explicit in-memory IPC protocol; no subprocess/native worker.
            async def rss_reader(process):
                return 0  # No OS RSS acceptance is claimed.
            settings = SimpleNamespace(revision=1, config=SimpleNamespace(camera_id='synthetic-camera', camera_media_source='miot_sdk'))
            probe = RecognitionProbe(settings, launcher=launcher, rss_reader=rss_reader, clock=clock)
            owner = replace(context, session=session)
            assert (await probe.start_owner(owner)).state == 'running'
            await probe.task
            request = NativeFaceRequest.model_validate_json(process.stdin.written)
            assert request.schema_version == 3 and request.session == session and request.provenance == context.provenance
            if late:
                assert probe.status().state == 'expired' and probe.private_result(owner) is None
                assert probe.result is None
            else:
                assert probe.private_result(owner) == response and probe.feature_message is None
                clock.now = 12.1
                assert probe.status().state == 'expired' and probe.private_result(owner) is None
            await probe.stop()
        finally:
            await fixture.close()
    asyncio.run(scenario())


def test_sealed_metadata_survives_provider_teardown_but_closed_fixture_cannot_restart():
    async def scenario():
        consumer, context, clock, detector, _ = setup()
        await send(consumer, clock)
        result = await consumer.finish()
        await consumer.close()
        assert consumer.private_worker_result() == result
        assert np.all(detector.last_owned == 0) and not context.admits(context.session)
        restarted = RecognitionConsumer(_owner_context=context, clock=clock)
        assert not restarted.active and restarted.status().reason == 'owner_admission_unavailable'
        await restarted.close()
        clock.now = 12.1
        rebuilt = consumer.private_worker_result(status=result.status)
        assert rebuilt.status.state == 'expired' and rebuilt.frame is None
    asyncio.run(scenario())


def test_native_encoder_revalidates_mutable_nested_payload_and_wire_cannot_inject_consent():
    async def scenario():
        consumer, context, clock, _, _ = setup()
        try:
            await send(consumer, clock)
            result = await consumer.finish()
            result.frame.slots.append(result.frame.slots[0])
            with pytest.raises(ValueError):
                encode_worker_output(result)
            request = NativeFaceRequest(schema_version=3, session=context.session, provenance=context.provenance).model_dump(mode='json')
            request['_fixture_consent'] = True
            with pytest.raises(ValueError):
                NativeFaceRequest.model_validate_json(json.dumps(request))
        finally:
            await consumer.close()
    asyncio.run(scenario())
