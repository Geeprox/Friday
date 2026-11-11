"""将符合时序条件的本机声音复核结果交给私有留档。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import math
import time
from collections import deque
from dataclasses import dataclass, field

from .camera_audio import MAX_AGE, SAMPLE_RATE, AdmittedAudio
from .safety_recording import ReviewReceipt
from .sound_model import AppleSoundProcess

INFANT_CRY_WINDOWS = 3
INFANT_CRY_MAX_GAP_SAMPLES = int(SAMPLE_RATE * .4)
INFANT_CRY_INTERVAL = 2.6


@dataclass
class SoundEvidence:
    kind: str
    sample_end: int
    captured: float
    confidence: float
    infant_windows: int = 0


@dataclass
class SoundWindow:
    session: str
    context: str
    capture: str
    epoch: int
    revision: int
    packets: deque = field(default_factory=lambda: deque(maxlen=10))
    seen: float = -math.inf
    submitted: float = -math.inf
    candidate: SoundEvidence | None = None
    pending_cry: SoundEvidence | None = None
    followup: bool = False


class SoundReviewer:
    def __init__(self, *, policy, current, deliver, engine=None, clock=time.monotonic, wall=time.time):
        self.policy, self.current, self.deliver = policy, current, deliver
        self.engine = engine or AppleSoundProcess()
        self.clock, self.wall = clock, wall
        self.windows = {}
        self.served = {}
        self.recorded = {}
        self.task = self.active_camera = None
        self.last_camera = None
        self.cleanup_task = None
        self.last_used = -math.inf
        self.retry_after = 0
        self.error = None
        self.completed = self.skipped = 0
        self.closed = False

    def readiness(self):
        return self.engine.readiness()

    def clear(self, camera):
        self.windows.pop(camera, None)
        if self.task and self.active_camera == camera:
            if not self.task.cancelling():
                self.task.cancel()
        elif not self.task and self.last_camera == camera and (not self.cleanup_task or self.cleanup_task.done()):
            self.cleanup_task = asyncio.create_task(self.engine.close())
        if self.last_camera == camera:
            self.last_camera = None

    def offer(self, camera, session, context, revision, admission):
        now = self.clock()
        if (self.closed or not isinstance(admission, AdmittedAudio) or admission.packet is None
                or admission.received_mono is None or not 0 <= now-admission.received_mono <= 1.5):
            self.clear(camera)
            return
        p = admission.packet
        state = self.windows.get(camera)
        identity = (session, context, p['capture_session'], p['audio_epoch'], revision)
        if state and (admission.reset or (state.session, state.context, state.capture, state.epoch, state.revision) != identity):
            self.clear(camera)
            state = None
        if state is None:
            if len(self.windows) >= 8:
                self.skipped += 1
                return
            state = self.windows[camera] = SoundWindow(*identity, submitted=self.served.get(camera, -math.inf))
        if (state.packets and (p['audio_sequence'] != state.packets[-1][1]+1
                or p['sample_start'] != state.packets[-1][2]+3200)):
            self.clear(camera)
            return
        state.seen = admission.received_mono
        state.packets.append((admission.received_mono, p['audio_sequence'], p['sample_start'],
                              base64.b64decode(p['pcm'], validate=True)))
        if (len(state.packets) < 10 or now-state.submitted < 2 or now < self.retry_after
                or self.task or self.cleanup_task and not self.cleanup_task.done()):
            return
        # 每个阳性候选可优先取得一次复核。连续两段婴儿哭声可再取一段，随后让给其他镜头。
        eligible = [(s.submitted, index, cid) for index, (cid, s) in enumerate(self.windows.items())
                    if len(s.packets) == 10 and 0 <= now-s.seen <= 1.5 and now-s.submitted >= 2]
        priority = [item for item in eligible if self._followup_ready(self.windows[item[2]])]
        if eligible and min(priority or eligible)[2] != camera:
            return
        followup = self._followup_ready(state)
        state.followup = False
        state.submitted = now
        # Retiring a view discards its PCM and candidate, but must not let the
        # same camera repeatedly jump ahead of feeds still awaiting analysis.
        self.served[camera] = now
        if len(self.served) > 8:
            self.served = dict(sorted(self.served.items(), key=lambda item: item[1])[-8:])
        packets = tuple(state.packets)
        self.active_camera = camera
        self.last_camera = camera
        self.task = asyncio.create_task(self._review(camera, state, packets, allow_followup=not followup))
        self.task.add_done_callback(self._finished)

    @staticmethod
    def _followup_ready(state):
        previous = state.candidate
        return bool(state.followup and previous and state.packets
                    and state.packets[0][2] >= previous.sample_end
                    and 0 < state.packets[-1][0] - previous.captured <= (
                        INFANT_CRY_INTERVAL if state.pending_cry is not None else 6))

    def stability_pending(self, camera, session, revision):
        """Retain the current view for analysis or a live corroborating window.

        The camera owner supplies its existing finite hold and motion guards.
        This hint neither locates a sound nor requests a camera direction.
        """
        state = self.windows.get(camera)
        if (state is None or state.session != session or state.revision != revision
                or len(state.packets) != 10
                or not self._current(camera, state, tuple(state.packets))):
            return False
        active = (self.active_camera == camera and self.task is not None
                  and not self.task.done() and not self.task.cancelling())
        if state.candidate is None:
            return active
        candidate = state.candidate
        receipt = self.recorded.get((camera, candidate.kind))
        if (not 0 <= self.clock()-candidate.captured <= 12
                or receipt is not None and receipt.suppresses(self.clock())):
            return False
        return bool(state.followup or active)

    def _finished(self, task):
        if self.task is task:
            self.task = self.active_camera = None

    def _current(self, camera, state, packets):
        p = self.policy()
        return (not self.closed and self.windows.get(camera) is state and p.enabled and p.sound_review
                and p.revision == state.revision and 0 <= self.clock()-state.seen <= 1.5
                and 0 <= self.clock()-packets[-1][0] <= 6
                and self.current(camera, state.session, state.context))

    async def _deliver(self, camera, state, evidence, windows):
        key = (camera, evidence.kind)
        receipt = self.recorded.get(key)
        now = self.clock()
        age = now - evidence.captured
        if not 0 <= age <= 6 or receipt is not None and receipt.suppresses(now):
            return
        result = dict(kind=evidence.kind, confidence=evidence.confidence, captured_mono=evidence.captured,
                      captured_at=self.wall()-age, corroborating_windows=windows, audio_seconds=2*windows,
                      age_basis='local_sdk_receive', policy_revision=state.revision)
        if windows == INFANT_CRY_WINDOWS:
            result['pattern'] = 'sustained_infant_cry'
        delivered = self.deliver(camera, state.session, state.context, result)
        if inspect.isawaitable(delivered):
            delivered = await delivered
        if delivered is False:
            return
        self.recorded.pop(key, None)
        self.recorded[key] = (delivered if isinstance(delivered, ReviewReceipt)
                              else ReviewReceipt(pending=False, saved_at=self.clock()))
        if len(self.recorded) > 32:
            self.recorded.pop(next(iter(self.recorded)))

    async def _review(self, camera, state, packets, *, allow_followup=True):
        try:
            if not self._current(camera, state, packets):
                return
            result = await self.engine.analyze(b''.join(p[3] for p in packets))
            self.last_used = self.clock()
            if not self._current(camera, state, packets):
                return
            self.completed += 1
            self.error = None
            previous = state.candidate
            candidate = None
            corroborates = False
            if result is not None:
                infant = result['kind'] == 'crying' and result.get('infant_cry') is True
                candidate = SoundEvidence(result['kind'], packets[-1][2]+3200, packets[-1][0],
                                          result['confidence'], int(infant))
                if previous and previous.kind == candidate.kind:
                    gap = packets[0][2] - previous.sample_end
                    interval = candidate.captured - previous.captured
                    corroborates = gap >= 0 and 0 < interval <= 6
                    if (corroborates and infant and previous.infant_windows
                            and gap <= INFANT_CRY_MAX_GAP_SAMPLES and interval <= INFANT_CRY_INTERVAL):
                        candidate.infant_windows = min(INFANT_CRY_WINDOWS, previous.infant_windows+1)
                        candidate.confidence = min(previous.confidence, candidate.confidence)
            sustained = candidate is not None and candidate.infant_windows == INFANT_CRY_WINDOWS
            pending, state.pending_cry = state.pending_cry, None
            if pending is not None and not sustained:
                await self._deliver(camera, state, pending, 2)
                if not self._current(camera, state, packets):
                    return
            state.candidate = candidate
            if candidate is None:
                return
            receipt = self.recorded.get((camera, candidate.kind))
            if receipt is not None and receipt.suppresses(self.clock()):
                return
            if not corroborates:
                state.followup = allow_followup
            elif candidate.infant_windows == 2:
                state.pending_cry = candidate
                state.followup = True
            else:
                evidence = SoundEvidence(candidate.kind, candidate.sample_end, candidate.captured,
                                         min(previous.confidence, candidate.confidence))
                await self._deliver(camera, state, evidence, INFANT_CRY_WINDOWS if sustained else 2)
        except asyncio.CancelledError:
            await self.engine.close()
            raise
        except Exception as error:
            state.candidate = None
            state.followup = False
            await self.engine.close()
            self.retry_after = self.clock()+15
            self.error = str(error) if str(error) in {
                'sound_runtime_missing', 'macos_required', 'sound_model_busy', 'sound_labels_unavailable',
                'sound_memory_pressure', 'sound_cpu_busy',
            } else 'sound_review_deferred'

    async def maintain(self):
        for camera, state in list(self.windows.items()):
            now = self.clock()
            if not 0 <= now-state.seen <= MAX_AGE:
                self.clear(camera)
                continue
            pending = state.pending_cry
            active = self.active_camera == camera and self.task is not None and not self.task.done()
            if pending is not None and not active and now-pending.captured > INFANT_CRY_INTERVAL + MAX_AGE:
                state.pending_cry = None
                state.candidate = None
                state.followup = False
                if self._current(camera, state, tuple(state.packets)):
                    await self._deliver(camera, state, pending, 2)
        if not self.task and self.clock()-self.last_used > 30:
            await self.engine.close()

    async def invalidate(self):
        self.windows.clear()
        self.served.clear()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = self.active_camera = None
        if self.cleanup_task:
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
            self.cleanup_task = None
        await self.engine.close()
        self.last_camera = None
        self.error = None
        self.retry_after = 0

    def status(self):
        return dict(self.readiness(), state='reviewing' if self.task else 'deferred' if self.error else 'idle',
                    completed_windows=self.completed, skipped_windows=self.skipped, error=self.error,
                    camera_count=len(self.windows), parallel_requests=1, window_seconds=2,
                    raw_audio_stored=False, thresholds_calibrated=False)

    async def close(self):
        self.closed = True
        await self.invalidate()
