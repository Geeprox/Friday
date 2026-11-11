"""将符合时序条件的本机声音复核结果交给私有留档。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import math
import time
from collections import deque
from dataclasses import dataclass, field

from .camera_audio import MAX_AGE, PCM_BYTES, SAMPLE_RATE, AdmittedAudio
from .safety_recording import ReviewReceipt
from .sound_model import AppleSoundProcess

INFANT_CRY_MAX_GAP_SAMPLES = int(SAMPLE_RATE * .4)
INFANT_CRY_INTERVAL = 2.6
INFANT_CRY_DEADLINE = INFANT_CRY_INTERVAL + MAX_AGE
REVIEW_MAX_AGE = 6
SOUND_SEARCH_START_AGE = 8
SOUND_SEARCH_MAX_AGE = 20


@dataclass(frozen=True)
class SoundPacket:
    received_mono: float
    received_at: float
    sequence: int
    sample_start: int
    pcm: bytes


@dataclass(frozen=True)
class SoundEvidence:
    kind: str
    sample_end: int
    captured: float
    captured_at: float
    confidence: float
    infant_cry: bool
    keyframe: tuple[float, bytes] | None


@dataclass
class SoundWindow:
    session: str
    context: str
    capture: str
    epoch: int
    revision: int
    packets: deque[SoundPacket] = field(default_factory=lambda: deque(maxlen=10))
    seen: float = -math.inf
    submitted: float = -math.inf
    candidate: SoundEvidence | None = None
    followup: bool = False


@dataclass(frozen=True)
class QualifiedCry:
    source: SoundWindow
    evidence: tuple[SoundEvidence, SoundEvidence]


@dataclass(frozen=True, eq=False)
class SoundSearchCue:
    """已留档异响的来源和时效，不含音频、画面或方向。"""

    camera: str
    session: str
    context: str
    revision: int
    generation: int
    captured: float
    captured_at: float
    receipt: ReviewReceipt


class SoundReviewer:
    def __init__(self, *, policy, current, deliver, keyframe=None, engine=None, clock=time.monotonic):
        self.policy, self.current, self.deliver = policy, current, deliver
        self.keyframe = keyframe
        self.engine = engine or AppleSoundProcess()
        self.clock = clock
        self.windows = {}
        self.qualified_cries: dict[str, QualifiedCry] = {}
        self.served = {}
        self.recorded = {}
        self.search_cues = {}
        self.search_generation = 0
        self.task = self.active_camera = None
        self.active_refinement = None
        self.last_camera = None
        self.cleanup_task = None
        self.last_used = -math.inf
        self.retry_after = 0
        self.error = None
        self.completed = self.skipped = 0
        self.closed = False

    def readiness(self):
        return self.engine.readiness()

    def clear(self, camera, *, keep_qualified=False):
        self.search_cues.pop(camera, None)
        state = self.windows.pop(camera, None)
        if state is not None:
            state.packets.clear()
            state.candidate = None
            state.followup = False
        if not keep_qualified:
            self.qualified_cries.pop(camera, None)
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
                or admission.received_mono is None):
            self.clear(camera)
            return
        p = admission.packet
        state = self.windows.get(camera)
        qualified = self.qualified_cries.get(camera)
        previous_source = state or (qualified.source if qualified is not None else None)
        capture, epoch = p['capture_session'], p['audio_epoch']
        if previous_source and (session, context, capture, revision) != (
                previous_source.session, previous_source.context, previous_source.capture, previous_source.revision):
            self.clear(camera)
            state = None
        if not 0 <= now-admission.received_mono <= MAX_AGE or not self.readiness()['available']:
            self.clear(camera, keep_qualified=True)
            return
        if state and (admission.reset or state.epoch != epoch):
            self.clear(camera, keep_qualified=True)
            state = None
        if state is None:
            tracked = self.windows.keys() | self.qualified_cries.keys()
            if camera not in tracked and len(tracked) >= 8:
                self.skipped += 1
                return
            state = self.windows[camera] = SoundWindow(session, context, capture, epoch, revision,
                                                        submitted=self.served.get(camera, -math.inf))
        if (state.packets and (p['audio_sequence'] != state.packets[-1].sequence+1
                or p['sample_start'] != state.packets[-1].sample_start+PCM_BYTES//2)):
            self.clear(camera, keep_qualified=True)
            return
        state.seen = admission.received_mono
        state.packets.append(SoundPacket(admission.received_mono, p['received_at'], p['audio_sequence'],
                                         p['sample_start'], base64.b64decode(p['pcm'], validate=True)))
        if (len(state.packets) < 10 or now-state.submitted < 2 or now < self.retry_after
                or self.task or self.cleanup_task and not self.cleanup_task.done()):
            return
        # 每个阳性候选可优先取得一次复核。连续两段婴儿哭声可再取一段，随后让给其他镜头。
        eligible = [(s.submitted, index, cid) for index, (cid, s) in enumerate(self.windows.items())
                    if len(s.packets) == 10 and 0 <= now-s.seen <= MAX_AGE and now-s.submitted >= 2]
        priority = [item for item in eligible if self._followup_ready(item[2], self.windows[item[2]])]
        if eligible and min(priority or eligible)[2] != camera:
            return
        followup = self._followup_ready(camera, state)
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
        self.active_refinement = qualified if followup and qualified and qualified.source is state else None
        self.task = asyncio.create_task(self._review(camera, state, packets, allow_followup=not followup))
        self.task.add_done_callback(self._finished)

    def _followup_ready(self, camera, state):
        previous = state.candidate
        qualified = self.qualified_cries.get(camera)
        max_interval = INFANT_CRY_INTERVAL if qualified and qualified.source is state else REVIEW_MAX_AGE
        return bool(state.followup and previous and state.packets
                    and state.packets[0].sample_start >= previous.sample_end
                    and 0 < state.packets[-1].received_mono - previous.captured <= max_interval)

    def stability_pending(self, camera, session, revision):
        """Retain the current view for analysis or a live corroborating window.

        The camera owner supplies its existing finite hold and motion guards.
        This hint neither locates a sound nor requests a camera direction.
        """
        qualified = self.qualified_cries.get(camera)
        if qualified is not None:
            source = qualified.source
            if (source.session == session and source.revision == revision
                    and 0 <= self.clock()-qualified.evidence[-1].captured <= REVIEW_MAX_AGE
                    and self._source_current(camera, source)):
                return True
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
            self.active_refinement = None

    def _source_current(self, camera, state):
        p = self.policy()
        return (not self.closed and p.enabled and p.sound_review and p.revision == state.revision
                and self.current(camera, state.session, state.context))

    def search_cue(self, camera, session, active=None):
        """转向会清空采样。已开始的环视仅在原来源恢复后继续复查同一回执。"""
        cue = active if active is not None else self.search_cues.get(camera)
        if cue is None or cue.camera != camera or cue.session != session or cue.generation != self.search_generation:
            return None
        now = self.clock()
        max_age = SOUND_SEARCH_MAX_AGE if active is not None else SOUND_SEARCH_START_AGE
        if (cue.receipt.pending or cue.receipt.saved_at is None or not 0 <= now-cue.receipt.saved_at
                or not 0 <= now-cue.captured <= max_age or not self._source_current(camera, cue)):
            return None
        return cue

    def _current(self, camera, state, packets):
        now = self.clock()
        return (self.windows.get(camera) is state and 0 <= now-state.seen <= MAX_AGE
                and 0 <= now-packets[-1].received_mono <= REVIEW_MAX_AGE and self._source_current(camera, state))

    async def _deliver(self, camera, state, evidence):
        latest = evidence[-1]
        key = (camera, latest.kind)
        receipt = self.recorded.get(key)
        now = self.clock()
        if (not 0 <= now-latest.captured <= REVIEW_MAX_AGE or not self._source_current(camera, state)
                or receipt is not None and receipt.suppresses(now)):
            return
        frames = [window.keyframe for window in evidence if window.keyframe is not None]
        result = dict(kind=latest.kind, confidence=min(window.confidence for window in evidence),
                      captured_mono=latest.captured, captured_at=latest.captured_at,
                      corroborating_windows=len(evidence), audio_seconds=2*len(evidence),
                      age_basis='local_sdk_receive', policy_revision=state.revision,
                      keyframes=[(latest.captured_at + (stamp-latest.captured), jpeg) for stamp, jpeg in frames])
        if len(evidence) == 3:
            result['pattern'] = 'sustained_infant_cry'
        search_generation = self.search_generation
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
        if (isinstance(delivered, ReviewReceipt) and self.policy().sound_search
                and search_generation == self.search_generation
                and self._source_current(camera, state)):
            self.search_cues.pop(camera, None)
            self.search_cues[camera] = SoundSearchCue(camera, state.session, state.context, state.revision,
                                                     self.search_generation, latest.captured, latest.captured_at, delivered)
            if len(self.search_cues) > 8:
                self.search_cues.pop(next(iter(self.search_cues)))

    async def _review(self, camera, state, packets, *, allow_followup=True):
        try:
            if not self._current(camera, state, packets):
                return
            first, last = packets[0], packets[-1]
            keyframe = (self.keyframe(camera, state.session, state.context, first.received_mono, last.received_mono)
                        if self.keyframe is not None else None)
            result = await self.engine.analyze(b''.join(p.pcm for p in packets))
            self.last_used = self.clock()
            if not self._current(camera, state, packets):
                return
            self.completed += 1
            self.error = None
            previous = state.candidate
            candidate = None
            corroborates = False
            continuous_infant = False
            if result is not None:
                candidate = SoundEvidence(result['kind'], last.sample_start+PCM_BYTES//2, last.received_mono,
                                          last.received_at, result['confidence'],
                                          result['kind'] == 'crying' and result.get('infant_cry') is True, keyframe)
                if previous and previous.kind == candidate.kind:
                    gap = first.sample_start - previous.sample_end
                    interval = candidate.captured - previous.captured
                    corroborates = gap >= 0 and 0 < interval <= REVIEW_MAX_AGE
                    continuous_infant = (corroborates and previous.infant_cry and candidate.infant_cry
                                         and gap <= INFANT_CRY_MAX_GAP_SAMPLES and interval <= INFANT_CRY_INTERVAL)
            qualified = self.qualified_cries.pop(camera, None)
            state.candidate = candidate
            if qualified is not None:
                same_pair = qualified.source is state and qualified.evidence[-1] is previous
                if (same_pair and continuous_infant
                        and self.clock()-previous.captured <= INFANT_CRY_DEADLINE):
                    await self._deliver(camera, state, (*qualified.evidence, candidate))
                    return
                await self._deliver(camera, qualified.source, qualified.evidence)
                if not self._current(camera, state, packets):
                    return
                # 已留档的两段证据不能和迟到的第三段再次印证。
                if same_pair:
                    corroborates = continuous_infant = False
            if candidate is None:
                return
            receipt = self.recorded.get((camera, candidate.kind))
            if receipt is not None and receipt.suppresses(self.clock()):
                return
            if not corroborates:
                state.followup = allow_followup
            elif continuous_infant:
                self.qualified_cries[camera] = QualifiedCry(state, (previous, candidate))
                state.followup = True
            else:
                await self._deliver(camera, state, (previous, candidate))
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
            if not self._source_current(camera, state):
                self.clear(camera)
            elif not 0 <= self.clock()-state.seen <= MAX_AGE:
                self.clear(camera, keep_qualified=True)
        for camera, qualified in list(self.qualified_cries.items()):
            if self.qualified_cries.get(camera) is not qualified:
                continue
            state = qualified.source
            if not self._source_current(camera, state):
                self.qualified_cries.pop(camera)
                continue
            attached = self.windows.get(camera) is state
            if not attached or self.clock()-qualified.evidence[-1].captured > INFANT_CRY_DEADLINE:
                self.qualified_cries.pop(camera)
                if attached and self.active_refinement is qualified:
                    # 淘汰这次额外复核，保留其他分析任务。
                    self.clear(camera)
                elif attached:
                    state.candidate = None
                    state.followup = False
                await self._deliver(camera, state, qualified.evidence)
        if (not self.task and not (self.cleanup_task and not self.cleanup_task.done())
                and self.clock()-self.last_used > 30):
            await self.engine.close()

    async def invalidate(self):
        self.search_generation += 1
        self.search_cues.clear()
        self.windows.clear()
        self.qualified_cries.clear()
        self.served.clear()
        if self.task:
            task = self.task
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.task = self.active_camera = None
            self.active_refinement = None
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
