"""Local safety candidates, bounded evidence, and one selected speech destination.

Geometry is a triage signal, never a diagnosis or proof of abusive intent.
No camera is opened here. Only the perception owner may supply observations.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SafetyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    announce: bool = False
    visual_review: bool = False
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
        try:
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

    def create(self, event, frames=()):
        event = dict(
            event,
            id=secrets.token_hex(16),
            at=time.time(),
            review="unreviewed",
            announcement={"status": "not_requested", "physical_audio_confirmed": False},
            evidence_kind="keyframes",
            clip_available=False,
        )
        with self.connect() as db:
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
            value = dict(json.loads(row[0]), **fields)
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

    def append_frame(self, event_id, stamp, jpeg):
        if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= self.MAX_FRAME:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM safety_events WHERE id=?", (event_id,)).fetchone():
                return
            count = db.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM safety_frames WHERE event=?", (event_id,)
            ).fetchone()[0]
            if count < 22:
                db.execute("INSERT INTO safety_frames VALUES (?,?,?,?)", (event_id, count, stamp, jpeg))
            self._prune(db)

    def claim_output(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT expires FROM safety_output WHERE id=1").fetchone()
            if row and row[0] > time.time():
                return False
            db.execute("INSERT OR REPLACE INTO safety_output VALUES (1,?,?)", (owner, time.time() + 40))
        return True

    def release_output(self, owner):
        with self.connect() as db:
            db.execute("DELETE FROM safety_output WHERE owner=?", (owner,))

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

    def __init__(self):
        self.cameras = {}

    def clear(self, camera):
        self.cameras.pop(camera, None)

    @staticmethod
    def geometry(person, aspect):
        joints, box = person.get("joints", {}), person.get("box", {})
        pts = [joints.get(side + part) for part in ("Shoulder", "Hip") for side in ("left", "right")]
        if any(
            not p
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
        if state is None or state["session"] != session or not 0 < now - state["last"] <= 0.75:
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
            if old is None or not 0 < now - old["last"] <= 0.75:
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
            anchor = old["anchor"]
            if not geometry["horizontal"]:
                old["down_since"] = None
                continue
            if old["down_since"] is None:
                if (
                    not anchor
                    or now - anchor["at"] > 1.2
                    or geometry["center"] - anchor["center"] < anchor["height"] * 0.25
                ):
                    continue
                old["down_since"] = now
            if now - old["down_since"] >= 2 and not old["fired"]:
                old["fired"] = True
                identity = person.get("identity", {})
                pid = (
                    identity.get("person_id")
                    if (
                        identity.get("state") in {"matched", "tracked"}
                        and 0 <= evaluated_mono - identity.get("observed_monotonic", -100) <= 1
                        and 0 <= evaluated_mono - identity.get("face_observed_monotonic", -100) <= 5
                    )
                    else None
                )
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
                        started_mono=anchor["at"],
                    )
                )
        state["tracks"] = {k: v for k, v in state["tracks"].items() if k in present}
        return found


class SafetyRuntime:
    def __init__(self, store, *, labels=lambda: {}, speaker=None, visual_engine=None):
        self.store, self.labels, self.speaker = store, labels, speaker
        self.detector = FallDetector()
        self.frames = {}
        self.post_frames = {}
        self.tasks = set()
        self.last_seen = {}
        self.policy_revision = None
        self.policy_task = None
        self.policy_lock = asyncio.Lock()
        self.error = None
        from .temporal_review import TemporalReviewer
        self.visual = TemporalReviewer(policy=store.policy, deliver=self._visual_event, engine=visual_engine)

    async def sync_policy(self):
        async with self.policy_lock:
            policy = self.store.policy()
            if self.policy_revision == policy.revision:return
            self.policy_revision = policy.revision
            self.detector.cameras.clear()
            self.frames.clear()
            self.post_frames.clear()
            self.last_seen.clear()
            await self.visual.invalidate()

    async def start(self, *, interval=.5):
        if self.policy_task is not None:return
        async def sync_safely():
            try:await self.sync_policy()
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                self.error = 'safety_storage_unavailable'
                await self.visual.invalidate()
        await sync_safely()
        async def watch():
            while True:
                await sync_safely()
                await asyncio.sleep(interval)
        self.policy_task = asyncio.create_task(watch(), name='friday-safety-policy')

    def clear(self, camera):
        self.visual.clear(camera)
        self.detector.clear(camera)
        self.frames.pop(camera, None)
        self.post_frames.pop(camera, None)
        self.last_seen.pop(camera, None)

    async def observe(self, camera, session, people, now, captured, *, stable, jpeg=b"", aspect=16 / 9):
        try:
            await self._observe(
                camera, session, people, now, captured, stable=stable, jpeg=jpeg, aspect=aspect
            )
            self.error = None
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # A full disk or broken safety database must not stop perception.
            self.clear(camera)
            self.error = "safety_storage_unavailable"

    async def _observe(self, camera, session, people, now, captured, *, stable, jpeg, aspect):
        await self.sync_policy()
        policy = self.store.policy()
        if not policy.enabled or not stable or not 0 <= time.monotonic() - captured <= 1.5:
            self.clear(camera)
            return
        if camera in self.frames and self.frames[camera]["session"] != session:
            self.clear(camera)
        self.last_seen[camera] = time.monotonic()
        if policy.visual_review:
            self.visual.offer(camera, session, policy.revision, captured, jpeg, stable=stable)
        else:
            self.visual.clear(camera)
        ring = self.frames.setdefault(camera, dict(session=session, items=deque(maxlen=16)))
        stamp = time.time() - (time.monotonic() - captured)
        if (
            jpeg
            and len(jpeg) <= self.store.MAX_FRAME
            and (not ring["items"] or stamp - ring["items"][-1][0] >= 0.5)
        ):
            ring["items"].append((stamp, jpeg))
            pending = self.post_frames.get(camera, [])
            self.post_frames[camera] = [p for p in pending if captured <= p["until"]]
            for pending_event in self.post_frames[camera]:
                self.store.append_frame(pending_event["id"], stamp, jpeg)
        events = self.detector.observe(
            camera, session, people, now, captured, stable=stable, aspect=aspect,
            evaluated_mono=time.monotonic(),
        )
        for event in events:
            name = self.labels().get(event["person_id"])
            event["person_name"] = name
            event["message"] = (
                f"检测到 {name} 疑似跌倒，请立即查看。" if name else "有人疑似跌倒，请立即查看。"
            )
            saved = self.store.create(event, [f for f in ring["items"] if 0 <= time.time() - f[0] <= 8])
            self.post_frames.setdefault(camera, []).append(dict(id=saved["id"], until=captured + 3))
            if policy.announce:
                self.schedule(saved, revision=policy.revision)

    def _visual_event(self, camera, session, frames, result, captured_at):
        messages = {
            "possible_fall": "画面中疑似出现跌落动作，请查看关键帧。",
            "face_obscured": "画面中疑似出现面部遮挡，请查看关键帧。",
            "rapid_movement": "画面中疑似出现快速移动或搬动，请查看关键帧。",
        }
        # Model text remains private evidence. It cannot supply identity or speech.
        self.store.create(
            dict(
                kind="visual_review", visual_kind=result.event, severity="review",
                status="pending_review", simulated=False, camera=camera, session=session,
                track=None, person_id=None, person_name=None,
                message=messages[result.event], observation=result.observation,
                captured_at=captured_at, basis="two_disjoint_local_windows",
                detector="qwen3.5-2b-window/1",
            ),
            [(captured_at - (frames[-1][0] - stamp), jpeg) for stamp, jpeg in frames],
        )

    def schedule(self, event, revision=None):
        # Bound pending output independently of the perception frame rate.
        if self.tasks:
            self.store.update(event["id"], announcement={"status": "busy", "physical_audio_confirmed": False})
            return
        task = asyncio.create_task(self.announce(event, revision))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def announce(self, event, revision=None):
        status = "unavailable"
        claimed = False
        try:
            policy = self.store.policy()
            if self.store.event(event["id"]).get("review") == "false_alarm":
                status = "cancelled"
            elif revision is not None and (
                not policy.enabled or not policy.announce or revision != policy.revision
            ):
                status = "cancelled"
            elif not self.store.claim_output(event["id"]):
                status = "busy"
            else:
                claimed = True
                from .speech import Speaker

                speaker = self.speaker or Speaker()
                self.store.update(
                    event["id"], announcement={"status": "requesting", "physical_audio_confirmed": False}
                )
                receipt = await asyncio.wait_for(speaker.say(event["message"]), 35)
                status = receipt.get("status", "unavailable")
                if status not in {"accepted", "paused", "busy", "unavailable"}:
                    status = "unavailable"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception:
            status = "unavailable"
        finally:
            try:
                if claimed:
                    self.store.release_output(event["id"])
                self.store.update(
                    event["id"], announcement={"status": status, "physical_audio_confirmed": False}
                )
            except (OSError, sqlite3.Error, ValueError):
                self.error = "safety_receipt_unavailable"

    async def test(self):
        event = self.store.create(
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
        self.schedule(event)
        return event

    def status(self):
        for camera, seen in list(self.last_seen.items()):
            if not 0 <= time.monotonic() - seen <= 2:
                self.clear(camera)
        return dict(
            cameras=[
                dict(camera=camera, fresh=0 <= time.monotonic() - seen <= 2)
                for camera, seen in self.last_seen.items()
            ],
            pending_announcements=len(self.tasks),
            error=self.error,
            visual_review=self.visual.status(),
        )

    async def close(self):
        if self.policy_task is not None:
            self.policy_task.cancel()
            await asyncio.gather(self.policy_task, return_exceptions=True)
            self.policy_task = None
        await self.visual.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
