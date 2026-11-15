"""Fresh, same-session windows create private review items, never speech jobs."""

from __future__ import annotations

import asyncio
import inspect
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from .decisions_review import API_ERRORS
from .rapid_review import MAX_CANDIDATE_AGE, RapidReviewWindow
from .review_health import review_progress
from .safety_recording import ReviewReceipt
from .temporal_model import WINDOW_SPAN, MlxWindowProcess, VisualResult, VisualSourceChanged, model_readiness

MAX_CORROBORATION_SECONDS = 60
POSTURE_CORROBORATION_SECONDS = 18
CLOUD_CUE_GAP = 5


@dataclass(slots=True)
class CloudCue:
    last_seen: float
    pending: bool = True

# 仅按平台选择离线后端；导入不加载模型，也不增加窗口结果的时效。
if sys.platform.startswith('linux'):
    from .cpu_review import CpuWindowProcess as WindowProcess, readiness as review_readiness
else:
    WindowProcess, review_readiness = MlxWindowProcess, model_readiness


@dataclass
class WindowState:
    session: str
    revision: int
    source_context: str | None = None
    frames: deque = field(default_factory=lambda: deque(maxlen=4))
    regions: deque = field(default_factory=lambda: deque(maxlen=48))
    seen: float = 0
    opened_at: float = 0
    last_result_at: float | None = None
    submitted: float = -math.inf
    candidate: tuple | None = None
    followup: bool = False
    recorded: dict = field(default_factory=dict)
    posture_track: tuple[int, int] | None = None
    posture_since: float = math.inf
    rapid: RapidReviewWindow = field(default_factory=RapidReviewWindow)
    cloud_cues: dict[tuple[int, int], CloudCue] = field(default_factory=dict)
    cloud_activity_revision: int = 0

    def posture_subject(self, frames):
        if self.posture_track is not None and frames and frames[0][0] >= self.posture_since:
            return self.posture_track, self.posture_since
        return None

    def observe_cloud_cues(self, tracks, captured):
        # 骨骼短暂丢失或姿态轻微抖动时，沿用原来的复核记录。
        next_activity = False
        for track, cue in list(self.cloud_cues.items()):
            if captured - cue.last_seen > CLOUD_CUE_GAP:
                del self.cloud_cues[track]
        for track in tracks:
            if track in self.cloud_cues:
                self.cloud_cues[track].last_seen = captured
            else:
                self.cloud_cues[track] = CloudCue(captured)
                next_activity = True
        if next_activity:
            self.cloud_activity_revision += 1
        if next_activity:
            self.candidate, self.followup = None, False

    def pending_cloud_cues(self, since):
        return [cue for cue in self.cloud_cues.values() if cue.pending and cue.last_seen >= since]


class TemporalReviewer:
    def __init__(self, *, policy, deliver, engine=None, cloud=None, cloud_current=None,
                 clock=time.monotonic, wall=time.time):
        self.policy, self.deliver = policy, deliver
        self.engine = engine or WindowProcess()
        self.cloud, self.cloud_current = cloud, cloud_current
        self.clock, self.wall = clock, wall
        self.windows = {}
        self.served = {}
        self.task = None
        self.active_camera = None
        self.active_rapid = ()
        self.last_rapid = False
        self.phase = None
        self.last_camera = None
        self.cleanup_task = None
        self.error = None
        self.completed = self.skipped = 0
        self.last_submit = -math.inf
        self.retry_after = 0
        self.failures = 0
        self.closed = False

    def clear(self, camera):
        existed = self.windows.pop(camera, None)
        owns_recent = existed is not None and self.last_camera == camera
        if self.task:
            if self.active_camera == camera and not self.task.cancelling():
                self.task.cancel()
        elif owns_recent and (not self.cleanup_task or self.cleanup_task.done()):
            self.cleanup_task = asyncio.create_task(self.engine.close())
        if self.last_camera == camera:
            self.last_camera = None

    def offer(self, camera, session, revision, captured, jpeg, *, stable, posture_track=None,
              rapid_people=None, aspect=16/9, source_context=None, regions=(), visual_cues=()):
        now = self.clock()
        if self.closed or not stable or not math.isfinite(captured) or not 0 <= now - captured <= 1.5:
            self.clear(camera)
            return
        if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= 512 * 1024:
            self.clear(camera)
            return
        cloud_source = getattr(self.policy(), 'visual_cloud_source', None)
        if cloud_source is not None and (cloud_source.camera, cloud_source.source_context) != (camera, source_context):
            self.clear(camera)
            return
        state = self.windows.get(camera)
        if state and (
            state.session != session
            or state.revision != revision
            or state.source_context != source_context
            or captured <= state.seen
            or captured - state.seen > 1.75
        ):
            self.clear(camera)
            state = None
        if cloud_source is not None and not regions and state and state.frames:
            # 遮挡立即撤销帧和复核，但短暂遮挡不能让同一姿态重新上传。
            cues, submitted = state.cloud_cues, state.submitted
            self.clear(camera)
            state = self.windows[camera] = WindowState(
                session, revision, source_context, opened_at=captured, submitted=submitted, cloud_cues=cues)
        if state is None:
            if len(self.windows) >= 8:
                self.skipped += 1
                return
            # View changes retire every pixel/candidate, but must not erase
            # scheduling debt and repeatedly put this camera before its peers.
            state = self.windows[camera] = WindowState(
                session, revision, source_context, opened_at=captured, submitted=self.served.get(camera, -math.inf))
        state.seen = captured
        if cloud_source is not None:
            state.observe_cloud_cues(visual_cues, captured)
            if not regions:
                return
            state.regions.append((captured, regions))
            if state.candidate and captured - state.candidate[1] > self._corroboration_limit(state.candidate[0]):
                state.candidate, state.followup = None, False
        if posture_track != state.posture_track:
            state.posture_track, state.posture_since = posture_track, captured
            if state.candidate and state.candidate[0] == 'prone_posture':
                state.candidate = None
                state.followup = False
        if not state.frames or captured - state.frames[-1][0] >= 1:
            state.frames.append((captured, jpeg))
            while captured - state.frames[0][0] > WINDOW_SPAN:
                state.frames.popleft()
        receipt = state.recorded.get('rapid_movement')
        if rapid_people is None or receipt is not None and receipt.suppresses(now):
            state.rapid.clear()
        else:
            state.rapid.offer(rapid_people, aspect, captured, jpeg, now)
        if (len(state.frames) != 4 and not state.rapid.pending(now)) or now - state.submitted < 6:
            return
        if self.task is not None or (self.cleanup_task and not self.cleanup_task.done()):
            self.skipped += 1
            return
        if now < self.retry_after or now - self.last_submit < 6:
            self.skipped += 1
            return
        live = [
            (index, cid, other)
            for index, (cid, other) in enumerate(self.windows.items())
            if 0 <= now - other.seen <= 1.5 and now - other.submitted >= 6
        ]
        eligible = [(other.submitted, index, cid) for index, cid, other in live
                    if len(other.frames) == 4 and (cloud_source is None or
                       other.pending_cloud_cues(other.frames[0][0]) or self._followup_ready(other))]
        rapid = [(frames[-1][0], index, cid) for index, cid, other in live if (frames := other.rapid.pending(now))]
        if not eligible and not rapid:
            return
        # One timely confirmation prevents a slower shared model from
        # expiring every camera's candidate before its second ordinary turn.
        # It cannot chain more follow-ups ahead of the other eligible feeds.
        priority = [item for item in eligible if self._followup_ready(self.windows[item[2]])]
        # 短动作只有一次。优先核对一段后，把下一次机会交还常规窗口。
        use_rapid = bool(rapid and (not self.last_rapid or not eligible))
        if min(rapid if use_rapid else priority or eligible)[2] != camera:
            return
        rapid_frames = state.rapid.take() if use_rapid else ()
        followup = not use_rapid and self._followup_ready(state)
        if not use_rapid:
            state.followup = False
        state.submitted = now
        if camera not in self.served and len(self.served) >= 8:
            inactive = [cid for cid in self.served if cid not in self.windows]
            if inactive:
                self.served.pop(min(inactive, key=self.served.get))
        self.served[camera] = now
        self.last_submit = now
        self.active_camera = camera
        self.active_rapid = rapid_frames
        self.last_rapid = use_rapid
        self.task = asyncio.create_task(self._review(camera, state, rapid_frames or tuple(state.frames),
                                                    rapid=use_rapid, allow_followup=not followup))
        self.task.add_done_callback(self._finished)

    def _corroboration_limit(self, event=None):
        if event == 'prone_posture':
            return POSTURE_CORROBORATION_SECONDS
        return max(18, min(MAX_CORROBORATION_SECONDS, len(self.windows)*6+12))

    def _followup_ready(self, state):
        previous = state.candidate
        return bool(state.followup and previous and state.frames
                    and state.frames[0][0] > previous[1]
                    and 0 < state.frames[-1][0]-previous[1] <= self._corroboration_limit(previous[0]))

    def stability_pending(self, camera, session, revision):
        """Current analysis/corroboration benefits from the existing stable view.

        This is a read-only scheduling hint, never a motor grant or an event.
        The camera owner applies its own finite hold/cooldown and intent checks.
        """
        state = self.windows.get(camera)
        if state is None or state.session != session or state.revision != revision:
            return False
        active = (self.active_camera == camera and self.task is not None
                  and not self.task.done() and not self.task.cancelling())
        rapid = (self.active_rapid if active else ()) or state.rapid.pending(self.clock())
        due = max(self.retry_after, self.last_submit + 6, state.submitted + 6)
        if (rapid and self._current(camera, state, rapid)
                and (active or due <= rapid[-1][0] + MAX_CANDIDATE_AGE)):
            return True
        if len(state.frames) != 4 or not self._current(camera, state, tuple(state.frames)):
            return False
        if state.candidate is None:
            return active
        event, captured, _ = state.candidate
        now = self.clock()
        receipt = state.recorded.get(event)
        if (not 0 <= now - captured <= self._corroboration_limit(event) + 12
                or receipt is not None and receipt.suppresses(now)):
            return False
        return bool(state.followup or active)

    def _finished(self, task):
        if self.task is task:
            self.task = self.active_camera = None
            self.active_rapid = ()
            self.phase = None

    def _current(self, camera, state, frames):
        policy = self.policy()
        return (
            not self.closed
            and self.windows.get(camera) is state
            and policy.enabled
            and policy.visual_review
            and policy.revision == state.revision
            and 0 <= self.clock() - state.seen <= 1.5
            and 0 <= self.clock() - frames[-1][0] <= 12
        )

    async def _review(self, camera, state, frames, *, rapid=False, allow_followup=True):
        try:
            if not self._current(camera, state, frames):
                return
            selected = False
            subject = None
            cloud_activity_revision = None
            policy = self.policy()
            cloud_source = getattr(policy, 'visual_cloud_source', None)
            provider = 'decisions' if cloud_source is not None else 'local'

            def cloud_current():
                return (self._current(camera, state, frames)
                        and self.cloud_current is not None
                        and self.cloud_current(camera, state.session, state.source_context, state.revision))

            def current_window():
                nonlocal frames, selected, subject, cloud_activity_revision
                latest = frames if rapid else tuple(state.frames)
                if (selected or len(latest) != 4 or not self._current(camera, state, latest)
                        or rapid and self.clock()-latest[-1][0] > MAX_CANDIDATE_AGE):
                    raise VisualSourceChanged('visual_source_changed')
                frames = latest
                subject = None if rapid else state.posture_subject(frames)
                selected = True
                if cloud_source is not None and not rapid:
                    # 一段连续的姿态只使用一轮额度，阳性结果再等待独立画面印证。
                    cloud_activity_revision = state.cloud_activity_revision
                    for cue in state.pending_cloud_cues(frames[0][0]):
                        cue.pending = False
                # Reserve cadence at image submission, after any startup or
                # host-budget wait, rather than at the earlier queue admission.
                submitted = self.clock()
                state.submitted = self.last_submit = submitted
                self.served[camera] = submitted
                self.phase = 'reviewing'
                return frames

            def scoped_window():
                selected_frames = current_window()
                return selected_frames, subject is not None

            scoped = getattr(self.engine, 'analyze_scoped', None)
            latest = getattr(self.engine, 'analyze_latest', None)
            if cloud_source is not None:
                if self.cloud is None or (cloud_source.camera, cloud_source.source_context) != (
                        camera, state.source_context):
                    return
                self.phase = 'preparing'
                result = await self.cloud.analyze_scoped(
                    scoped_window, current=cloud_current,
                    regions_for=lambda stamp: next((rects for at, rects in state.regions if at == stamp), ()),
                )
            elif callable(scoped):
                self.phase = 'preparing'
                result = await scoped(scoped_window)
            elif callable(latest):
                self.phase = 'preparing'
                result = await latest(current_window)
            else:
                result = await self.engine.analyze(current_window())
            if not selected:
                raise ValueError('visual_window_not_submitted')
            self.last_camera = camera
            result = VisualResult.model_validate(result) if not isinstance(result, VisualResult) else result
            if not self._current(camera, state, frames):
                return
            self.completed += 1
            state.last_result_at = frames[-1][0]
            self.error = None
            self.failures = 0
            if rapid:
                if result.event == 'rapid_movement':
                    await self._deliver(camera, state, frames, result, rapid=True, provider=provider)
                return
            if cloud_activity_revision is not None and cloud_activity_revision != state.cloud_activity_revision:
                return
            posture_invalid = (result.event == 'prone_posture'
                               and (subject is None or subject != state.posture_subject(frames)))
            if result.event in {"none", "unclear"} or posture_invalid:
                state.candidate = None
                return
            previous = state.candidate
            state.candidate = (result.event, frames[-1][0], frames)
            receipt = state.recorded.get(result.event)
            if receipt is not None and receipt.suppresses(self.clock()):
                return
            # Two disjoint windows of the same kind; a repeated snapshot is not corroboration.
            if (
                not previous
                or previous[0] != result.event
                or frames[0][0] <= previous[1]
                or frames[-1][0] - previous[1] > self._corroboration_limit(result.event)
            ):
                if provider == 'decisions' and not allow_followup:
                    state.candidate = None
                state.followup = allow_followup
                return
            await self._deliver(camera, state, previous[2] + frames, result, provider=provider)
            if provider == 'decisions':
                state.candidate, state.followup = None, False
        except asyncio.CancelledError:
            raise
        except VisualSourceChanged:
            return
        except Exception as error:
            state.candidate = None
            self.failures = min(3, self.failures + 1)
            self.retry_after = self.clock() + min(60, 15 * 2 ** (self.failures - 1))
            reason = str(error)
            self.error = (
                reason
                if reason
                in API_ERRORS | {
                    "memory_pressure",
                    "memory_unknown",
                    "memory_capacity",
                    "cpu_busy",
                    "model_busy",
                    "model_not_installed",
                    "model_pin_mismatch",
                    "local_runtime_missing",
                }
                else "visual_review_deferred"
            )

    async def _deliver(self, camera, state, frames, result, *, rapid=False, provider='local'):
        captured_at = self.wall() - (self.clock() - frames[-1][0])
        arguments = (camera, state.session, frames, result, captured_at)
        options = {'rapid': True} if rapid else {}
        if provider != 'local':
            options['provider'] = provider
        delivered = self.deliver(*arguments, **options)
        if inspect.isawaitable(delivered):
            delivered = await delivered
        if delivered is not False:
            state.recorded[result.event] = (delivered if isinstance(delivered, ReviewReceipt)
                                            else ReviewReceipt(pending=False, saved_at=self.clock()))
            if rapid and state.candidate and state.candidate[0] == result.event:
                state.candidate = None
                state.followup = False

    def status(self):
        policy = self.policy()
        if not policy.enabled or not policy.visual_review:
            for camera in list(self.windows):
                self.clear(camera)
        cloud = getattr(policy, 'visual_cloud_source', None)
        ready = self.cloud.readiness() if cloud and self.cloud is not None else review_readiness()
        if not cloud:
            ready['provider'] = 'local'
        posture = getattr(policy, 'posture_source', None)
        posture_camera = None
        if posture is not None and (state := self.windows.get(posture.camera)) is not None:
            frames = tuple(state.frames)
            if len(frames) == 4 and state.posture_subject(frames) and self._current(posture.camera, state, frames):
                posture_camera = posture.camera
        if cloud:
            # API 只在出现本机线索时调用。静候触发不计作复核延迟。
            reviewing = self.task is not None and not self.task.done() and not self.task.cancelling()
            source = self.windows.get(cloud.camera)
            fresh = (source is not None and source.revision == policy.revision
                     and source.source_context == cloud.source_context and 0 <= self.clock() - source.seen <= 1.5)
            progress = 'waiting_input' if not fresh else 'reviewing' if reviewing else 'standby'
        else:
            live = [state for camera, state in self.windows.items()
                    if state.frames and self._current(camera, state, state.frames)]
            progress = review_progress(live, self.clock(), self._corroboration_limit())
        return dict(
            **ready,
            enabled=bool(policy.enabled and policy.visual_review),
            state=(self.phase or "preparing") if self.task else "deferred" if self.error else "idle",
            completed_windows=self.completed,
            skipped_windows=self.skipped,
            error=self.error,
            resident=bool(getattr(self.engine, "process", None)),
            peak_mlx_mib=round(getattr(self.engine, "peak_mlx_bytes", 0) / 1024**2),
            peak_rss_mib=round(getattr(self.engine, "peak_rss_bytes", 0) / 1024**2),
            camera_count=len(self.windows),
            progress=progress,
            posture_ready_camera=posture_camera,
            posture_revision=policy.revision if posture_camera else None,
        )

    async def invalidate(self):
        self.windows.clear()
        self.served.clear()
        self.last_rapid = False
        self.last_submit, self.retry_after = -math.inf, 0
        self.error, self.failures = None, 0
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.cleanup_task:
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
        await self.engine.close()

    async def close(self):
        self.closed = True
        await self.invalidate()
