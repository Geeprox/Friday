"""Bounded, source-owned recording of already qualified incidents.

This owner retries private persistence, never perception or device actions. A
stable incident ID reconciles uncertain commits; source/policy admission is still
checked by the original store inside its write transaction.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field


@dataclass(eq=False)
class ReviewReceipt:
    pending: bool = True
    saved_at: float | None = None
    event_id: str | None = None

    def suppresses(self, now):
        return self.pending or self.saved_at is not None and 0 <= now - self.saved_at < 60


@dataclass(eq=False)
class IncidentRecording:
    camera: str
    session: str
    context: str | None
    token: object
    captured: float
    revision: int
    event: dict
    initial: tuple
    person: dict | None = None
    announce: bool = False
    feature: str | None = None
    receipt: ReviewReceipt | None = None
    identifier: str = field(default_factory=lambda: secrets.token_hex(16))
    following: list = field(default_factory=list)
    appended: int = 0
    saved: dict | None = None
    output_considered: bool = False
    attempts: int = 0
    failures: int = 0
    retry_at: float = -float('inf')
    failed: bool = False
    expired: bool = False


class SafetyRecorder:
    MAX_PENDING = 32
    MAX_BYTES = 64 * 1024 * 1024
    MAX_REVIEW_PENDING = 8
    MAX_REVIEW_BYTES = 8 * 1024 * 1024
    RETRY_WINDOW = 20
    POST_WINDOW = 3

    def __init__(self, *, persist, reconcile, current, clock=time.monotonic):
        self.persist, self.reconcile, self.current, self.clock = persist, reconcile, current, clock
        self.jobs = {}
        self.task = None
        self.active = None
        self.changed = asyncio.Event()
        self.closed = False
        self.completed = self.retried = self.dropped = self.partial = self.uncertain = self.skipped_frames = 0
        self.bytes = 0

    @staticmethod
    def size(job):
        return sum(len(jpeg) for _, jpeg in (*job.initial, *job.following))

    def _drop(self, job):
        if self.jobs.pop(job.identifier, None) is job:
            if job.receipt is not None:
                job.receipt.pending = False
            if self.active is not job:
                self.bytes -= self.size(job)

    def recorded(self, job):
        self.completed += 1
        if job.receipt is not None:
            job.receipt.saved_at = self.clock()
            job.receipt.event_id = job.identifier
            job.receipt.pending = False

    def has_camera(self, camera):
        return any(job.camera == camera for job in self.jobs.values())

    def pending_review(self, camera, session, revision):
        return any(job.camera == camera and job.session == session and job.revision == revision
                   and job.receipt is not None and job.receipt.pending and not job.expired
                   and self.current(job) for job in self.jobs.values())

    def pending_fall(self, camera, session, revision):
        now = self.clock()
        return any(job.camera == camera and job.session == session and job.revision == revision
                   and job.feature is None and not job.expired and self.current(job)
                   and (now <= job.captured + self.POST_WINDOW or job.saved is None
                        or job.appended < len(job.following)) for job in self.jobs.values())

    def maintain(self):
        now = self.clock()
        for job in tuple(self.jobs.values()):
            if now - job.captured > self.RETRY_WINDOW:
                if job.saved is None or job.appended < len(job.following):
                    job.expired = True
                else:
                    self._drop(job)
            elif not self.current(job):
                self._drop(job)
            elif job.saved is not None and job.appended == len(job.following) and now > job.captured + self.POST_WINDOW:
                self._drop(job)

    def submit(self, *, camera, session, context, token, captured, revision, event, frames,
               person=None, announce=False, feature=None, require_complete=False):
        self.maintain()
        owned = list(self.jobs.values())
        if self.active is not None and self.active.identifier not in self.jobs:
            owned.append(self.active)
        if self.closed or len(owned) >= self.MAX_PENDING:
            self.dropped += 1
            return None
        available = self.MAX_BYTES - self.bytes
        if feature is not None:
            # 限制待复核事件占用，把大部分队列和画面容量留给疑似跌倒。
            reviews = [job for job in owned if job.feature is not None]
            if len(reviews) >= self.MAX_REVIEW_PENDING:
                self.dropped += 1
                return None
            available = min(available, self.MAX_REVIEW_BYTES - sum(self.size(job) for job in reviews))
        frames = tuple(frames)[-16:]
        if require_complete and sum(len(jpeg) for _, jpeg in frames) > available:
            self.dropped += 1
            return None
        chosen = []
        for stamp, jpeg in reversed(frames):
            if len(jpeg) <= available:
                chosen.append((stamp, jpeg))
                available -= len(jpeg)
            else:
                self.skipped_frames += 1
        job = IncidentRecording(camera, session, context, token, captured, revision,
                                dict(event), tuple(reversed(chosen)), person=person, announce=announce,
                                feature=feature, receipt=ReviewReceipt() if feature is not None else None)
        self.jobs[job.identifier] = job
        self.bytes += self.size(job)
        self.wake()
        return job

    def frame(self, camera, captured, stamp, jpeg):
        self.maintain()
        for job in self.jobs.values():
            if (job.feature is not None or job.camera != camera
                    or not job.captured < captured <= job.captured + self.POST_WINDOW
                    or len(job.following) >= 6):
                continue
            if job.following and stamp <= job.following[-1][0]:
                continue
            if self.bytes + len(jpeg) > self.MAX_BYTES:
                self.skipped_frames += 1
                continue
            job.following.append((stamp, jpeg))
            self.bytes += len(jpeg)
        self.wake()

    def clear(self, camera=None, *, feature=None):
        for job in tuple(self.jobs.values()):
            if (camera is None or job.camera == camera) and (feature is None or job.feature == feature):
                self._drop(job)
        self.changed.set()

    def wake(self):
        self.changed.set()
        work = any(job.saved is None or job.appended < len(job.following) or job.expired
                   for job in self.jobs.values())
        if not self.closed and self.task is None and work:
            self.task = asyncio.create_task(self._run(), name='friday-safety-recordings')

    async def _finish_expired(self, job):
        try:
            receipt = await self.reconcile(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.jobs.get(job.identifier) is job:
                self.uncertain += 1
                self._drop(job)
            return
        if self.jobs.get(job.identifier) is not job:
            return
        if receipt is None:
            if job.saved is None:
                self.dropped += 1
        else:
            if job.saved is None:
                self.recorded(job)
            if any(stamp not in receipt['stamps'] for stamp, _ in job.following):
                self.partial += 1
        self._drop(job)

    async def _run(self):
        from .safety import SafetyAdmissionChanged
        try:
            while not self.closed:
                self.changed.clear()
                self.maintain()
                # IDs, not payload snapshots: a removed job must not remain
                # retained by a batch list while another DB call is blocked.
                pending = [key for key, job in self.jobs.items()
                           if job.saved is None or job.appended < len(job.following) or job.expired]
                if not pending:
                    return
                ready = [key for key in pending if self.jobs[key].expired or self.clock() >= self.jobs[key].retry_at]
                if not ready:
                    try:
                        await asyncio.wait_for(self.changed.wait(), max(.01, min(2, min(self.jobs[key].retry_at for key in pending)-self.clock())))
                    except TimeoutError:
                        pass
                    continue
                # 每次写入后重新选取，新增的疑似跌倒无需排在一批复核写入之后。
                key = min(ready, key=lambda key: self.jobs[key].feature is not None)
                job = self.active = self.jobs[key]
                try:
                    if job.expired:
                        await self._finish_expired(job)
                        continue
                    if not self.current(job):
                        self._drop(job)
                        continue
                    job.attempts += 1
                    if job.failed:
                        self.retried += 1
                    try:
                        await self.persist(job)
                        job.failed = False
                        job.failures = 0
                        if job.feature is not None and job.saved is not None:
                            self._drop(job)
                    except asyncio.CancelledError:
                        raise
                    except SafetyAdmissionChanged:
                        self._drop(job)
                    except Exception:
                        job.failed = True
                        job.failures += 1
                        job.retry_at = self.clock() + min(2, .25 * 2 ** min(job.failures-1, 3))
                finally:
                    if self.jobs.get(key) is not job:
                        self.bytes -= self.size(job)
                    self.active = None
                    job = None
        finally:
            if self.task is asyncio.current_task():
                self.task = None

    async def drain(self):
        while self.task is not None:
            await asyncio.shield(self.task)

    def status(self):
        self.maintain()
        error = ('safety_recording_lost' if self.dropped or self.partial else
                 'safety_recording_unconfirmed' if self.uncertain else
                 'safety_storage_unavailable' if any(job.failed for job in self.jobs.values()) else None)
        return dict(pending=sum(job.saved is None for job in self.jobs.values()),
                    retained_bytes=self.bytes, completed=self.completed, retried=self.retried,
                    dropped=self.dropped, partial=self.partial, unconfirmed=self.uncertain,
                    in_flight=int(self.active is not None), skipped_frames=self.skipped_frames,
                    error=error)

    async def close(self):
        self.closed = True
        self.changed.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for job in tuple(self.jobs.values()):
            self._drop(job)
