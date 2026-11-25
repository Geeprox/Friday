"""从各路本机语音段选出同一句话的收音来源。"""

import asyncio
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

# 只允许在唤醒与设备动作之前明确放弃的语句转交另一路。
RETRY_OTHER_MIC = object()


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
    view_revision: int | None = None


@dataclass(frozen=True, slots=True)
class WakeFollowup:
    camera: str
    session: str
    epoch: int
    source: tuple[str, str]
    revision: int
    starts_after: float
    expires: float


@dataclass(slots=True)
class _ActiveUtterance:
    camera: str
    window: tuple[float, float]
    full: bool
    alternates: dict


def overlaps(first, second):
    return first[0] <= second[1] + .25 and second[0] <= first[1] + .25


def _attending(service, item):
    return item.attending and (item.view_revision is None or item.view_revision == service.view_revision)


def _priority(row, *, use_attention=True):
    service, item = row
    level = item.level * (1.10 if use_attention and _attending(service, item) else 1.0)
    return not item.wake_only, level, item.ended - item.started, service.camera_id


class VoiceRouting:
    COLLECT_SECONDS = .35
    START_LIMIT = 3.0
    REPEAT_LIMIT = 12.0
    FOLLOWUP_SECONDS = 6.0
    FOLLOWUP_LEVEL_RATIO = 1.25

    def __init__(self, input_open, *, clock=time.monotonic):
        self.input_open = input_open
        self.clock = clock
        self.pending = {}
        self.recent = deque(maxlen=12)
        self.followup = None
        self.reply_until = 0.0
        self.replies = 0
        self._active = None
        self.task = None

    def listening(self, now):
        return self.input_open() and self.replies == 0 and now >= self.reply_until

    @contextmanager
    def replying(self, until):
        self.reply_until = max(self.reply_until, until)
        self.pending.clear()
        self.followup = None
        if self._active is not None:
            self._active.alternates.clear()
        self.replies += 1
        try:
            yield
        finally:
            self.replies -= 1
            self.reply_until = max(self.reply_until, self.clock() + .25)

    def discard(self, camera):
        self.pending.pop(camera, None)
        self.discard_followup(camera)
        if self._active is not None:
            self._active.alternates.pop(camera, None)

    def discard_followup(self, camera):
        if self.followup is not None and self.followup.camera == camera:
            self.followup = None

    def open_followup(self, service, item, cue):
        self.followup = WakeFollowup(
            service.camera_id, cue.session, item.epoch, cue.source, cue.voice_revision,
            item.ended, cue.confirmed_mono + self.FOLLOWUP_SECONDS,
        )

    def followup_for(self, service, item):
        followup = self.followup
        if (
            followup is not None
            and not item.wake_only
            and service.camera_id == followup.camera
            and item.source == followup.source
            and item.epoch == followup.epoch
            and service.camera_session == followup.session
            and service.audio_allowed
            and followup.starts_after <= item.started < followup.expires
            and self.listening(self.clock())
            and service.current(item.captured, item.epoch)
        ):
            return followup
        return None

    def waiting(self, service, source, now):
        entry = self.pending.get(service.camera_id)
        return (entry is not None and entry[0] is service and entry[1].source == source
                and self._eligible(*entry, now))

    def _current(self, service, item, now):
        return (self.listening(now) and item.started >= self.reply_until
                and 0 <= now - item.captured <= self.START_LIMIT
                and service.current(item.captured, item.epoch))

    def _duplicates(self, item, now, *, except_window=None):
        return any(
            window is not except_window and 0 <= now - admitted <= self.REPEAT_LIMIT
            and overlaps((item.started, item.ended), window)
            for admitted, window in self.recent)

    def _eligible(self, service, item, now, *, except_window=None):
        return not self._duplicates(item, now, except_window=except_window) and self._current(service, item, now)

    def offer(self, service, item):
        now = self.clock()
        if not self._current(service, item, now):
            return False
        camera = service.camera_id
        active = self._active
        candidates, except_window = self.pending, None
        if active is not None and active.full and overlaps(active.window, (item.started, item.ended)):
            # 完整语句已预留去重窗口。各副路各保留一份，等首路明确失败再选。
            if camera == active.camera:
                return False
            candidates, except_window = active.alternates, active.window
        if self._duplicates(item, now, except_window=except_window):
            return False
        previous = candidates.get(camera)
        if (item.wake_only and previous and not previous[1].wake_only
                and self._eligible(*previous, now, except_window=except_window)):
            return False
        candidates[camera] = (service, item)
        asyncio.get_running_loop().call_later(
            self.START_LIMIT - (now - item.captured), self._expire, camera, item.captured)
        if self.task is None:
            self.task = asyncio.create_task(self._dispatch(), name='friday-voice-routing')
        return True

    def _expire(self, camera, captured):
        candidate = self.pending.get(camera)
        if candidate is not None and candidate[1].captured == captured:
            self.pending.pop(camera)
        active = self._active
        if active is not None:
            candidate = active.alternates.get(camera)
            if candidate is not None and candidate[1].captured == captured:
                active.alternates.pop(camera)

    def _select(self, now):
        fresh = [(service, item) for service, item in self.pending.values()
                 if self._eligible(service, item, now)]
        self.pending.clear()
        if not fresh:
            return None
        _, first = min(fresh, key=lambda row: (row[1].started, row[0].camera_id))
        window = first.started, first.ended
        candidates = []
        for service, item in fresh:
            if overlaps(window, (item.started, item.ended)):
                candidates.append((service, item))
            else:
                self.pending[service.camera_id] = (service, item)
        # 仅可识别唤醒词的短片段不能抢占同一句的完整语音。
        # 注视只在收音证据接近时加权，入镜本身不影响录入资格。
        service, item = max(candidates, key=_priority)
        if self.followup is not None and not _attending(service, item):
            strongest = max((audio.level for _, audio in candidates if not audio.wake_only), default=0)
            # 只有收音强度接近时才优先使用刚确认唤醒的镜头。
            continuing = next(
                (row for row in candidates if row[1].level * self.FOLLOWUP_LEVEL_RATIO >= strongest
                 and self.followup_for(*row) is not None),
                None,
            )
            if continuing is not None:
                service, item = continuing
        window = min(item.started for _, item in candidates), max(item.ended for _, item in candidates)
        alternates = {other.camera_id: (other, audio) for other, audio in candidates
                      if other.camera_id != service.camera_id}
        return service, item, _ActiveUtterance(service.camera_id, window, not item.wake_only, alternates)

    async def _dispatch(self):
        try:
            while self.pending:
                await asyncio.sleep(self.COLLECT_SECONDS)
                selection = self._select(self.clock())
                if selection is None:
                    continue
                service, item, active = selection
                window = active.window
                self._active = active
                job = service.spawn(self._transcribe(service, item, window))
                result, = await asyncio.gather(job, return_exceptions=True)
                fallback = None
                if result is RETRY_OTHER_MIC:
                    now = self.clock()
                    fresh = (row for row in active.alternates.values()
                             if self._eligible(*row, now, except_window=window))
                    # 备用镜头各自验证唤醒词，排序使用该路收音。
                    fallback = max(fresh, key=lambda row: _priority(row, use_attention=False), default=None)
                active.alternates.clear()
                self._active = None
                if fallback is not None:
                    service, item = fallback
                    job = service.spawn(self._transcribe(service, item, window, alternate=True))
                    await asyncio.gather(job, return_exceptions=True)
        finally:
            self._active = None
            self.task = None

    async def _transcribe(self, service, item, window, *, alternate=False):
        now = self.clock()
        duplicate = self._duplicates(item, now)
        if not alternate and duplicate:
            return None
        if not self._current(service, item, now):
            return RETRY_OTHER_MIC
        # 短片段识别失败时，迟到的完整语音仍有机会被识别。
        if not item.wake_only and not duplicate:
            self.recent.append((now, window))
        service.voice_busy = True
        # 备用镜头必须听清唤醒词。原镜头的注视和人员归属不能转移。
        result = await service.transcribe(item.pcm, None if alternate else item.owner,
                                          item.captured, item.attending and not alternate,
                                          item.epoch, item.source, item.wake_only, utterance=item,
                                          followup=None if alternate else self.followup_for(service, item))
        if item.wake_only and result is True and not duplicate:
            self.recent.append((self.clock(), window))
        return result
