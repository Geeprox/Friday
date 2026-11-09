"""Fresh, same-session windows create private review items, never speech jobs."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections import deque
from dataclasses import dataclass, field

from .temporal_model import MlxWindowProcess, VisualResult, VisualSourceChanged, model_readiness


@dataclass
class WindowState:
    session: str
    revision: int
    frames: deque = field(default_factory=lambda: deque(maxlen=4))
    seen: float = 0
    submitted: float = -math.inf
    candidate: tuple | None = None
    followup: bool = False
    recorded: dict = field(default_factory=dict)


class TemporalReviewer:
    def __init__(self, *, policy, deliver, engine=None, clock=time.monotonic, wall=time.time):
        self.policy, self.deliver = policy, deliver
        self.engine = engine or MlxWindowProcess()
        self.clock, self.wall = clock, wall
        self.windows = {}
        self.served = {}
        self.task = None
        self.active_camera = None
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
        if self.task and (self.active_camera == camera or owns_recent):
            if not self.task.cancelling():
                self.task.cancel()
        elif owns_recent and (not self.cleanup_task or self.cleanup_task.done()):
            self.cleanup_task = asyncio.create_task(self.engine.close())
        if self.last_camera == camera:
            self.last_camera = None

    def offer(self, camera, session, revision, captured, jpeg, *, stable):
        now = self.clock()
        if self.closed or not stable or not math.isfinite(captured) or not 0 <= now - captured <= 1.5:
            self.clear(camera)
            return
        if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= 512 * 1024:
            self.clear(camera)
            return
        state = self.windows.get(camera)
        if state and (
            state.session != session
            or state.revision != revision
            or captured <= state.seen
            or captured - state.seen > 1.75
        ):
            self.clear(camera)
            state = None
        if state is None:
            if len(self.windows) >= 8:
                self.skipped += 1
                return
            # View changes retire every pixel/candidate, but must not erase
            # scheduling debt and repeatedly put this camera before its peers.
            state = self.windows[camera] = WindowState(session, revision, submitted=self.served.get(camera, -math.inf))
        state.seen = captured
        if not state.frames or captured - state.frames[-1][0] >= 1:
            state.frames.append((captured, jpeg))
        if len(state.frames) != 4 or now - state.submitted < 6:
            return
        if self.task is not None or (self.cleanup_task and not self.cleanup_task.done()):
            self.skipped += 1
            return
        if now < self.retry_after or now - self.last_submit < 6:
            self.skipped += 1
            return
        eligible = [
            (other.submitted, index, cid)
            for index, (cid, other) in enumerate(self.windows.items())
            if len(other.frames) == 4 and 0 <= now - other.seen <= 1.5 and now - other.submitted >= 6
        ]
        # One timely confirmation prevents a slower shared model from
        # expiring every camera's candidate before its second ordinary turn.
        # It cannot chain more follow-ups ahead of the other eligible feeds.
        priority = [item for item in eligible if self._followup_ready(self.windows[item[2]])]
        if eligible and min(priority or eligible)[2] != camera:
            return
        followup = self._followup_ready(state)
        state.followup = False
        state.submitted = now
        if camera not in self.served and len(self.served) >= 8:
            inactive = [cid for cid in self.served if cid not in self.windows]
            if inactive:
                self.served.pop(min(inactive, key=self.served.get))
        self.served[camera] = now
        self.last_submit = now
        self.active_camera = camera
        self.task = asyncio.create_task(self._review(camera, state, tuple(state.frames), allow_followup=not followup))
        self.task.add_done_callback(self._finished)

    def _corroboration_limit(self):
        return max(18, min(60, len(self.windows)*6+12))

    def _followup_ready(self, state):
        previous = state.candidate
        return bool(state.followup and previous and state.frames
                    and state.frames[0][0] > previous[1]
                    and 0 < state.frames[-1][0]-previous[1] <= self._corroboration_limit())

    def stability_pending(self, camera, session, revision):
        """Current analysis/corroboration benefits from the existing stable view.

        This is a read-only scheduling hint, never a motor grant or an event.
        The camera owner applies its own finite hold/cooldown and intent checks.
        """
        state = self.windows.get(camera)
        if (state is None or state.session != session or state.revision != revision
                or len(state.frames) != 4
                or not self._current(camera, state, tuple(state.frames))):
            return False
        active = (self.active_camera == camera and self.task is not None
                  and not self.task.done() and not self.task.cancelling())
        if state.candidate is None:
            return active
        event, captured, _ = state.candidate
        now = self.clock()
        if (not 0 <= now - captured <= self._corroboration_limit() + 12
                or now - state.recorded.get(event, -math.inf) < 60):
            return False
        return bool(state.followup or active)

    def _finished(self, task):
        if self.task is task:
            self.task = self.active_camera = None
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

    async def _review(self, camera, state, frames, *, allow_followup=True):
        try:
            if not self._current(camera, state, frames):
                return
            selected = False
            def current_window():
                nonlocal frames, selected
                latest = tuple(state.frames)
                if selected or len(latest) != 4 or not self._current(camera, state, latest):
                    raise VisualSourceChanged('visual_source_changed')
                frames = latest
                selected = True
                # Reserve cadence at image submission, after any startup or
                # host-budget wait, rather than at the earlier queue admission.
                submitted = self.clock()
                state.submitted = self.last_submit = submitted
                self.served[camera] = submitted
                self.phase = 'reviewing'
                return frames
            latest = getattr(self.engine, 'analyze_latest', None)
            if callable(latest):
                self.phase = 'preparing'
                result = await latest(current_window)
                if not selected:
                    raise ValueError('visual_window_not_submitted')
            else:
                result = await self.engine.analyze(current_window())
            self.last_camera = camera
            result = VisualResult.model_validate(result) if not isinstance(result, VisualResult) else result
            if not self._current(camera, state, frames):
                return
            self.completed += 1
            self.error = None
            self.failures = 0
            if result.event in {"none", "unclear"}:
                state.candidate = None
                return
            previous = state.candidate
            state.candidate = (result.event, frames[-1][0], frames)
            if self.clock() - state.recorded.get(result.event, -math.inf) < 60:
                return
            # Two disjoint windows of the same kind; a repeated snapshot is not corroboration.
            if (
                not previous
                or previous[0] != result.event
                or frames[0][0] <= previous[1]
                or frames[-1][0] - previous[1] > self._corroboration_limit()
            ):
                state.followup = allow_followup
                return
            delivered = self.deliver(
                camera,
                state.session,
                previous[2] + frames,
                result,
                self.wall() - (self.clock() - frames[-1][0]),
            )
            if inspect.isawaitable(delivered):
                delivered = await delivered
            if delivered is False:
                return
            state.recorded[result.event] = self.clock()
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
                in {
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

    def status(self):
        policy = self.policy()
        if not policy.enabled or not policy.visual_review:
            for camera in list(self.windows):
                self.clear(camera)
        ready = model_readiness()
        return dict(
            **ready,
            enabled=bool(policy.enabled and policy.visual_review),
            state=(self.phase or "preparing") if self.task else "deferred" if self.error else "idle",
            completed_windows=self.completed,
            skipped_windows=self.skipped,
            error=self.error,
            resident=bool(getattr(self.engine, "process", None)),
            peak_mlx_mib=round(getattr(self.engine, "peak_mlx_bytes", 0) / 1024**2),
            camera_count=len(self.windows),
        )

    async def invalidate(self):
        self.windows.clear()
        self.served.clear()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.cleanup_task:
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
        await self.engine.close()

    async def close(self):
        self.closed = True
        await self.invalidate()
