"""Qualified camera PCM -> two disjoint local windows -> private review item."""

from __future__ import annotations

import asyncio
import base64
import inspect
import math
import time
from collections import deque
from dataclasses import dataclass, field

from .camera_audio import AdmittedAudio
from .safety_recording import ReviewReceipt
from .sound_model import AppleSoundProcess


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
    candidate: tuple | None = None
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
        # One positive window gets one timely, disjoint follow-up. A pure
        # round-robin can expire every candidate when several feeds share a
        # slower serial model. Further work yields to the oldest other feed.
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
                    and state.packets[0][2] >= previous[1]
                    and 0 < state.packets[-1][0]-previous[2] <= 6)

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
        kind, _, captured, _ = state.candidate
        receipt = self.recorded.get((camera, kind))
        if (not 0 <= self.clock()-captured <= 12
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
            if result is None:
                state.candidate = None
                return
            previous = state.candidate
            state.candidate = (result['kind'], packets[-1][2]+3200, packets[-1][0], result['confidence'])
            key = (camera, result['kind'])
            receipt = self.recorded.get(key)
            if receipt is not None and receipt.suppresses(self.clock()):
                return
            if (not previous or previous[0] != result['kind'] or packets[0][2] < previous[1]
                    or not 0 < packets[-1][0]-previous[2] <= 6):
                state.followup = allow_followup
                return
            delivered = self.deliver(camera, state.session, state.context, dict(
                kind=result['kind'], confidence=min(previous[3], result['confidence']),
                captured_mono=packets[-1][0],
                captured_at=self.wall()-(self.clock()-packets[-1][0]), corroborating_windows=2,
                audio_seconds=4, age_basis='local_sdk_receive',policy_revision=state.revision))
            if inspect.isawaitable(delivered):
                delivered = await delivered
            if delivered is False:
                return
            self.recorded.pop(key, None)
            self.recorded[key] = (delivered if isinstance(delivered, ReviewReceipt)
                                  else ReviewReceipt(pending=False, saved_at=self.clock()))
            if len(self.recorded) > 32:
                self.recorded.pop(next(iter(self.recorded)))
        except asyncio.CancelledError:
            await self.engine.close()
            raise
        except Exception as error:
            state.candidate = None
            await self.engine.close()
            self.retry_after = self.clock()+15
            self.error = str(error) if str(error) in {
                'sound_runtime_missing', 'macos_required', 'sound_model_busy', 'sound_labels_unavailable',
                'sound_memory_pressure', 'sound_cpu_busy',
            } else 'sound_review_deferred'

    async def maintain(self):
        for camera, state in list(self.windows.items()):
            if not 0 <= self.clock()-state.seen <= 1.5:
                self.clear(camera)
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
