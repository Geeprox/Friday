"""从各路本机语音段选出同一句话的收音来源。"""

import asyncio
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import time


@dataclass(frozen=True, slots=True)
class Utterance:
    pcm: bytes
    owner: int | str | None
    captured: float
    attending: bool
    epoch: int
    source: tuple[str, str]
    started: float
    ended: float
    level: float
    wake_only: bool = False
    view_epoch: int | None = None
    source_epoch: int = 0


def overlaps(first, second):
    return first[0] <= second[1] + .25 and second[0] <= first[1] + .25


class VoiceRouting:
    COLLECT_SECONDS = .35
    FOLLOWUP_LEVEL_RATIO = 1.25
    START_LIMIT = 3.0
    DECODE_SECONDS = 8.0
    REPEAT_LIMIT = 12.0

    def __init__(self, input_open, *, wake_feedback=None, clock=time.monotonic):
        self.input_open = input_open
        self.wake_feedback = wake_feedback
        self.clock = clock
        self.pending = {}
        self.dispatching = None
        self.inflight = {}
        self.recent = deque(maxlen=12)
        self.followup_owner = None
        self.reply_until = 0.0
        self.replies = 0
        self.task = None

    def listening(self, now):
        return self.input_open() and self.replies == 0 and now >= self.reply_until

    @contextmanager
    def replying(self, until):
        self.reply_until = max(self.reply_until, until)
        self.pending.clear()
        self.dispatching = None
        self.clear_followup()
        self.replies += 1
        try:
            yield
        finally:
            self.replies -= 1
            self.reply_until = max(self.reply_until, self.clock() + .25)

    def discard(self, camera):
        self.pending.pop(camera, None)
        if self.dispatching is not None and self.dispatching[0].camera_id == camera:
            self.dispatching = None
        if self.followup_owner is not None and self.followup_owner.camera_id == camera:
            self.clear_followup()

    def clear_followup(self):
        if self.followup_owner is not None:
            self.followup_owner.revoke_wake_followup()
            self.followup_owner = None

    def acknowledge(self, service, followup, *, deadline):
        if self.followup_owner is not service:
            self.clear_followup()
        self.followup_owner = service
        if self.wake_feedback is not None:
            def current():
                now = self.clock()
                return (now < deadline and self.listening(now) and not service.speaking
                        and service.wake_followup is followup
                        and service.followup_for(followup.source, now, now) is followup)

            service.spawn(self.wake_feedback(current=current))

    def waiting(self, service, source, now):
        """只为同一采集来源仍可受理的语音段暂缓唤醒转向。"""
        for entry in (self.pending.get(service.camera_id), self.dispatching):
            if entry is not None:
                owner, item = entry
                if owner is service and item.source == source and self._eligible(service, item, now):
                    return True
        return False

    def _eligible(self, service, item, now):
        return (self.listening(now) and item.started >= self.reply_until
                and 0 <= now - item.captured <= self.START_LIMIT
                and service.current(item.captured, item.epoch)
                and item.source_epoch == service.audio_source_epoch
                and not any(0 <= now - admitted <= self.REPEAT_LIMIT
                            and overlaps((item.started, item.ended), window)
                            for admitted, window in self.recent))

    def offer(self, service, item):
        now = self.clock()
        if not self._eligible(service, item, now):
            return
        camera = service.camera_id
        previous = self.pending.get(camera)
        if item.wake_only and previous and not previous[1].wake_only and self._eligible(*previous, now):
            return
        self.pending[camera] = (service, item)
        asyncio.get_running_loop().call_later(
            self.START_LIMIT - (now - item.captured), self._expire, camera, item.captured)
        self._schedule()

    def _expire(self, camera, captured):
        candidate = self.pending.get(camera)
        if candidate is not None and candidate[1].captured == captured:
            self.pending.pop(camera, None)

    def _schedule(self):
        if self.task is None and self.pending:
            self.task = asyncio.create_task(self._dispatch(), name='friday-voice-routing')

    def _select(self, now):
        fresh = [(service, item) for service, item in self.pending.values()
                 if self._eligible(service, item, now)]
        self.pending = {service.camera_id: (service, item) for service, item in fresh}
        available = [(service, item) for service, item in fresh if service.camera_id not in self.inflight]
        if not available:
            return None
        _, first = min(available, key=lambda row: (row[1].started, row[0].camera_id))
        window = first.started, first.ended
        candidates = [(service, item) for service, item in available
                      if overlaps(window, (item.started, item.ended))]
        strongest = max((item.level for _, item in candidates if not item.wake_only), default=0.0)

        def priority(row):
            service, item = row
            followup = service.followup_for(item.source, item.started, item.captured)
            # 只有收音接近时才让刚唤醒的镜头优先，避免接走其他房间的清楚语音。
            if item.level * self.FOLLOWUP_LEVEL_RATIO < strongest:
                followup = None
            attending = item.attending and (item.view_epoch is None or item.view_epoch == service.voice_view_epoch)
            return (
                not item.wake_only, followup is not None,
                item.level * (1.10 if attending else 1.0),
                item.ended - item.started, service.camera_id,
            )

        # 仅可识别唤醒词的短片段不能抢占同一句的完整语音。
        # 注视只在收音证据接近时加权，入镜本身不影响录入资格。
        service, item = max(candidates, key=priority)
        for candidate_service, _ in candidates:
            self.pending.pop(candidate_service.camera_id)
        window = min(item.started for _, item in candidates), max(item.ended for _, item in candidates)
        return service, item, window

    async def _dispatch(self):
        try:
            while self.pending:
                await asyncio.sleep(self.COLLECT_SECONDS)
                selection = self._select(self.clock())
                if selection is None:
                    return
                service, item, window = selection
                self.dispatching = (service, item)
                recognized = asyncio.Event()
                job = service.spawn(self._transcribe(service, item, window, recognized))
                self.inflight[service.camera_id] = job
                job.add_done_callback(
                    lambda finished, camera=service.camera_id, ready=recognized:
                    self._turn_finished(camera, finished, ready)
                )
                await recognized.wait()
        finally:
            self.dispatching = None
            self.task = None

    def _turn_finished(self, camera, job, recognized):
        if self.inflight.get(camera) is job:
            self.inflight.pop(camera)
        recognized.set()
        if not job.cancelled():
            job.exception()
        self._schedule()

    async def _transcribe(self, service, item, window, recognized):
        now = self.clock()
        if self.dispatching is None or self.dispatching[1] is not item:
            return
        self.dispatching = None
        if not self._eligible(service, item, now):
            return
        # 短片段识别失败时，迟到的完整语音仍有机会被识别。
        if not item.wake_only:
            self.recent.append((now, window))
        service.voice_busy = True

        def decoded(wake_detected=False):
            if item.wake_only and wake_detected:
                self.recent.append((self.clock(), window))
            recognized.set()

        def current():
            return (self.listening(self.clock()) and item.started >= self.reply_until
                    and service.current(item.captured, item.epoch)
                    and item.source_epoch == service.audio_source_epoch)

        await service.transcribe(
            item.pcm, item.owner, item.captured, item.attending, item.epoch, item.source,
            item.wake_only, decoded=decoded, decode_current=current,
            decode_deadline=min(now + self.DECODE_SECONDS, item.captured + 10),
            started=item.started, ended=item.ended, view_epoch=item.view_epoch, source_epoch=item.source_epoch)
