"""Latest-frame process ownership with synthetic peers; never loads a face model."""

import asyncio
import copy
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from homeplane.people.identity_session import IdentityPeer, IdentitySession
from homeplane.people.identity_wire import receive_packet, send_packet
from homeplane.people.live import IdentityStore

JPEG = b"\xff\xd8SYNTHETIC-NOT-CAMERA-PIXELS\xff\xd9"


def body(track=1):
    return dict(id=track, box=dict(x=0.1, y=0.1, w=0.4, h=0.8))


def row(track, at):
    return dict(
        track=str(track),
        person_id="synthetic_person",
        state="matched",
        facing=True,
        box=dict(x=0.2, y=0.2, w=0.1, h=0.1),
        enroll_ready=True,
        observed_monotonic=at,
        face_observed_monotonic=at,
        basis="face_similarity",
    )


def wait_until(check, timeout=1):
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= end:
            raise AssertionError("synthetic peer did not reach expected state")
        time.sleep(0.002)


@pytest.fixture
def store(tmp_path):
    private = tmp_path / "identity"
    private.mkdir(mode=0o700)
    result = IdentityStore(private / "identities.sqlite")
    try:
        yield result
    finally:
        result.close()


class Peer:
    def __init__(self, *, init_gate=None, frame_gate=None, ignore_abort=False):
        self.aborted = self.closed = False
        self.init_gate, self.frame_gate, self.ignore_abort = init_gate, frame_gate, ignore_abort
        self.initializing, self.processing = threading.Event(), threading.Event()
        self.calls = []

    def _wait(self, gate, timeout):
        if gate is None:
            return
        until = time.monotonic() + timeout
        while not gate.wait(0.002):
            if self.aborted and not self.ignore_abort:
                raise EOFError("synthetic abort")
            if time.monotonic() > until:
                raise TimeoutError("synthetic native stall")

    def call(self, action, value, timeout):
        self.calls.append((action, copy.deepcopy(value)))
        if action == "init":
            self.initializing.set()
            self._wait(self.init_gate, timeout)
            return {"reason": "synthetic"}
        if action == "frame":
            self.processing.set()
            self._wait(self.frame_gate, timeout)
            return dict(
                rows=[row(p["id"], value["captured_at"]) for p in value["people"] or [body("face_1")]],
                reason="synthetic",
                diagnostics={"frames": 1},
            )
        if action == "discard":
            return {}
        if action == "enroll":
            return dict(
                status="enrolled",
                person_id=value["person"],
                samples=3,
                policy="sface-household-experimental-v1",
            )
        raise AssertionError("unknown synthetic request")

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


def test_status_construction_and_empty_or_moving_input_never_start_a_process(store):
    factory = Mock(side_effect=AssertionError("status must not start models"))
    session = IdentitySession(store, "camera", "source", ".", _peer_factory=factory)
    assert session.public() == [] and session.diagnostics()["transport"]["resident"] == 0
    assert session.process(JPEG, [], captured_at=time.monotonic()) == []
    assert session.process(JPEG, [body()], moving=True) == []
    assert session.close() is True
    factory.assert_not_called()


def test_slow_startup_does_not_hold_caller_and_only_the_latest_frame_is_processed(store):
    gate = threading.Event()
    peer = Peer(init_gate=gate)
    clock = [100.0]
    session = IdentitySession(
        store, "camera", "source", ".", clock=lambda: clock[0], _peer_factory=lambda: peer
    )
    try:
        began = time.monotonic()
        assert session.process(JPEG, [body(1)], captured_at=clock[0]) == []
        assert time.monotonic() - began < 0.1
        assert peer.initializing.wait(0.5)
        for track in range(2, 7):
            clock[0] += 0.6
            assert session.process(JPEG, [body(track)], captured_at=clock[0]) == []
        gate.set()
        wait_until(lambda: bool(session.public()))
        frames = [value for action, value in peer.calls if action == "frame"]
        assert len(frames) == 1 and frames[0]["people"][0]["id"] == 6
        assert session.public()[0]["track"] == "6"
        assert session.diagnostics()["transport"]["replaced"] == 4
    finally:
        gate.set()
        assert session.close()
    assert peer.closed


def test_idle_discard_retracts_presence_without_reloading_resident_models(store):
    peer = Peer()
    factory = Mock(return_value=peer)
    clock = [100.0]
    session = IdentitySession(store, "camera", "source", ".", clock=lambda: clock[0], _peer_factory=factory)
    try:
        session.process(JPEG, [body()], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        store.observe("camera", "source", "1", "synthetic_person", "matched")
        session.discard()
        assert session.public() == []
        wait_until(lambda: not any(p["present"] for p in store.presence()))
        assert not peer.aborted
        assert len([a for a, _ in peer.calls if a == "discard"]) == 1
        for _ in range(10):
            session.discard()
        clock[0] += 0.6
        session.process(JPEG, [body()], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        factory.assert_called_once()
    finally:
        assert session.close()


def test_late_reply_after_revoke_cannot_publish_into_a_replacement_session(store):
    gate = threading.Event()
    old = Peer(frame_gate=gate, ignore_abort=True)
    new = Peer()
    peers = iter([old, new])
    clock = [100.0]
    session = IdentitySession(
        store, "camera", "old_source", ".", clock=lambda: clock[0], _peer_factory=lambda: next(peers)
    )
    try:
        session.process(JPEG, [body(1)], captured_at=clock[0])
        assert old.processing.wait(0.5)
        began = time.monotonic()
        session.session = "new_source"
        assert time.monotonic() - began < 0.1 and session.public() == []
        clock[0] += 0.6
        session.process(JPEG, [body(2)], captured_at=clock[0])
        gate.set()
        wait_until(lambda: bool(session.public()))
        assert old.aborted and old.closed
        assert session.public()[0]["track"] == "2"
        assert [v for a, v in new.calls if a == "init"][0]["session"] == "new_source"
    finally:
        gate.set()
        assert session.close()


def test_recent_result_requires_current_visible_track_and_current_input(store):
    peer = Peer()
    clock = [100.0]
    session = IdentitySession(
        store, "camera", "source", ".", clock=lambda: clock[0], _peer_factory=lambda: peer
    )
    try:
        session.process(JPEG, [body()], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        clock[0] += 0.1
        assert session.process(JPEG, [body(2)], sample=False, captured_at=clock[0]) == []
        assert session.public() == []
        clock[0] += 2
        assert session.public() == []
        assert session.process(JPEG, [body(), body()], captured_at=clock[0]) == []
        assert session.public() == []
    finally:
        assert session.close()


def test_idle_models_release_their_slot_and_only_restart_on_fresh_input(store):
    peers = [Peer(), Peer()]
    factory = Mock(side_effect=peers)
    clock = [100.0]
    session = IdentitySession(store, "camera", "source", ".", clock=lambda: clock[0], _peer_factory=factory)
    try:
        session.process(JPEG, [body()], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        clock[0] += 31
        session.discard()
        wait_until(lambda: peers[0].closed)
        assert session.diagnostics()["transport"]["resident"] == 0
        clock[0] += 1
        session.process(JPEG, [body(2)], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        assert factory.call_count == 2 and session.public()[0]["track"] == "2"
    finally:
        assert session.close()


def test_close_refuses_late_input_until_an_explicit_new_source_session(store):
    peers = [Peer(), Peer()]
    factory = Mock(side_effect=peers)
    clock = [100.0]
    session = IdentitySession(store, "camera", "source", ".", clock=lambda: clock[0], _peer_factory=factory)
    try:
        session.process(JPEG, [body()], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        assert session.close()
        clock[0] += 0.6
        assert session.process(JPEG, [body()], captured_at=clock[0]) == []
        assert factory.call_count == 1 and session._latest is None
        session.session = "explicit_new_source"
        session.process(JPEG, [body(2)], captured_at=clock[0])
        wait_until(lambda: bool(session.public()))
        assert factory.call_count == 2
        assert session.close()
        clock[0] += 0.6
        assert session.process(JPEG, [body(3)], captured_at=clock[0]) == []
        assert factory.call_count == 2 and session._latest is None
    finally:
        assert session.close()


def test_global_child_budget_still_allows_capture_when_all_model_slots_are_taken(store, monkeypatch):
    from homeplane.people import identity_session as module

    monkeypatch.setattr(module, "PROCESS_SLOTS", threading.BoundedSemaphore(1))
    gate = threading.Event()
    first, second = Peer(init_gate=gate), Peer()
    clock = [100.0]
    a = IdentitySession(
        store, "camera_a", "source_a", ".", clock=lambda: clock[0], _peer_factory=lambda: first
    )
    factory = Mock(return_value=second)
    b = IdentitySession(store, "camera_b", "source_b", ".", clock=lambda: clock[0], _peer_factory=factory)
    try:
        a.process(JPEG, [body()], captured_at=clock[0])
        assert first.initializing.wait(0.5)
        assert b.process(JPEG, [body()], captured_at=clock[0]) == []
        wait_until(lambda: b.diagnostics()["transport"]["failed"] == 1)
        factory.assert_not_called()
        assert a.close()
        clock[0] += 6
        b.process(JPEG, [body()], captured_at=clock[0])
        wait_until(lambda: bool(b.public()))
        factory.assert_called_once()
    finally:
        gate.set()
        assert a.close()
        assert b.close()


def test_blocked_database_retraction_is_reported_and_retried_without_retaining_a_model(store):
    peer = Peer()
    session = IdentitySession(store, "camera", "source", ".", _peer_factory=lambda: peer)
    other = sqlite3.connect(store.path)
    try:
        session.process(JPEG, [body()], captured_at=time.monotonic())
        wait_until(lambda: bool(session.public()))
        store.observe("camera", "source", "1", "synthetic_person", "matched")
        other.execute("BEGIN IMMEDIATE")
        began = time.monotonic()
        session.discard()
        assert time.monotonic() - began < 0.1 and session.public() == []
        assert session.close() is False
        assert peer.closed and session.diagnostics()["transport"]["cleanup_pending"] == 1
        other.rollback()
        assert session.close() is True
        assert not any(row["present"] for row in store.presence())
        assert session.diagnostics()["transport"]["resident"] == 0
    finally:
        other.rollback()
        other.close()
        assert session.close()


@pytest.mark.asyncio
async def test_pending_enrollment_is_revoked_with_its_source(store):
    peer = Peer()
    clock = [100.0]
    session = IdentitySession(
        store, "camera", "source", ".", clock=lambda: clock[0], _peer_factory=lambda: peer
    )
    gate = threading.Event()
    try:
        session.process(JPEG, [body()], captured_at=clock[0])
        await asyncio.to_thread(wait_until, lambda: bool(session.public()))
        peer.processing.clear()
        peer.frame_gate = gate
        clock[0] += 0.6
        session.process(JPEG, [body()], captured_at=clock[0])
        assert await asyncio.to_thread(peer.processing.wait, 0.5)
        pending = asyncio.create_task(asyncio.to_thread(session.enroll, "1", "synthetic_person"))
        await asyncio.to_thread(wait_until, lambda: bool(session._controls))
        session.discard()
        with pytest.raises(ValueError, match="identity_session_changed"):
            await pending
        assert not any(a == "enroll" for a, _ in peer.calls)
    finally:
        gate.set()
        assert await asyncio.to_thread(session.close)


INERT_CHILD = """
import os,socket,sys,time
from homeplane.people.identity_worker import serve
from homeplane.embodied.camera_guardian import exit_with_owner
exit_with_owner(os.getppid())
class Inert:
 reason='synthetic'
 def process(self,*args,**kwargs):
  print('synthetic-inference-entered',flush=True)
  time.sleep(60)
 def close(self):pass
serve(socket.socket(fileno=int(sys.argv[1])),pipeline_factory=lambda value:(Inert(),None))
"""


class InertProcessPeer(IdentityPeer):
    def __init__(self, peers):
        import secrets

        self.connection, child = socket.socketpair()
        self.nonce, self.sequence, self.aborted = secrets.token_hex(16), 0, False
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-B", "-c", INERT_CHILD, str(child.fileno())],
                cwd=Path(__file__).resolve().parents[1],
                pass_fds=(child.fileno(),),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        finally:
            child.close()
        peers.append(self)


@pytest.mark.asyncio
async def test_busy_real_child_is_killed_and_reaped_while_the_event_loop_continues(store):
    peers = []
    session = IdentitySession(store, "camera", "source", ".", _peer_factory=lambda: InertProcessPeer(peers))
    ticks = []

    async def tick():
        for _ in range(8):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    ticker = None
    try:
        assert session.process(JPEG, [body()], captured_at=time.monotonic()) == []
        await asyncio.to_thread(wait_until, lambda: bool(peers), 3)
        entered = await asyncio.wait_for(asyncio.to_thread(peers[0].process.stdout.readline), 3)
        assert entered == b"synthetic-inference-entered\n"
        ticker = asyncio.create_task(tick())
        began = time.monotonic()
        session.discard()
        assert time.monotonic() - began < 0.1 and session.public() == []
        assert await asyncio.to_thread(session.close)
        if ticker:
            await ticker
        assert len(ticks) == 8 and peers[0].process.poll() is not None
        assert session._thread is not None and not session._thread.is_alive()
    finally:
        await asyncio.to_thread(session.close)
        for peer in peers:
            peer.close()
        if ticker:
            await ticker


@pytest.mark.asyncio
async def test_real_child_deadline_reaps_the_stalled_model_without_blocking_new_body_input(store):
    peers = []
    session = IdentitySession(store, "camera", "source", ".", _peer_factory=lambda: InertProcessPeer(peers))
    try:
        session.process(JPEG, [body()], captured_at=time.monotonic())
        await asyncio.to_thread(wait_until, lambda: bool(peers), 3)
        assert (
            await asyncio.wait_for(asyncio.to_thread(peers[0].process.stdout.readline), 3)
            == b"synthetic-inference-entered\n"
        )
        for _ in range(10):
            began = time.monotonic()
            assert session.process(JPEG, [body()], captured_at=time.monotonic()) == []
            assert time.monotonic() - began < 0.1
            await asyncio.sleep(0.01)
        await asyncio.to_thread(wait_until, lambda: peers[0].process.poll() is not None, 3)
        assert len(peers) == 1 and session.public() == []
        assert session.diagnostics()["transport"]["failed"] == 1
        assert await asyncio.to_thread(session.close)
    finally:
        await asyncio.to_thread(session.close)
        for peer in peers:
            peer.close()


def test_private_peer_rejects_wrong_nonce_before_any_rows_are_accepted():
    parent, child = socket.socketpair()
    peer = IdentityPeer.__new__(IdentityPeer)
    peer.connection, peer.nonce, peer.sequence = parent, "a" * 32, 0

    def respond():
        request = receive_packet(child, time.monotonic() + 1)
        send_packet(
            child,
            dict(
                v=1,
                nonce="b" * 32,
                sequence=request["sequence"],
                action=request["action"],
                ok=True,
                result={"rows": []},
            ),
            time.monotonic() + 1,
        )

    responder = threading.Thread(target=respond)
    responder.start()
    try:
        with pytest.raises(ValueError, match="identity_protocol_invalid"):
            peer.call("frame", {}, 0.5)
    finally:
        parent.close()
        child.close()
        responder.join(timeout=1)


def test_actual_child_entrypoint_rejects_incomplete_context_before_database_or_model_start(tmp_path):
    sentinel = tmp_path / "must-not-be-created.sqlite"
    peer = IdentityPeer()
    try:
        with pytest.raises(ValueError, match="^identity_context_invalid$"):
            peer.call("init", dict(database=str(sentinel), camera="synthetic", session="synthetic"), 2)
    finally:
        peer.close()
    assert not sentinel.exists()
    assert peer.process.poll() == 0 and peer.connection.fileno() == -1
