"""锚定视角的恢复只消费当前摄像头的新画面。"""

from dataclasses import dataclass


MAX_FRAME_AGE = 1.5
SETTLE_SECONDS = 2
VERIFY_SECONDS = 8
BAD_SECONDS = 3
RETRY_SECONDS = 30
RECOVERED_HOLD = 20
PREEMPTED = frozenset(("stale_input", "camera_intent_changed", "motor_busy", "camera_patrol_deferred",
                       "capture_source_changed", "privacy_shutter_closed"))


@dataclass(frozen=True)
class ViewRecall:
    scope: tuple
    revision: str
    preset: int
    observed: float


class CameraViewRecovery:
    def __init__(self, presets=()):
        self.presets = frozenset(preset["idx"] for preset in presets if type(preset.get("idx")) is int)
        self.scope = self.reference = None
        self.sent_at = self.retry_at = None
        self.recovered_at = None
        self.attempts = 0
        self.stamp = self.good_at = self.bad_since = None
        self.state = None
        self.waiting = False

    def _clear_samples(self):
        self.stamp = self.good_at = self.bad_since = None

    def _evidence(self, sample, now):
        captured = sample.get("_local_received")
        coverage = sample.get("room_coverage")
        if (sample.get("status") != "live" or type(captured) not in (int, float)
                or not 0 <= now - captured <= MAX_FRAME_AGE or not coverage
                or coverage["preset"] not in self.presets or not 0 <= now - coverage["at"] <= MAX_FRAME_AGE):
            return None
        return coverage

    def observe(self, sample, now, scope, *, idle):
        evidence = self._evidence(sample, now)
        reference = (evidence["revision"], evidence["preset"]) if evidence else self.reference
        if scope != self.scope or reference != self.reference:
            self.scope, self.reference = scope, reference
            self.sent_at = self.retry_at = None
            self.recovered_at = None
            self.attempts = 0
            self._clear_samples()
        self.waiting = False
        if self.sent_at is not None and now - self.sent_at >= VERIFY_SECONDS:
            self.sent_at = None
        if self.sent_at is not None:
            self.state = "verifying"
            self.waiting = idle
        elif self.attempts and evidence:
            self.state = "unconfirmed"
        else:
            self.state = evidence["state"] if evidence else None
        if not evidence:
            self._clear_samples()
            return None
        at = evidence["at"]
        if self.sent_at is not None and (
                at < self.sent_at + SETTLE_SECONDS or sample["_local_received"] < self.sent_at + SETTLE_SECONDS):
            return None
        if self.stamp is not None and at < self.stamp:
            return None
        if self.stamp is not None and at - self.stamp > MAX_FRAME_AGE:
            self._clear_samples()
        distinct = at != self.stamp
        self.stamp = at
        if evidence["state"] == "covered":
            self.bad_since = None
            if distinct:
                if self.good_at is not None:
                    if self.attempts:
                        self.recovered_at = now
                    self.attempts = 0
                    self.sent_at = self.retry_at = None
                    self.state, self.waiting = "covered", False
                self.good_at = at
            return None
        self.good_at = None
        if not idle or not scope[1]:
            self.bad_since = None
            return None
        if self.sent_at is not None:
            return None
        if self.bad_since is None:
            self.bad_since = at
        if self.attempts < 2 and (self.retry_at is None or now >= self.retry_at):
            if at - self.bad_since >= BAD_SECONDS:
                return ViewRecall(scope, *reference, at)
            self.waiting = True
        # 失败后保留原有的遮挡搜索，同时避免把清晰但偏离的画面纳入普通巡航。
        elif sample.get("scene_visible") is True:
            self.waiting = True
        return None

    def current(self, request, sample, now, scope, *, idle):
        evidence = self._evidence(sample, now)
        return (idle and request.scope == scope == self.scope and self.reference == (request.revision, request.preset)
                and self.sent_at is None and self.attempts < 2
                and self.bad_since is not None and evidence is not None
                and evidence["revision"] == request.revision and evidence["preset"] == request.preset
                and evidence["state"] != "covered" and evidence["at"] >= request.observed
                and evidence["at"] - self.stamp <= MAX_FRAME_AGE)

    def dispatched(self, now):
        self.sent_at, self.retry_at = now, now + RETRY_SECONDS
        self.attempts += 1
        self._clear_samples()
        self.state = "verifying"

    def refused(self, now, error):
        # 到发送前的现场变化不算一次尝试。真实连接或预置位错误按退避重试。
        if self.sent_at is None and str(error) not in PREEMPTED:
            self.attempts += 1
            self.retry_at = now + RETRY_SECONDS
            self.state = "unconfirmed"

    def holding(self, now):
        return self.recovered_at is not None and 0 <= now - self.recovered_at < RECOVERED_HOLD
