"""Synthetic queue/camera-time regressions; no devices, enrolled people or models."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.frame_sampling import offer_frame
from homeplane.people.live import FaceIdentityPipeline
from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.recognition.contracts import Embedding

BODY = dict(id=1, box=dict(x=0., y=0., w=1., h=1.))
ROW = (150., 140., 80., 90., 170., 165., 210., 165., 190., 185., 175., 210., 205., 210., .99)


@pytest.fixture
def face(monkeypatch):
    clock = [100.]
    store = Mock()
    store.match.return_value = ('known', 'candidate')
    store.registered.return_value = {'known': 100.}
    pipeline = FaceIdentityPipeline(store, 'camera', 'session', '.', clock=lambda: clock[0])
    pipeline.provider = Mock(status=SimpleNamespace(state='ready'))
    pipeline.provider.detect.return_value = SimpleNamespace(rows=[ROW])
    vector = Embedding('face', SFACE_SHA256, (1.,) + (0.,) * 127, False)
    pipeline.provider.embed_slot.return_value = SimpleNamespace(embedding=vector)
    monkeypatch.setattr(cv2, 'Laplacian', lambda *_: SimpleNamespace(var=lambda: 100.))
    jpeg = cv2.imencode('.jpg', np.zeros((720, 1280, 3), dtype=np.uint8))[1].tobytes()
    return pipeline, jpeg, clock, store


def packet(at, face=False):
    return dict(type='frame', received_at=at, jpeg='body-' + str(at),
                **({'face_jpeg': 'face-' + str(at)} if face else {}))


def test_recent_high_resolution_packet_survives_preview_burst_as_whole_frame():
    queue = asyncio.Queue(maxsize=1)
    original = packet(100., True)
    offer_frame(queue, original, now=100.)
    for at in (100.2, 100.4):
        offer_frame(queue, packet(at), now=at)
    assert queue.qsize() == 1
    assert queue.get_nowait() is original


@pytest.mark.parametrize('age', [.50001, -1.])
def test_priority_never_retains_stale_or_future_frame(age):
    queue = asyncio.Queue(maxsize=1)
    offer_frame(queue, packet(100., True), now=100.)
    newest = packet(100. + age)
    offer_frame(queue, newest, now=100. + age)
    assert queue.get_nowait() is newest


def test_new_face_and_terminal_error_replace_pending_frame_immediately():
    queue = asyncio.Queue(maxsize=1)
    offer_frame(queue, packet(100., True), now=100.)
    newer = packet(100.2, True)
    offer_frame(queue, newer, now=100.2)
    assert queue.get_nowait() is newer
    offer_frame(queue, newer, now=100.2)
    terminal = dict(type='error', reason='privacy_shutter_closed')
    offer_frame(queue, terminal, now=100.3)
    offer_frame(queue, packet(100.4, True), now=100.4)
    assert queue.get_nowait() is terminal


def test_five_hz_producer_and_slower_consumer_can_accumulate_independent_faces(face):
    pipeline, jpeg, clock, _ = face
    queue = asyncio.Queue(maxsize=1)
    states, observed = [], []
    for i in range(37):
        at = 100. + i * .2
        offer_frame(queue, packet(at, i % 3 == 0), now=at)
        if i % 2 == 0:
            current = queue.get_nowait()
            clock[0] = at
            rows = pipeline.process(jpeg, [BODY], sample=bool(current.get('face_jpeg')),
                                    captured_at=current['received_at'])
            if current.get('face_jpeg'):
                observed.append(current['received_at'])
                states.extend(row['state'] for row in rows)
    assert len(observed) >= 10
    assert max(b - a for a, b in zip(observed, observed[1:])) < 1
    assert states[:3] == ['candidate', 'candidate', 'matched']
    assert states[-1] == 'matched'
    assert pipeline.diagnostics()['source_rejected'] == 0


def test_replayed_frame_cannot_advance_votes_or_refresh_public_identity(face):
    pipeline, jpeg, clock, _ = face
    assert pipeline.process(jpeg, [BODY], captured_at=100.)[0]['state'] == 'candidate'
    clock[0] = 100.6
    pipeline.process(jpeg, [BODY], captured_at=100.)
    assert pipeline.votes['1'][1] == 1
    assert pipeline.diagnostics()['source_rejected'] == 1
    assert pipeline.process(jpeg, [BODY], captured_at=100.6)[0]['state'] == 'candidate'
    clock[0] = 101.4
    result = pipeline.process(jpeg, [BODY], captured_at=101.2)[0]
    assert result['state'] == 'matched'
    assert result['face_observed_monotonic'] == result['observed_monotonic'] == 101.2
    clock[0] = 102.3
    assert pipeline.public() == []


@pytest.mark.parametrize('stamp', [98.9, 100.1, float('nan'), True])
def test_invalid_original_arrival_never_enters_native_face_work(face, stamp):
    pipeline, jpeg, _, store = face
    assert pipeline.process(jpeg, [BODY], captured_at=stamp) == []
    pipeline.provider.detect.assert_not_called()
    store.observe.assert_not_called()


def test_slow_embedding_is_revoked_before_presence_or_candidate_side_effects(face):
    pipeline, jpeg, clock, store = face
    result = pipeline.provider.embed_slot.return_value
    def slow(*_):
        clock[0] += 1.1
        return result
    pipeline.provider.embed_slot.side_effect = slow
    assert pipeline.process(jpeg, [BODY], captured_at=100.) == []
    assert pipeline.public() == [] and not pipeline.votes and not pipeline.samples
    store.observe.assert_not_called()
    store.submit_candidate.assert_not_called()
    store.clear_camera.assert_called_once()


def test_slow_detector_cannot_refresh_body_carried_presence(face):
    pipeline, jpeg, clock, store = face
    for at in (100., 100.6, 101.2):
        clock[0] = at
        pipeline.process(jpeg, [BODY], captured_at=at)
    assert pipeline.public()[0]['state'] == 'matched'
    store.reset_mock()
    def slow(*_):
        clock[0] += 1.1
        return SimpleNamespace(rows=[])
    pipeline.provider.detect.side_effect = slow
    clock[0] = 101.8
    assert pipeline.process(jpeg, [BODY], captured_at=101.8) == []
    store.observe.assert_not_called()
    assert not pipeline.public()


def sample(at, people):
    return dict(status='live', _local_received=at, session_id='camera-session',
                scene_visible=True, people=people)


def edge_person(identity=False):
    return dict(id=1, box=dict(x=0., y=.1, w=.2, h=.8),
                identity=dict(state='matched', person_id='known') if identity else {})


def test_follow_waits_for_face_acquisition_after_pan_but_wait_is_bounded():
    director = CameraDirector()
    director.sent(100., 'left')
    assert director.choose(sample(102.1, []), 102.1) is None
    for at in (102.4, 103., 103.6):
        assert director.choose(sample(at, [edge_person()]), at) is None
    assert director.choose(sample(104., [edge_person()]), 104.) == 'left'


def test_fresh_matched_face_can_release_post_pan_follow_wait():
    director = CameraDirector()
    director.sent(100., 'left')
    assert director.choose(sample(102.1, []), 102.1) is None
    assert director.choose(sample(102.4, [edge_person()]), 102.4) is None
    assert director.choose(sample(103.6, [edge_person(True)]), 103.6) == 'left'
