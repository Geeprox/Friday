"""Local safety candidates, bounded evidence, and one selected speech destination.

Geometry is a triage signal, never a diagnosis or proof of abusive intent.
No camera is opened here. Only the perception owner may supply observations.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .safety_storage import SafetyIO, SafetyStorageBusy


class SafetyAdmissionChanged(ValueError):
    pass


class SafetyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    announce: bool = False
    visual_review: bool = False
    sound_review: bool = False
    revision: int = Field(default=0, ge=0)


CAPABILITIES = [
    dict(
        id="fall",
        name="疑似跌倒",
        status="experimental",
        severity="urgent",
        basis="持续直立后快速倒下，并持续倒卧；需要清晰骨骼和稳定镜头",
    ),
    dict(id="crying", name="持续哭闹", status="unavailable", basis="尚未接入哭声时序分类器"),
    dict(
        id="infant_face_covered",
        name="婴儿口鼻疑似被遮挡",
        status="unavailable",
        basis="需要婴儿、面部与织物遮挡的连续视觉证据",
    ),
    dict(
        id="rough_handling",
        name="疑似粗暴照护或击打",
        status="unavailable",
        basis="需要照护人与婴儿的交互时序证据；骨骼不能判断力度或意图",
    ),
    dict(
        id="visual_review", name="本机画面复核", status="experimental",
        basis="有限时间窗口识别可见变化；两段一致后留作人工复核，不自动播报",
    ),
    dict(id="sound_review", name="声音复核", status="experimental",
         basis="本机分类器支持的疑似哭声、尖叫、破碎声或警报；两段独立声音一致后留档待核对"),
]


class SafetyStore:
    MAX_EVENTS = 100
    MAX_BYTES = 64 * 1024 * 1024
    MAX_FRAME = 512 * 1024
    RETENTION = 30 * 86400

    def __init__(self, path):
        self.path = Path(path)
        self.last_prune = 0

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("safety_private_path_required")
        db = sqlite3.connect(self.path, timeout=2)
        try:
            self.path.chmod(0o600)
            db.execute("PRAGMA secure_delete=ON")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS safety_policy (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS safety_events
              (id TEXT PRIMARY KEY, at REAL NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS safety_frames
              (event TEXT NOT NULL, ordinal INTEGER NOT NULL, at REAL NOT NULL, jpeg BLOB NOT NULL,
               PRIMARY KEY(event, ordinal));
            CREATE TABLE IF NOT EXISTS safety_output
              (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
            """)
            with db:
                yield db
        finally:
            db.close()

    def policy(self):
        with self.connect() as db:
            if time.monotonic() - self.last_prune >= 60:
                self._prune(db)
                self.last_prune = time.monotonic()
            row = db.execute("SELECT value FROM safety_policy WHERE id=1").fetchone()
        return SafetyPolicy.model_validate_json(row[0]) if row else SafetyPolicy()

    def configure(self, policy):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM safety_policy WHERE id=1").fetchone()
            current = SafetyPolicy.model_validate_json(row[0]) if row else SafetyPolicy()
            if current.revision != policy.revision:
                raise ValueError("safety_policy_changed")
            value = policy.model_copy(update={"revision": current.revision + 1})
            db.execute("INSERT OR REPLACE INTO safety_policy VALUES (1,?)", (value.model_dump_json(),))
        return value

    def _prune(self, db):
        db.execute("DELETE FROM safety_events WHERE at < ?", (time.time() - self.RETENTION,))
        db.execute(
            "DELETE FROM safety_events WHERE id NOT IN "
            "(SELECT id FROM safety_events ORDER BY at DESC LIMIT ?)",
            (self.MAX_EVENTS,),
        )
        db.execute("DELETE FROM safety_frames WHERE event NOT IN (SELECT id FROM safety_events)")
        while (
            db.execute("SELECT COALESCE(SUM(LENGTH(jpeg)),0) FROM safety_frames").fetchone()[0]
            > self.MAX_BYTES
        ):
            db.execute(
                "DELETE FROM safety_events WHERE id=(SELECT id FROM safety_events ORDER BY at LIMIT 1)"
            )
            db.execute("DELETE FROM safety_frames WHERE event NOT IN (SELECT id FROM safety_events)")

    @staticmethod
    def _admit(db, *, policy_revision=None, feature=None, guard=None):
        if guard is not None and not guard():
            raise SafetyAdmissionChanged('safety_source_changed')
        if policy_revision is not None:
            row = db.execute('SELECT value FROM safety_policy WHERE id=1').fetchone()
            policy = SafetyPolicy.model_validate_json(row[0]) if row else SafetyPolicy()
            if (not policy.enabled or policy.revision != policy_revision
                    or feature is not None and not getattr(policy, feature)):
                raise SafetyAdmissionChanged('safety_policy_changed')

    @staticmethod
    def same_recording(saved, event):
        keys = ('kind', 'camera', 'session', 'track', 'started_mono', 'captured_at',
                'camera_source_context', 'basis', 'detector', 'sound_kind', 'sound_evidence',
                'visual_kind', 'observation')
        return all(saved.get(key) == event.get(key) for key in keys)

    def create(self, event, frames=(), *, policy_revision=None, feature=None, guard=None, event_id=None):
        if event_id is not None:
            kind = event.get('kind')
            scoped_review = kind in ('sound_review', 'visual_review') and feature == kind and policy_revision is not None
            if (not isinstance(event_id, str) or not re.fullmatch(r'[a-f0-9]{32}', event_id)
                    or kind != 'fall' and not scoped_review):
                raise ValueError('safety_event_key_invalid')
        event = dict(
            event,
            id=event_id or secrets.token_hex(16),
            at=time.time(),
            review="unreviewed",
            announcement={"status": "not_requested", "physical_audio_confirmed": False},
            evidence_kind="keyframes",
            clip_available=False,
        )
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._admit(db, policy_revision=policy_revision, feature=feature, guard=guard)
            if event_id is not None:
                existing = db.execute('SELECT value FROM safety_events WHERE id=?', (event_id,)).fetchone()
                if existing:
                    saved = json.loads(existing[0])
                    if not self.same_recording(saved, event):
                        raise SafetyAdmissionChanged('safety_event_changed')
                    return saved
            db.execute(
                "INSERT INTO safety_events VALUES (?,?,?)",
                (event["id"], event["at"], json.dumps(event, ensure_ascii=False)),
            )
            for ordinal, (stamp, jpeg) in enumerate(list(frames)[-16:]):
                if isinstance(jpeg, bytes) and 0 < len(jpeg) <= self.MAX_FRAME:
                    db.execute(
                        "INSERT INTO safety_frames VALUES (?,?,?,?)", (event["id"], ordinal, stamp, jpeg)
                    )
            self._prune(db)
        return event

    def update(self, event_id, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM safety_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise ValueError("safety_event_unavailable")
            previous = json.loads(row[0])
            value = dict(previous, **fields)
            if previous.get('announcement_withdrawn') or fields.get('review') == 'false_alarm':
                # Changing a review afterwards does not authorize an old
                # pending announcement again, including from another process.
                value['announcement_withdrawn'] = True
            db.execute(
                "UPDATE safety_events SET value=? WHERE id=?",
                (json.dumps(value, ensure_ascii=False), event_id),
            )
        return value

    def event(self, event_id):
        with self.connect() as db:
            row = db.execute("SELECT value FROM safety_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise ValueError("safety_event_unavailable")
        return json.loads(row[0])

    def recording_receipt(self, event_id):
        """Read only this incident's metadata; no keyframe pixels or NAS access."""
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT value FROM safety_events WHERE id=?', (event_id,)).fetchone()
            if not row:
                return None
            stamps = [value[0] for value in db.execute(
                'SELECT at FROM safety_frames WHERE event=? ORDER BY ordinal LIMIT 22', (event_id,))]
        return dict(event=json.loads(row[0]), stamps=stamps)

    def append_frame(self, event_id, stamp, jpeg, *, policy_revision=None, guard=None, deduplicate=False):
        if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= self.MAX_FRAME:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admit(db, policy_revision=policy_revision, guard=guard)
            if not db.execute("SELECT 1 FROM safety_events WHERE id=?", (event_id,)).fetchone():
                return
            if deduplicate:
                previous = db.execute('SELECT jpeg FROM safety_frames WHERE event=? AND at=?',
                                      (event_id, stamp)).fetchone()
                if previous:
                    if previous[0] != jpeg:
                        raise SafetyAdmissionChanged('safety_frame_changed')
                    return
            count = db.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM safety_frames WHERE event=?", (event_id,)
            ).fetchone()[0]
            if count < 22:
                db.execute("INSERT INTO safety_frames VALUES (?,?,?,?)", (event_id, count, stamp, jpeg))
            self._prune(db)

    def claim_output(self, owner, *, policy_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._admit(db, policy_revision=policy_revision, feature='announce')
            event = db.execute('SELECT value FROM safety_events WHERE id=?', (owner,)).fetchone()
            if event:
                current = json.loads(event[0])
                if current.get('review') == 'false_alarm' or current.get('announcement_withdrawn'):
                    raise SafetyAdmissionChanged('safety_review_changed')
            row = db.execute("SELECT expires FROM safety_output WHERE id=1").fetchone()
            if row and row[0] > time.time():
                return False
            db.execute("INSERT OR REPLACE INTO safety_output VALUES (1,?,?)", (owner, time.time() + 40))
        return True

    def release_output(self, owner):
        with self.connect() as db:
            db.execute("DELETE FROM safety_output WHERE owner=?", (owner,))

    def output_current(self, event_id, revision=None):
        """One metadata snapshot for pending speech, including other processes' reviews."""
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT value FROM safety_events WHERE id=?', (event_id,)).fetchone()
            if row is None:
                return False
            event = json.loads(row[0])
            if event.get('review') == 'false_alarm' or event.get('announcement_withdrawn'):
                return False
            if revision is not None:
                row = db.execute('SELECT value FROM safety_policy WHERE id=1').fetchone()
                policy = SafetyPolicy.model_validate_json(row[0]) if row else SafetyPolicy()
                if not policy.enabled or not policy.announce or policy.revision != revision:
                    return False
        return True

    def events(self):
        with self.connect() as db:
            self._prune(db)
            rows = db.execute(
                "SELECT value FROM safety_events ORDER BY at DESC LIMIT ?", (self.MAX_EVENTS,)
            ).fetchall()
            return [
                dict(
                    json.loads(row[0]),
                    frames=[
                        dict(ordinal=n, at=stamp)
                        for n, stamp in db.execute(
                            "SELECT ordinal,at FROM safety_frames WHERE event=? ORDER BY ordinal",
                            (json.loads(row[0])["id"],),
                        )
                    ],
                )
                for row in rows
            ]

    def frame(self, event_id, ordinal):
        with self.connect() as db:
            self._prune(db)
            row = db.execute(
                "SELECT jpeg FROM safety_frames WHERE event=? AND ordinal=?", (event_id, ordinal)
            ).fetchone()
        if not row:
            raise ValueError("safety_frame_unavailable")
        return row[0]


class FallDetector:
    """Continuous upright -> rapid descent -> sustained horizontal torso.

    All thresholds are experimental image geometry, without probability claims.
    Gaps, camera motion, bad joints and session changes invalidate a candidate.
    """

    MAX_FRAME_GAP = 0.75

    def __init__(self):
        self.cameras = {}

    def clear(self, camera):
        self.cameras.pop(camera, None)

    @staticmethod
    def geometry(person, aspect):
        joints, box = person.get("joints", {}), person.get("box", {})
        if not isinstance(joints, dict) or not isinstance(box, dict):
            return None
        pts = [joints.get(side + part) for part in ("Shoulder", "Hip") for side in ("left", "right")]
        if any(
            not isinstance(p, dict)
            or type(p.get("confidence")) not in (int, float)
            or not math.isfinite(p["confidence"])
            or not 0.55 <= p["confidence"] <= 1
            or any(
                not isinstance(p.get(k), (int, float)) or not math.isfinite(p[k]) or not 0 <= p[k] <= 1
                for k in ("x", "y")
            )
            for p in pts
        ):
            return None
        if any(
            not isinstance(box.get(k), (int, float)) or not math.isfinite(box[k])
            for k in ("x", "y", "w", "h")
        ):
            return None
        confidence = box.get("confidence")
        if (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0.65 <= confidence <= 1
            or box["h"] < 0.12
            or box["w"] < 0.04
        ):
            return None
        sx, sy = (pts[0]["x"] + pts[1]["x"]) / 2, (pts[0]["y"] + pts[1]["y"]) / 2
        hx, hy = (pts[2]["x"] + pts[3]["x"]) / 2, (pts[2]["y"] + pts[3]["y"]) / 2
        dx, dy = abs(sx - hx) * aspect, abs(sy - hy)
        if math.hypot(dx, dy) < 0.06:
            return None
        upright = dy > dx * 1.6 and sy < hy and box["h"] > box["w"] * aspect * 1.15
        horizontal = dx > dy * 1.5 and box["w"] * aspect > box["h"] * 1.15
        return dict(upright=upright, horizontal=horizontal, center=(sy + hy) / 2, height=box["h"])

    @staticmethod
    def current_identity(person, evaluated_mono):
        identity = person.get('identity', {})
        if not isinstance(identity, dict) or identity.get('state') not in {'matched', 'tracked'}:
            return None
        seen, face = identity.get('observed_monotonic'), identity.get('face_observed_monotonic')
        if (type(seen) not in (int, float) or type(face) not in (int, float)
                or not 0 <= evaluated_mono - seen <= 1 or not 0 <= evaluated_mono - face <= 5):
            return None
        person_id = identity.get('person_id')
        return person_id if isinstance(person_id, str) and person_id else None

    @staticmethod
    def _descent(track, geometry, now):
        anchor = track["anchor"]
        return geometry["horizontal"] and (
            track["down_since"] is not None
            or anchor is not None and now - anchor["at"] <= 1.2
            and geometry["center"] - anchor["center"] >= anchor["height"] * 0.25
        )

    def needs_stable_view(self, camera, session, queued=None):
        """用最新一帧判断是否需要维持视角，不消耗跌倒确认进度。"""
        state = self.cameras.get(camera)
        if state is None or state["session"] != session:
            return False
        if queued is None:
            return any(not track["fired"] and track["down_since"] is not None
                       for track in state["tracks"].values())
        people, now, aspect = queued
        if not 0 < now - state["last"] <= self.MAX_FRAME_GAP or not 0.2 <= aspect <= 5:
            return False
        for person in people[:10]:
            track = state["tracks"].get(str(person.get("id"))) if isinstance(person, dict) else None
            if track is None or track["fired"] or not 0 < now - track["last"] <= self.MAX_FRAME_GAP:
                continue
            geometry = self.geometry(person, aspect)
            if geometry is not None and self._descent(track, geometry, now):
                return True
        return False

    def observe(self, camera, session, people, now, captured, *, stable, aspect=16 / 9, evaluated_mono=None):
        evaluated_mono = now if evaluated_mono is None else evaluated_mono
        if (
            not stable
            or not math.isfinite(now)
            or not math.isfinite(aspect)
            or not 0.2 <= aspect <= 5
            or not 0 <= now - captured <= 1.5
            or not math.isfinite(evaluated_mono)
            or not 0 <= evaluated_mono - captured <= 1.5
        ):
            self.clear(camera)
            return []
        state = self.cameras.get(camera)
        if state is None or state["session"] != session or not 0 < now - state["last"] <= self.MAX_FRAME_GAP:
            state = dict(session=session, last=now, tracks={})
            self.cameras[camera] = state
        state["last"] = now
        found, present = [], set()
        for person in people[:10]:
            track = str(person["id"])
            present.add(track)
            geometry = self.geometry(person, aspect)
            if geometry is None:
                state["tracks"].pop(track, None)
                continue
            old = state["tracks"].get(track)
            if old is None or not 0 < now - old["last"] <= self.MAX_FRAME_GAP:
                old = dict(last=now, upright_since=None, anchor=None, down_since=None, fired=False)
                state["tracks"][track] = old
            old["last"] = now
            if geometry["upright"]:
                old["upright_since"] = old["upright_since"] if old["upright_since"] is not None else now
                if now - old["upright_since"] >= 1:
                    old["anchor"] = dict(at=now, **geometry)
                old["down_since"] = None
                # One event per continuous episode; rearm after sustained recovery.
                if now - old["upright_since"] >= 3:
                    old["fired"] = False
                continue
            old["upright_since"] = None
            if not self._descent(old, geometry, now):
                old["down_since"] = None
                continue
            if old["down_since"] is None:
                old["down_since"] = now
            if now - old["down_since"] >= 2 and not old["fired"]:
                old["fired"] = True
                pid = self.current_identity(person, evaluated_mono)
                found.append(
                    dict(
                        kind="fall",
                        severity="urgent",
                        camera=camera,
                        session=session,
                        track=track,
                        person_id=pid,
                        simulated=False,
                        status="suspected",
                        basis="upright_rapid_descent_sustained_horizontal",
                        detector="geometry-fall/1",
                        started_mono=old["anchor"]["at"],
                    )
                )
        state["tracks"] = {k: v for k, v in state["tracks"].items() if k in present}
        return found


class SafetyRuntime:
    MAX_CAMERAS = 8
    MAX_PEOPLE = 32
    POLICY_TTL = 2

    def __init__(self, store, *, labels=lambda: {}, speaker=None, visual_engine=None, sound_engine=None):
        self.store, self.labels, self.speaker = store, labels, speaker
        self.io = SafetyIO()
        self.closed = False
        self.close_task = None
        self.detector = FallDetector()
        self.frames = {}
        self.post_frames = {}
        self.tasks = set()
        self.last_seen = {}
        self.source_epochs = {}
        self.source_tokens = {}
        self.sources = {}
        self.pending = OrderedDict()
        self.observation_task = None
        self.coalesced = self.skipped = 0
        self.output_revisions = {}
        self.policy_revision = None
        self.policy_task = None
        self.policy_lock = asyncio.Lock()
        self.error = None
        self.policy_cache = SafetyPolicy()
        self.policy_read_at = -math.inf
        self.policy_failure_since = None
        from .safety_recording import SafetyRecorder
        self.recording = SafetyRecorder(persist=self._persist_recording, reconcile=self._recording_receipt,
                                        current=self._recording_current,
                                        clock=lambda: time.monotonic())
        from .temporal_review import TemporalReviewer
        self.visual = TemporalReviewer(policy=self._cached_policy, deliver=self._visual_event, engine=visual_engine)
        from .sound_review import SoundReviewer
        self.sound = SoundReviewer(policy=self._cached_policy, current=self._sound_current,
                                   deliver=self._sound_event, engine=sound_engine)

    def _cached_policy(self):
        if self.closed or not 0 <= time.monotonic() - self.policy_read_at <= self.POLICY_TTL:
            return SafetyPolicy(revision=self.policy_cache.revision)
        return self.policy_cache

    @staticmethod
    def _context(value):
        return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) else None

    def _guard(self, camera, session, context, captured, *, max_age=1.5):
        token = self.source_tokens.get(camera)

        def current():
            ring = self.frames.get(camera, {})
            now = time.monotonic()
            return (not self.closed and token is not None and not token.is_set()
                    and self.source_tokens.get(camera) is token
                    and ring.get('session') == session and ring.get('source_context') == context
                    and 0 <= now - self.last_seen.get(camera, -math.inf) <= 1.5
                    and 0 <= now - captured <= max_age)

        return current

    def offer(self, camera, session, people, now, captured, *, stable, jpeg=b'', aspect=16 / 9,
              source_context=None, source_name=None):
        """Perception never waits for storage; retain only each camera's latest frame."""
        if self.closed:
            return False
        if (not stable or not isinstance(people, list)
                or len(people) > self.MAX_PEOPLE or type(captured) not in (int, float)
                or not math.isfinite(captured) or not 0 <= time.monotonic() - captured <= 1.5):
            self.clear(camera)
            return False
        if camera not in self.sources and len(self.sources) >= self.MAX_CAMERAS:
            self.skipped += 1
            return False
        source_context = self._context(source_context)
        source = (session, source_context)
        if camera in self.sources and self.sources[camera] != source:
            self.clear(camera)
        previous_seen = self.last_seen.get(camera)
        if previous_seen is not None and not 0 < captured - previous_seen <= 1.5:
            self.clear(camera)
        self.sources[camera] = source
        token = self.source_tokens.setdefault(camera, threading.Event())
        self.last_seen[camera] = captured
        if not self._cached_policy().enabled:
            if self.policy_cache.enabled and self.recording.has_camera(camera):
                # Keep only the receipt of live input for an already qualified
                # recording. Unknown policy admits no new detection or frames;
                # the original DB transaction still checks its exact revision.
                self.pending.pop(camera, None)
                self.detector.clear(camera)
                self.visual.clear(camera)
                self.sound.clear(camera)
                self.frames.pop(camera, None)
                self.post_frames.pop(camera, None)
                self.recording.wake()
            else:
                self.clear(camera)
            return False
        if camera in self.pending:
            self.coalesced += 1
        self.pending[camera] = (
            (camera, session, copy.deepcopy(people), now, captured),
            dict(stable=stable, jpeg=jpeg if isinstance(jpeg, bytes) and len(jpeg) <= self.store.MAX_FRAME else b'',
                 aspect=aspect, source_context=source_context, source_name=source_name, _admission=token),
        )
        if self.observation_task is None:
            self.observation_task = asyncio.create_task(self._consume(), name='friday-safety-observations')
        return True

    async def _consume(self):
        try:
            while self.pending and not self.closed:
                _, (args, kwargs) = self.pending.popitem(last=False)
                try:
                    await self.observe(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.clear(args[0])
                    self.error = 'safety_observation_failed'
        finally:
            self.observation_task = None

    async def drain(self):
        """Wait for admitted observations, primarily for explicit shutdown/readback."""
        while self.observation_task is not None:
            await asyncio.shield(self.observation_task)
        await self.recording.drain()
        await self.io.drain()

    def _sound_current(self, camera, session, context):
        ring = self.frames.get(camera, {})
        return (self._sound_input_open() and ring.get('session') == session
                and ring.get('source_context') == context and context is not None
                and 0 <= time.monotonic()-self.last_seen.get(camera, -100) <= 1.5)

    def sound_requested(self, camera):
        ring = self.frames.get(camera, {})
        policy = self._cached_policy()
        return bool(policy.enabled and policy.sound_review
                    and self._sound_current(camera, ring.get('session'), ring.get('source_context'))
                    and self.sound.readiness()['available'])

    def _current_review_source(self, camera, session):
        token = self.source_tokens.get(camera)
        source = self.sources.get(camera)
        return (not self.closed and token is not None and not token.is_set()
                and source is not None and source[0] == session and source[1] is not None
                and 0 <= time.monotonic() - self.last_seen.get(camera, -math.inf) <= 1.5)

    def fall_pending(self, camera, session):
        """仅在当前获准来源的疑似跌倒确认或留档期间请求稳定视角。"""
        if not self._current_review_source(camera, session):
            return False
        # 策略暂时读不到时，已准入的留档仍可短暂停留，新的候选需要有效策略。
        if self.policy_cache.enabled and self.recording.pending_fall(camera, session, self.policy_cache.revision):
            return True
        if not self._cached_policy().enabled:
            return False
        queued = self.pending.get(camera)
        if queued is None:
            return self.detector.needs_stable_view(camera, session)
        args, kwargs = queued
        _, _, people, now, captured = args
        aspect = kwargs['aspect']
        if (kwargs['_admission'] is not self.source_tokens.get(camera)
                or type(now) not in (int, float) or not 0 <= now - captured <= 1.5
                or type(aspect) not in (int, float)):
            return False
        return self.detector.needs_stable_view(camera, session, (people, now, aspect))

    def review_pending(self, camera, session):
        """Fresh same-source sound or visual review, without starting a model."""
        policy = self._cached_policy()
        ring = self.frames.get(camera, {})
        current = (policy.enabled and self._current_review_source(camera, session)
                   and self.sources.get(camera) == (ring.get('session'), ring.get('source_context')))
        return bool(current and (
            policy.visual_review and self.visual.stability_pending(camera, session, policy.revision)
            or policy.sound_review and self.sound.stability_pending(camera, session, policy.revision)
            or self.recording.pending_review(camera, session, policy.revision)))

    def observe_audio(self, camera, admission):
        from .camera_audio import AdmittedAudio
        ring = self.frames.get(camera, {})
        if (not self.sound_requested(camera) or not isinstance(admission, AdmittedAudio)
                or admission.packet is None
                or admission.packet.get('camera_source_context') != ring.get('source_context')):
            self.sound.clear(camera)
            return
        self.sound.offer(camera, ring['session'], ring['source_context'], self.policy_cache.revision, admission)

    def _sound_event(self, camera, session, context, result):
        from .sound_model import MESSAGES
        if not self._sound_current(camera, session, context):
            return False
        ring = self.frames[camera]
        return self._submit_review(dict(kind='sound_review', sound_kind=result['kind'], severity='review',
            status='pending_review', simulated=False, camera=camera, session=session, track=None,
            person_id=None, person_name=None, message=MESSAGES[result['kind']],
            camera_source_context=context, camera_name=ring.get('source_name'),
            captured_at=result['captured_at'], detector='apple-soundanalysis-version1/1',
            basis='two_disjoint_local_audio_windows',
            sound_evidence={k:result[k] for k in ('confidence','corroborating_windows','audio_seconds','age_basis')},
            raw_audio_stored=False, directional_audio=False),
            [f for f in ring['items'] if 0 <= time.time()-f[0] <= 8],
            captured=result['captured_mono'], revision=result['policy_revision'], max_age=6)

    def _submit_review(self, event, frames, *, captured, revision, max_age):
        camera, session, context = event['camera'], event['session'], event['camera_source_context']
        feature = event['kind']
        policy = self._cached_policy()
        if (not policy.enabled or policy.revision != revision or not getattr(policy, feature)
                or not self._guard(camera, session, context, captured, max_age=max_age)()):
            return False
        job = self.recording.submit(camera=camera, session=session, context=context, token=self.source_tokens[camera],
            captured=captured, revision=revision, event=event, frames=frames, feature=feature)
        return job.receipt if job is not None else False

    async def pause_sound(self):
        self.recording.clear(feature='sound_review')
        await self.sound.invalidate()

    async def sync_policy(self):
        async with self.policy_lock:
            try:
                policy = await self.io.call(self.store.policy)
            except (OSError, sqlite3.Error, ValueError, TypeError, SafetyStorageBusy):
                if self.policy_failure_since is None:
                    self.policy_failure_since = time.monotonic()
                self.policy_read_at = -math.inf
                raise
            self.policy_cache = policy
            self.policy_read_at = time.monotonic()
            self.policy_failure_since = None
            self.recording.wake()
            if self.error == 'safety_storage_unavailable':
                self.error = None
            if self.policy_revision == policy.revision:
                return
            previous = self.policy_revision
            self.policy_revision = policy.revision
            if previous is not None:
                for camera in set(self.source_tokens) | set(self.pending) | set(self.frames):
                    self.clear(camera)
                for task, revision in tuple(self.output_revisions.items()):
                    if revision is not None and revision != policy.revision:
                        task.cancel()
            self.detector.cameras.clear()
            self.frames.clear()
            self.post_frames.clear()
            self.last_seen.clear()
            await self.visual.invalidate()
            await self.sound.invalidate()

    async def start(self, *, interval=.5):
        if self.policy_task is not None or self.closed:
            return
        async def sync_safely():
            try:
                await self.sync_policy()
                if not self._sound_input_open():
                    await self.pause_sound()
                else:
                    await self.sound.maintain()
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, SafetyStorageBusy):
                self.error = 'safety_storage_unavailable'
                self.policy_read_at = -math.inf
                await self.visual.invalidate()
                await self.sound.invalidate()
        await sync_safely()
        async def watch():
            while True:
                await sync_safely()
                await asyncio.sleep(interval)
        self.policy_task = asyncio.create_task(watch(), name='friday-safety-policy')

    def clear(self, camera):
        token = self.source_tokens.pop(camera, None)
        if token is not None:
            token.set()
        self.pending.pop(camera, None)
        self.sources.pop(camera, None)
        self.source_epochs[camera] = self.source_epochs.get(camera, 0) + 1
        self.visual.clear(camera)
        self.sound.clear(camera)
        self.detector.clear(camera)
        self.frames.pop(camera, None)
        self.post_frames.pop(camera, None)
        self.last_seen.pop(camera, None)
        self.recording.clear(camera)

    async def observe(self, camera, session, people, now, captured, *, stable, jpeg=b"", aspect=16 / 9,
                      source_context=None, source_name=None, _admission=None):
        if self.closed:
            return
        source_context = self._context(source_context)
        if _admission is not None:
            if _admission.is_set() or self.source_tokens.get(camera) is not _admission:
                return
            token = _admission
        else:
            source = (session, source_context)
            if camera in self.sources and self.sources[camera] != source:
                self.clear(camera)
            self.sources[camera] = source
            token = self.source_tokens.setdefault(camera, threading.Event())
            if stable and type(captured) in (int, float) and 0 <= time.monotonic() - captured <= 1.5:
                self.last_seen[camera] = max(captured, self.last_seen.get(camera, -math.inf))
        try:
            await self._observe(
                camera, session, people, now, captured, stable=stable, jpeg=jpeg, aspect=aspect,
                source_context=source_context, source_name=source_name, token=token,
                refresh_policy=_admission is None,
            )
            if not token.is_set():
                self.error = None
        except SafetyAdmissionChanged:
            self.skipped += 1
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, SafetyStorageBusy):
            # A full disk or broken safety database must not stop perception.
            if self.source_tokens.get(camera) is token:
                self.clear(camera)
            self.error = "safety_storage_unavailable"

    async def _observe(self, camera, session, people, now, captured, *, stable, jpeg, aspect,
                       source_context=None, source_name=None, token, refresh_policy=True):
        if refresh_policy:
            await self.sync_policy()
        if token.is_set() or self.source_tokens.get(camera) is not token:
            return
        policy = self._cached_policy()
        if not policy.enabled or not stable or not 0 <= time.monotonic() - captured <= 1.5:
            self.clear(camera)
            return
        if (not isinstance(source_name, str) or not 1 <= len(source_name) <= 80
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in source_name)):
            source_name = None
        self.last_seen[camera] = max(captured, self.last_seen.get(camera, -math.inf))
        if policy.visual_review:
            self.visual.offer(camera, session, policy.revision, captured, jpeg, stable=stable)
        else:
            self.visual.clear(camera)
        ring = self.frames.setdefault(camera, dict(session=session, source_context=source_context,
                                                  items=deque(maxlen=16)))
        ring['source_name'] = source_name
        stamp = time.time() - (time.monotonic() - captured)
        guard = self._guard(camera, session, source_context, captured)
        if (
            jpeg
            and len(jpeg) <= self.store.MAX_FRAME
            and (not ring["items"] or stamp - ring["items"][-1][0] >= 0.5)
        ):
            ring["items"].append((stamp, jpeg))
            pending = self.post_frames.get(camera, [])
            self.post_frames[camera] = [p for p in pending if captured <= p["until"]]
            self.recording.frame(camera, captured, stamp, jpeg)
        events = self.detector.observe(
            camera, session, people, now, captured, stable=stable, aspect=aspect,
            evaluated_mono=time.monotonic(),
        )
        if not guard():
            return
        for event in events:
            event['camera_source_context'] = source_context
            event['camera_name'] = source_name
            event['captured_at'] = stamp
            person = next((p for p in people if str(p.get('id')) == str(event.get('track'))), {})
            identity = person.get('identity', {})
            identity = identity if isinstance(identity, dict) else {}
            person_id = identity.get('person_id')
            person_id = person_id if isinstance(person_id, str) and len(person_id) <= 128 else None
            state = identity.get('state')
            identity = dict(person_id=person_id, state=state if isinstance(state, str) else None,
                **{key: value if type(value := identity.get(key)) in (int, float) and math.isfinite(value) else None
                   for key in ('observed_monotonic', 'face_observed_monotonic')})
            if event.get('person_id') != person_id:
                event['person_id'] = None
            job = self.recording.submit(camera=camera, session=session, context=source_context,
                token=token, captured=captured, revision=policy.revision, event=event,
                person={'identity': identity},
                frames=[f for f in ring['items'] if 0 <= time.time() - f[0] <= 8], announce=policy.announce)
            if job is not None:
                self.post_frames.setdefault(camera, []).append(
                    dict(id=job.identifier, until=captured + self.recording.POST_WINDOW))

    def _recording_current(self, job):
        return (not self.closed and not job.token.is_set()
                and (job.receipt is None or job.receipt.pending)
                and (job.feature != 'sound_review' or self._sound_input_open())
                and self.source_tokens.get(job.camera) is job.token
                and self.sources.get(job.camera) == (job.session, job.context)
                and 0 <= time.monotonic() - self.last_seen.get(job.camera, -math.inf) <= 1.5
                and 0 <= time.monotonic() - job.captured <= self.recording.RETRY_WINDOW)

    async def _persist_recording(self, job):
        if job.saved is None:
            event = dict(job.event)
            if job.feature is None:
                labels = await self.io.call(self.labels)
                if event['person_id'] != FallDetector.current_identity(job.person, time.monotonic()):
                    event['person_id'] = None
                name = labels.get(event['person_id']) if event['person_id'] is not None else None
                event.update(person_name=name, message=f'检测到 {name} 疑似跌倒，请立即查看。' if name
                             else '有人疑似跌倒，请立即查看。')
            job.saved = await self.io.call(self.store.create, event, job.initial, event_id=job.identifier,
                policy_revision=job.revision, feature=job.feature, guard=lambda: self._recording_current(job))
            self.recording.recorded(job)
        if job.feature is not None or not self._recording_current(job):
            return
        if not job.output_considered:
            job.output_considered = True
            state = self.detector.cameras.get(job.camera, {}).get('tracks', {}).get(str(job.event.get('track')), {})
            if (job.announce and 0 <= time.monotonic() - job.captured <= 1.5
                    and state.get('fired') and state.get('down_since') is not None):
                await self.schedule(job.saved, revision=job.revision)
        while job.appended < len(job.following) and self._recording_current(job):
            stamp, jpeg = job.following[job.appended]
            await self.io.call(self.store.append_frame, job.identifier, stamp, jpeg,
                policy_revision=job.revision, guard=lambda: self._recording_current(job), deduplicate=True)
            job.appended += 1

    async def _recording_receipt(self, job):
        receipt = await self.io.call(self.store.recording_receipt, job.identifier)
        if receipt is not None and not self.store.same_recording(receipt['event'], job.event):
            raise SafetyAdmissionChanged('safety_event_changed')
        return receipt

    def _visual_event(self, camera, session, frames, result, captured_at):
        messages = {
            "possible_fall": "画面中疑似出现跌落动作，请查看关键帧。",
            "face_obscured": "画面中疑似出现面部遮挡，请查看关键帧。",
            "rapid_movement": "画面中疑似出现快速移动或搬动，请查看关键帧。",
        }
        # Model text remains private evidence. It cannot supply identity or speech.
        ring = self.frames.get(camera, {})
        source_context = ring.get('source_context') if ring.get('session') == session else None
        policy = self._cached_policy()
        return self._submit_review(
            dict(
                kind="visual_review", visual_kind=result.event, severity="review",
                status="pending_review", simulated=False, camera=camera, session=session,
                track=None, person_id=None, person_name=None,
                message=messages[result.event], observation=result.observation,
                captured_at=captured_at, basis="two_disjoint_local_windows",
                camera_source_context=source_context,
                camera_name=ring.get('source_name') if ring.get('session') == session else None,
                detector="qwen3.5-2b-window/1",
            ),
            [(captured_at - (frames[-1][0] - stamp), jpeg) for stamp, jpeg in frames],
            captured=frames[-1][0], revision=policy.revision, max_age=12,
        )

    async def schedule(self, event, revision=None):
        # Bound pending output independently of the perception frame rate.
        if self.tasks:
            await self.io.call(self.store.update, event["id"], announcement={"status": "busy", "physical_audio_confirmed": False})
            return
        task = asyncio.create_task(self.announce(event, revision))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.output_revisions[task] = revision
        task.add_done_callback(lambda job: self.output_revisions.pop(job, None))

    async def announce(self, event, revision=None):
        status = "unavailable"
        claimed = False
        try:
            policy = await self.io.call(self.store.policy)
            if (await self.io.call(self.store.event, event["id"])).get("review") == "false_alarm":
                status = "cancelled"
            elif revision is not None and (
                not policy.enabled or not policy.announce or revision != policy.revision
            ):
                status = "cancelled"
            elif not await self.io.call(self.store.claim_output, event["id"], policy_revision=revision):
                status = "busy"
            else:
                claimed = True
                from .speech import Speaker

                speaker = self.speaker or Speaker()
                await self.io.call(self.store.update,
                    event["id"], announcement={"status": "requesting", "physical_audio_confirmed": False}
                )
                current_policy = await self.io.call(self.store.policy)
                current_event = await self.io.call(self.store.event, event['id'])
                if (current_event.get('review') == 'false_alarm' or revision is not None and (
                        current_policy.revision != revision or not current_policy.enabled or not current_policy.announce)):
                    status = 'cancelled'
                    return
                async def revalidate():
                    try:
                        return await self.io.call(self.store.output_current, event['id'], revision)
                    except (OSError, sqlite3.Error, ValueError, SafetyStorageBusy):
                        self.error = 'safety_receipt_unavailable'
                        return False

                receipt = await asyncio.wait_for(speaker.say(event["message"],
                    current=lambda: not self.closed, revalidate=revalidate), 35)
                status = receipt.get("status", "unavailable")
                status = {"muted": "paused", "expired": "cancelled"}.get(status, status)
                if status not in {"accepted", "paused", "busy", "unavailable", "unconfirmed", "cancelled"}:
                    status = "unavailable"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except SafetyAdmissionChanged:
            status = 'cancelled'
        except Exception:
            status = "unavailable"
        finally:
            try:
                if claimed:
                    await self.io.call(self.store.release_output, event["id"])
                await self.io.call(self.store.update,
                    event["id"], announcement={"status": status, "physical_audio_confirmed": False}
                )
            except (OSError, sqlite3.Error, ValueError, SafetyStorageBusy):
                self.error = "safety_receipt_unavailable"

    async def test(self):
        event = await self.io.call(self.store.create,
            dict(
                kind="fall",
                severity="test",
                simulated=True,
                status="test",
                camera=None,
                session=None,
                track=None,
                person_id=None,
                person_name=None,
                message="测试播报：有人疑似跌倒，请立即查看。",
                basis="manual_audio_test",
                detector=None,
            )
        )
        await self.schedule(event)
        return event

    def recording_status(self):
        """Cheap frame/Console projection; never probes optional native models."""
        report = self.recording.status()
        current = not self.closed and 0 <= time.monotonic() - self.policy_read_at <= self.POLICY_TTL
        failed_for = (max(0, time.monotonic() - self.policy_failure_since)
                      if self.policy_failure_since is not None else 0)
        state = ('unavailable' if report['dropped'] or report['partial'] or report['unconfirmed'] or failed_for >= self.recording.RETRY_WINDOW
                 else 'recovering' if not current or report['error']
                 else 'off' if not self.policy_cache.enabled
                 else 'saving' if report['pending'] else 'ready')
        return dict(report, state=state, enabled=self.policy_cache.enabled if current else None,
                    policy_current=current)

    def status(self):
        for camera, seen in list(self.last_seen.items()):
            if not 0 <= time.monotonic() - seen <= 2:
                self.clear(camera)
        recording = self.recording_status()
        return dict(
            cameras=[
                dict(camera=camera, fresh=0 <= time.monotonic() - seen <= 2)
                for camera, seen in self.last_seen.items()
            ],
            pending_announcements=len(self.tasks),
            pending_observations=len(self.pending),
            coalesced_observations=self.coalesced,
            skipped_observations=self.skipped,
            pending_storage=self.io.pending,
            error=self.error or recording['error'],
            recording=recording,
            visual_review=self.visual.status(),
            sound_review=dict(self.sound.status(),input_paused=not self._sound_input_open()),
        )

    def _sound_input_open(self):
        from .paths import PRIVATE
        return not (PRIVATE/'voice-input-paused').exists()

    async def close(self):
        if self.close_task is None:
            self.closed = True
            for camera in set(self.source_tokens) | set(self.pending) | set(self.frames):
                self.clear(camera)
            self.close_task = asyncio.create_task(self._close(), name='friday-safety-close')
        cancelled = False
        while True:
            try:
                await asyncio.shield(self.close_task)
                break
            except asyncio.CancelledError:
                if self.close_task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self):
        failures = []
        try:
            owners = [task for task in (self.observation_task, self.policy_task) if task is not None]
            for task in owners:
                task.cancel()
            failures.extend(await asyncio.gather(*owners, return_exceptions=True))
            self.policy_task = None
            failures.extend(await asyncio.gather(self.recording.close(), return_exceptions=True))
            failures.extend(await asyncio.gather(self.visual.close(), self.sound.close(), return_exceptions=True))
            outputs = tuple(self.tasks)
            for task in outputs:
                task.cancel()
            failures.extend(await asyncio.gather(*outputs, return_exceptions=True))
        finally:
            await self.io.close()
        if any(isinstance(result, Exception) for result in failures):
            self.error = 'safety_cleanup_unavailable'
            raise RuntimeError(self.error)
