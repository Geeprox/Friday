"""Synthetic queue/camera-time regressions; no devices, enrolled people or models."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from homeplane.embodied.camera_director import CameraDirector
from homeplane.embodied.frame_sampling import PendingFrames
from homeplane.people.live import FaceIdentityPipeline
from homeplane.recognition.artifacts import SFACE_SHA256
from homeplane.recognition.contracts import Embedding


@pytest.mark.asyncio
async def test_original_secondary_body_pipeline_continues_while_identity_startup_is_blocked(tmp_path, monkeypatch):
    import base64
    import json
    import threading
    import time
    from unittest.mock import AsyncMock
    from homeplane.embodied import camera_recognition
    from homeplane.people.identity_session import IdentitySession
    from homeplane.people.live import IdentityStore
    from tests.test_identity_session import Peer, JPEG
    from tests.embodied.test_tracking_continuity import frame
    private=tmp_path/'private';private.mkdir(mode=0o700)
    store=IdentityStore(private/'identities.sqlite')
    gate=threading.Event();peer=Peer(init_gate=gate)
    monkeypatch.setattr(camera_recognition,'IdentitySession',
        lambda *args:IdentitySession(*args,_peer_factory=lambda:peer))
    recognizer=camera_recognition.CameraRecognition('synthetic-camera',store)
    from tests.embodied.test_room_process import room_port
    recognizer.room=room_port(registration=np.eye(3))
    process=SimpleNamespace(returncode=None,
        stdin=SimpleNamespace(write=Mock(),drain=AsyncMock()),
        stdout=SimpleNamespace(readline=AsyncMock(return_value=(json.dumps(frame())+'\n').encode())))
    def terminate():process.returncode=0
    process.terminate=process.kill=terminate;process.wait=AsyncMock(return_value=0)
    recognizer.vision.process=process
    try:
        observed=[]
        for _ in range(6):
            began=time.monotonic()
            result=await recognizer.observe(dict(received_at=time.time(),jpeg=base64.b64encode(JPEG).decode(),
                width=640,height=360),source_current=lambda:True)
            assert time.monotonic()-began<.2
            observed.append(result)
            await asyncio.sleep(.02)
        assert peer.initializing.is_set() and not gate.is_set()
        assert all(item is not None and item['tracking_current'] for item in observed)
        assert observed[-1]['people'] and observed[-1]['people'][0]['joints']
        assert observed[-1]['people'][0]['identity']['state']=='unknown'
        assert recognizer.identities.public()==[]
        assert await recognizer.close()
        assert peer.aborted and peer.closed and process.returncode==0
    finally:
        gate.set();await recognizer.close();store.close()

def test_four_camera_frames_reach_consumers_while_identity_and_default_executor_are_busy(tmp_path, monkeypatch):
    import base64
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import AsyncMock

    from homeplane.embodied import camera_recognition
    from homeplane.people.identity_session import IdentitySession
    from homeplane.people.live import IdentityStore, analyze, close_identity
    from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope
    from tests.embodied.test_room_process import room_port
    from tests.embodied.test_tracking_continuity import frame
    from tests.test_embodied_safety import person
    from tests.test_identity_session import JPEG, Peer

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        occupied = asyncio.Event()
        queue_release, peer_release = threading.Event(), threading.Event()

        def occupy_executor():
            loop.call_soon_threadsafe(occupied.set)
            assert queue_release.wait(6)

        executor_task = loop.run_in_executor(None, occupy_executor)
        names = ("fixture-primary", "fixture-secondary-a", "fixture-secondary-b", "fixture-secondary-c")
        peers = {name: Peer(frame_gate=peer_release, ignore_abort=True) for name in names}
        identities, recognizers, tasks = [], [], []
        clock = SimpleNamespace(time=lambda: 1000., monotonic=lambda: 10.)
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        store = IdentityStore(private / "identities.sqlite")

        def identity(owner, camera, session, models):
            result = IdentitySession(owner, camera, session, models, clock=clock.monotonic,
                                     _peer_factory=lambda: peers[camera])
            identities.append(result)
            assert result.process(JPEG, [person()], captured_at=9.5) == []
            return result

        entered = set()
        all_entered = asyncio.Event()

        async def identity_stage(pipeline, *args, **kwargs):
            entered.add(pipeline.camera)
            if len(entered) == len(names):
                all_entered.set()
            return await analyze(pipeline, *args, **kwargs)

        try:
            await asyncio.wait_for(occupied.wait(), 2)
            jpeg = base64.b64encode(JPEG).decode()
            scope, _ = primary_frame_scope(tmp_path)
            scope.update(identities=identity(store, names[0], "fixture-view", tmp_path),
                         time=clock, analyze=identity_stage)
            scope["tracks"].update.return_value = [person()]
            scope["item"]["jpeg"] = jpeg
            monkeypatch.setattr(camera_recognition, "IdentitySession", identity)
            monkeypatch.setattr(camera_recognition, "analyze", identity_stage)
            monkeypatch.setattr(camera_recognition, "time", clock)
            for name in names[1:]:
                recognizer = camera_recognition.CameraRecognition(name, store)
                recognizers.append(recognizer)
                recognizer.room = room_port(registration=np.eye(3))
                recognizer.vision.detect = AsyncMock(side_effect=lambda *args, **kwargs: frame())
                for observed in (9.7, 9.8, 9.9):
                    recognizer.tracks.update(frame(), observed, transform=np.eye(3))

            async with asyncio.timeout(2):
                while not all(peer.processing.is_set() for peer in peers.values()):
                    await asyncio.sleep(.002)
            tasks.append(asyncio.create_task(frame_continuation(scope)()))
            tasks.extend(asyncio.create_task(recognizer.observe(
                dict(received_at=1000., jpeg=jpeg, width=640, height=360), source_current=lambda: True))
                for recognizer in recognizers)
            await asyncio.wait_for(all_entered.wait(), 2)
            assert all(task.done() and not task.cancelled() for task in tasks)
            assert not executor_task.done() and not peer_release.is_set()
            tasks[0].result()
            scope["safety"].offer.assert_called_once()
            scope["home"].observe.assert_awaited_once()
            primary_people = scope["safety"].offer.call_args.args[2]
            camera_people = [primary_people, *(task.result()["people"] for task in tasks[1:])]
            assert all(people and people[0]["joints"] and people[0]["identity"]["state"] == "unknown"
                       for people in camera_people)
            for session in identities:
                transport = session.diagnostics()["transport"]
                assert transport["busy"] == transport["pending"] == 1
                session.discard()
            assert all(peer.aborted for peer in peers.values())
            peer_release.set()
            async with asyncio.timeout(2):
                while any(session.diagnostics()["transport"]["busy"] for session in identities):
                    await asyncio.sleep(.002)
            assert all(session.public() == [] for session in identities)
        finally:
            queue_release.set()
            peer_release.set()
            await executor_task
            await asyncio.gather(*tasks, return_exceptions=True)
            for session in identities:
                await close_identity(session)
            for recognizer in recognizers:
                await recognizer.vision.close()
            store.close()

    asyncio.run(exercise())


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
    queue = PendingFrames(clock=lambda: 100.4)
    original = packet(100., True)
    newest = packet(100.4)
    queue.offer(original)
    queue.offer(packet(100.2))
    queue.offer(newest)
    assert queue.get_nowait() is original
    assert queue.get_nowait() is newest
    assert queue.empty()


def test_recent_processing_cost_prefers_a_fresh_frame_then_restores_face_priority():
    queue = PendingFrames(clock=lambda: 100.4)
    face, latest = packet(100., True), packet(100.4)
    queue.note_processing(1.22)
    queue.offer(face)
    queue.offer(latest)
    assert queue.get_nowait() is latest
    assert queue.empty()
    queue.note_processing(.5)
    queue.offer(face)
    queue.offer(latest)
    assert queue.get_nowait() is face
    assert queue.get_nowait() is latest


@pytest.mark.parametrize('taken_at, newest_at', [(100.50001, 100.4), (99., 99.)])
def test_priority_is_checked_when_taken_and_excludes_stale_or_future_face(taken_at, newest_at):
    queue = PendingFrames(clock=lambda: taken_at)
    newest = packet(newest_at)
    queue.offer(packet(100., True))
    queue.offer(newest)
    assert queue.get_nowait() is newest
    assert queue.empty()


@pytest.mark.parametrize('scope_key', ['capture_session', 'camera_source_context', '_motion_generation'])
def test_face_priority_cannot_carry_across_source_or_motion_change(scope_key):
    queue = PendingFrames(clock=lambda: 100.4)
    queue.offer(dict(packet(100., True), **{scope_key: 'old'}))
    newest = dict(packet(100.4), **{scope_key: 'new'})
    queue.offer(newest)
    assert queue.get_nowait() is newest
    assert queue.empty()


def test_new_face_and_terminal_error_replace_pending_frame_immediately():
    queue = PendingFrames(clock=lambda: 100.4)
    queue.offer(packet(100., True))
    newer = packet(100.2, True)
    queue.offer(newer)
    assert queue.get_nowait() is newer
    queue.offer(newer)
    queue.offer(packet(100.3))
    terminal = dict(type='error', reason='privacy_shutter_closed')
    queue.offer(terminal)
    queue.offer(packet(100.4, True))
    assert queue.get_nowait() is terminal
    assert queue.empty()


def test_five_hz_producer_and_slower_consumer_can_accumulate_independent_faces(face):
    pipeline, jpeg, clock, _ = face
    queue = PendingFrames(clock=lambda: clock[0])
    states, observed = [], []
    for i in range(37):
        at = 100. + i * .2
        clock[0] = at
        queue.offer(packet(at, i % 3 == 0))
        if i % 2 == 0:
            current = queue.get_nowait()
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
