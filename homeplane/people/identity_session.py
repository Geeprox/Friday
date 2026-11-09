"""Camera-owned, latest-frame face process; the control loop never waits on a model.

Only public identity rows cross back. Images stay in bounded memory and the
existing private store remains the only template/candidate persistence owner.
"""

import base64
import copy
import math
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from .identity_wire import REPLY_LIMIT, UNAVAILABLE_REASONS, abort_socket, receive_packet, send_packet

REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JPEG_LIMIT = 4 * 1024 * 1024
PROCESS_SLOTS = threading.BoundedSemaphore(8)
IDLE_SECONDS = 30


class IdentityPeer:
    def __init__(self):
        self.connection, child = socket.socketpair()
        self.nonce, self.sequence = secrets.token_hex(16), 0
        self.aborted = False
        environment = dict(os.environ)
        environment.pop("DYLD_INSERT_LIBRARIES", None)
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "homeplane.people.identity_worker",
                    "--fd",
                    str(child.fileno()),
                    "--owner-pid",
                    str(os.getpid()),
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=environment,
                close_fds=True,
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            self.connection.close()
            raise
        finally:
            child.close()

    def call(self, action, value, timeout):
        self.sequence += 1
        deadline = time.monotonic() + timeout
        send_packet(
            self.connection,
            dict(v=1, nonce=self.nonce, sequence=self.sequence, action=action, value=value),
            deadline,
        )
        reply = receive_packet(self.connection, deadline, limit=REPLY_LIMIT)
        if (
            set(reply) != {"v", "nonce", "sequence", "action", "ok", "result"}
            or type(reply["v"]) is not int
            or reply["v"] != 1
            or reply["nonce"] != self.nonce
            or type(reply["sequence"]) is not int
            or reply["sequence"] != self.sequence
            or reply["action"] != action
            or type(reply["ok"]) is not bool
            or not isinstance(reply["result"], dict)
        ):
            raise ValueError("identity_protocol_invalid")
        if not reply["ok"]:
            reason = reply["result"].get("reason")
            raise ValueError(
                reason
                if isinstance(reason, str) and reason in UNAVAILABLE_REASONS
                else "identity_request_unavailable"
            )
        return reply["result"]

    def abort(self):
        self.aborted = True
        abort_socket(self.connection)
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                pass

    def close(self):
        abort_socket(self.connection)
        try:
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.abort()
                self.process.wait(timeout=0.5)
        finally:
            if self.process.poll() is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()


def retract_presence(path, camera, session):
    """Use an independent short SQLite lease, never the camera loop's model lock."""
    try:
        db = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=rw", uri=True, timeout=0.05)
        try:
            with db:
                db.execute(
                    "UPDATE presence SET state='unknown',facing=NULL WHERE camera=? AND session=?",
                    (camera, session),
                )
        finally:
            db.close()
        return True
    except sqlite3.Error:
        return False


class IdentitySession:
    def __init__(self, store, camera, session, models, *, clock=time.monotonic, _peer_factory=IdentityPeer):
        if not all(isinstance(v, str) and REF.fullmatch(v) for v in (camera, session)):
            raise ValueError("identity_source_invalid")
        self.camera, self._session = camera, session
        self.database, self.models = Path(store.path).absolute(), Path(models).absolute()
        self.clock, self._factory = clock, _peer_factory
        self._condition = threading.Condition()
        self._thread = self._peer = self._peer_session = self._busy = None
        self._closed = self._allow_reopen = False
        self._revoked = False
        self._cleanup_ok = True
        self._has_slot = False
        self._epoch = 0
        self._latest = None
        self._controls = []
        self._clear = set()
        self._published = ()
        self._published_epoch = -1
        self._visible = set()
        self._last_offered = -math.inf
        self._last_input = -math.inf
        self._retry_at = 0
        self._model_used_at = -math.inf
        self.reason = "not_started"
        self._diagnostics = {}
        self._counts = dict(
            offered=0, replaced=0, processed=0, source_rejected=0, failed=0, cleanup_pending=0
        )

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, value):
        if not isinstance(value, str) or not REF.fullmatch(value):
            raise ValueError("identity_source_invalid")
        if value == self._session:
            return
        self.discard()
        with self._condition:
            self._session = value
            self._allow_reopen = self._closed
            self._retry_at = 0
            self._condition.notify_all()

    def _wake(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="friday-identity-owner", daemon=True)
            self._cleanup_ok = False
            self._thread.start()
        self._condition.notify_all()

    def process(self, jpeg, people=None, faces=(), *, moving=False, sample=True, captured_at=None):
        observed = self.clock() if captured_at is None else captured_at
        if (
            moving
            or people is not None
            and not people
            or type(observed) not in (int, float)
            or not math.isfinite(observed)
            or not 0 <= self.clock() - observed <= 1
        ):
            self.discard()
            return []
        if (
            not isinstance(jpeg, bytes)
            or len(jpeg) > JPEG_LIMIT
            or not jpeg.startswith(b"\xff\xd8")
            or not jpeg.endswith(b"\xff\xd9")
            or people is not None
            and (not isinstance(people, list) or len(people) > 16)
            or not isinstance(faces, (list, tuple))
            or len(faces) > 32
        ):
            self.discard()
            return []
        if people is not None and (
            any(
                not isinstance(p, dict)
                or type(p.get("id")) not in (int, str)
                or not REF.fullmatch(str(p["id"]))
                for p in people
            )
            or len({str(p["id"]) for p in people}) != len(people)
        ):
            self.discard()
            return []
        with self._condition:
            if self._closed:
                if not self._allow_reopen or self._thread and self._thread.is_alive():
                    return []
                self._closed = self._allow_reopen = False
            self._last_input = observed
            self._visible = {str(p["id"]) for p in people} if people is not None else None
            if sample and observed - self._last_offered >= 0.5:
                self._last_offered = observed
                if self._latest is not None:
                    self._counts["replaced"] += 1
                self._counts["offered"] += 1
                self._revoked = False
                self._latest = (
                    self._epoch,
                    self._session,
                    jpeg,
                    copy.deepcopy(people),
                    copy.deepcopy(list(faces)),
                    observed,
                )
                self._wake()
        return self.public()

    def public(self):
        with self._condition:
            if (
                self._closed
                or self._published_epoch != self._epoch
                or not 0 <= self.clock() - self._last_input <= 1
            ):
                return []
            return [
                copy.deepcopy(row)
                for row in self._published
                if (self._visible is None or row["track"] in self._visible)
                and 0 <= self.clock() - row["observed_monotonic"] <= 1
            ]

    def diagnostics(self):
        with self._condition:
            return dict(
                self._diagnostics,
                transport=dict(
                    self._counts,
                    pending=int(self._latest is not None),
                    busy=int(self._busy is not None),
                    resident=int(self._peer is not None),
                ),
            )

    def discard(self):
        with self._condition:
            if self._revoked:
                return
            self._revoked = True
            self._epoch += 1
            self._latest = None
            self._published = ()
            self._visible = set()
            self._last_offered = self._last_input = -math.inf
            if self._peer_session:
                self._clear.add(self._peer_session)
            peer = self._busy
            for _, _, _, future in self._controls:
                if not future.done():
                    future.set_exception(ValueError("identity_session_changed"))
            self._controls.clear()
            self._condition.notify_all()
        # Interrupt only a busy native operation. Idle resident models survive
        # ordinary lost frames; the owner sends discard before any next frame.
        if peer is not None:
            peer.abort()

    def enroll(self, track, person):
        if not all(isinstance(v, str) and REF.fullmatch(v) for v in (str(track), person)):
            raise ValueError("identity_samples_required")
        future = Future()
        with self._condition:
            if (
                self._closed
                or len(self._controls) >= 4
                or not any(r["track"] == str(track) and r.get("enroll_ready") for r in self.public())
            ):
                raise ValueError("identity_samples_required")
            self._controls.append(
                (self._epoch, self.clock() + 1, dict(track=str(track), person=person), future)
            )
            self._wake()
        try:
            return future.result(timeout=1.5)
        except FutureTimeout:
            future.cancel()
            raise ValueError("identity_enrollment_unavailable") from None

    def close(self):
        self.discard()
        with self._condition:
            self._closed = True
            self._allow_reopen = False
            self._epoch += 1
            self._latest = None
            self._published = ()
            self._visible = set()
            self._last_offered = self._last_input = -math.inf
            for _, _, _, future in self._controls:
                if not future.done():
                    future.set_exception(ValueError("identity_session_changed"))
            self._controls.clear()
            peer, thread = self._busy, self._thread
            if (thread is None or not thread.is_alive()) and (self._peer is not None or self._clear):
                self._wake()
                thread = self._thread
            self._condition.notify_all()
        if peer is not None:
            peer.abort()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        if thread and thread.is_alive():
            self.reason = "identity_cleanup_pending"
            return False
        return self._cleanup_ok and self._peer is None

    def _drop_peer(self):
        peer, session = self._peer, self._peer_session
        if peer is not None:
            peer.close()
        with self._condition:
            if self._peer is peer:
                self._peer = self._busy = self._peer_session = None
            if session:
                self._clear.add(session)
            if self._has_slot:
                PROCESS_SLOTS.release()
                self._has_slot = False

    def _request(self, peer, action, value, timeout, *, epoch=None):
        with self._condition:
            if epoch is not None and (self._closed or epoch != self._epoch):
                raise ValueError("identity_session_changed")
            self._busy = peer
        try:
            return peer.call(action, value, timeout)
        finally:
            with self._condition:
                if self._busy is peer:
                    self._busy = None

    def _ensure_peer(self, epoch, session):
        if self._peer is not None and (self._peer_session != session or self._peer.aborted):
            self._drop_peer()
        if self._peer is None:
            if not PROCESS_SLOTS.acquire(blocking=False):
                raise ValueError("identity_capacity")
            self._has_slot = True
            try:
                peer = self._factory()
            except BaseException:
                PROCESS_SLOTS.release()
                self._has_slot = False
                raise
            with self._condition:
                self._peer, self._peer_session = peer, session
                if self._closed or epoch != self._epoch:
                    peer.abort()
                    raise ValueError("identity_session_changed")
                self.reason = "not_started"
            result = self._request(
                peer,
                "init",
                dict(
                    database=str(self.database), models=str(self.models), camera=self.camera, session=session
                ),
                12,
                epoch=epoch,
            )
            if not isinstance(result.get("reason"), str) or not re.fullmatch(
                "[a-z0-9_]{1,64}", result["reason"]
            ):
                raise ValueError("identity_protocol_invalid")
            self.reason = result["reason"]
            self._model_used_at = self.clock()
        return self._peer

    def _rows(self, result):
        rows = result.get("rows")
        diagnostics, reason = result.get("diagnostics"), result.get("reason")
        if (
            not isinstance(rows, list)
            or len(rows) > 16
            or not isinstance(reason, str)
            or not re.fullmatch("[a-z0-9_]{1,64}", reason)
            or not isinstance(diagnostics, dict)
            or len(diagnostics) > 64
            or any(
                not isinstance(k, str)
                or not re.fullmatch("[a-z0-9_]{1,64}", k)
                or type(v) is not int
                or not 0 <= v <= 1_000_000_000
                for k, v in diagnostics.items()
            )
        ):
            raise ValueError("identity_protocol_invalid")
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "track",
                    "person_id",
                    "state",
                    "observed_monotonic",
                    "face_observed_monotonic",
                    "box",
                    "facing",
                    "enroll_ready",
                    "basis",
                }
                or not isinstance(row.get("track"), str)
                or not REF.fullmatch(row["track"])
                or row.get("state") not in {"unknown", "ambiguous", "candidate", "matched", "tracked"}
                or type(row.get("observed_monotonic")) not in (int, float)
                or not math.isfinite(row["observed_monotonic"])
                or type(row.get("face_observed_monotonic")) not in (int, float)
                or not math.isfinite(row["face_observed_monotonic"])
                or row["basis"] not in {"face_similarity", "body_continuity"}
                or type(row["enroll_ready"]) is not bool
                or row["facing"] is not None
                and type(row["facing"]) is not bool
                or row["person_id"] is not None
                and (not isinstance(row["person_id"], str) or not REF.fullmatch(row["person_id"]))
                or row["state"] in {"matched", "tracked"}
                and row["person_id"] is None
                or not isinstance(row["box"], dict)
                or set(row["box"]) != {"x", "y", "w", "h"}
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in row["box"].values()
                )
            ):
                raise ValueError("identity_protocol_invalid")
        if len({row["track"] for row in rows}) != len(rows):
            raise ValueError("identity_protocol_invalid")
        return copy.deepcopy(rows)

    def _run(self):
        try:
            while True:
                with self._condition:
                    if self._closed:
                        break
                    no_work = not self._clear and not self._controls and self._latest is None
                    idle = (
                        no_work
                        and self._peer is not None
                        and self.clock() - self._model_used_at >= IDLE_SECONDS
                    )
                    if no_work and not idle:
                        self._condition.wait(0.2)
                        continue
                    clears, self._clear = self._clear, set()
                    control = self._controls.pop(0) if self._controls else None
                    work, self._latest = self._latest, None
                    epoch = self._epoch
                try:
                    if idle:
                        self._drop_peer()
                        self.reason = "not_started"
                    retry_clear = False
                    for session in clears:
                        if (
                            self._peer is not None
                            and self._peer_session == session
                            and not self._peer.aborted
                        ):
                            self._request(self._peer, "discard", {}, 1)
                        if not retract_presence(self.database, self.camera, session):
                            with self._condition:
                                self._clear.add(session)
                            retry_clear = True
                    if retry_clear and control is None and work is None:
                        with self._condition:
                            self._condition.wait(0.2)
                    if control is not None:
                        version, expires, value, future = control
                        if version != self._epoch or expires < self.clock() or future.done():
                            if not future.done():
                                future.set_exception(ValueError("identity_session_changed"))
                        else:
                            peer = self._ensure_peer(version, self._session)
                            result = self._request(
                                peer, "enroll", dict(value, expires=expires), 1, epoch=version
                            )
                            if (
                                set(result) != {"status", "person_id", "samples", "policy"}
                                or result["status"] != "enrolled"
                                or result["person_id"] != value["person"]
                                or type(result["samples"]) is not int
                                or not 3 <= result["samples"] <= 5
                                or result["policy"] != "sface-household-experimental-v1"
                            ):
                                raise ValueError("identity_protocol_invalid")
                            if version != self._epoch:
                                raise ValueError("identity_session_changed")
                            if not future.done():
                                future.set_result(result)
                            self._model_used_at = self.clock()
                    if work is None:
                        continue
                    version, session, jpeg, people, faces, observed = work
                    if version != self._epoch or not 0 <= self.clock() - observed <= 1:
                        continue
                    if self.clock() < self._retry_at:
                        continue
                    peer = self._ensure_peer(version, session)
                    if version != self._epoch or not 0 <= self.clock() - observed <= 1:
                        continue
                    result = self._request(
                        peer,
                        "frame",
                        dict(
                            jpeg=base64.b64encode(jpeg).decode(),
                            people=people,
                            faces=faces,
                            moving=False,
                            sample=True,
                            captured_at=observed,
                        ),
                        0.8,
                        epoch=version,
                    )
                    rows = self._rows(result)
                    self._model_used_at = self.clock()
                    with self._condition:
                        if not self._closed and version == self._epoch and self._session == session:
                            self._published, self._published_epoch = tuple(rows), version
                            self._diagnostics = dict(result.get("diagnostics", {}))
                            self.reason = result.get("reason", "identity_request_unavailable")
                            self._counts["processed"] += 1
                except Exception as error:
                    with self._condition:
                        self._published = ()
                        self._counts["failed"] += 1
                        if epoch == self._epoch:
                            reason = (
                                str(error)
                                if isinstance(error, ValueError) and str(error) in UNAVAILABLE_REASONS
                                else "identity_request_unavailable"
                            )
                            self.reason = reason
                            configuration_missing = reason in {
                                "identity_model_unavailable",
                                "artifact_or_native_configuration_invalid",
                                "selected_model_fingerprint_mismatch",
                                "optional_dependency_missing",
                            }
                            self._retry_at = self.clock() + (30 if configuration_missing else 5)
                    if control is not None and not control[3].done():
                        control[3].set_exception(ValueError("identity_enrollment_unavailable"))
                    try:
                        self._drop_peer()
                    except Exception:
                        self.reason = "identity_cleanup_pending"
                        break
                finally:
                    # A reaper may outlive a failed source. It retains process
                    # ownership, never the last frame or its encoded payload.
                    work = jpeg = people = faces = result = rows = None
        finally:
            try:
                while self._peer is not None:
                    try:
                        self._drop_peer()
                    except Exception:
                        # Retain ownership and the global slot until the exact
                        # child is reaped. Camera shutdown itself waits at most2s.
                        self.reason = "identity_cleanup_pending"
                        with self._condition:
                            self._counts["cleanup_pending"] = 1
                        time.sleep(0.2)
            finally:
                with self._condition:
                    clears, self._clear = self._clear, set()
                failed = {
                    session for session in clears if not retract_presence(self.database, self.camera, session)
                }
                with self._condition:
                    self._clear.update(failed)
                    self._counts["cleanup_pending"] = len(failed)
                    self._cleanup_ok = self._peer is None and not failed
                    if failed:
                        self.reason = "identity_cleanup_pending"
                    for _, _, _, future in self._controls:
                        if not future.done():
                            future.set_exception(ValueError("identity_session_changed"))
                    self._controls.clear()
