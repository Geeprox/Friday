"""静止空镜头的推理节奏。只使用本次采集的运动证据。"""

import math


class MotionSample:
    """用小图同时比较上一帧与静止基准，识别局部和缓慢变化。"""

    def __init__(self):
        self.previous = self.anchor = None
        self.previous_at = self.quiet_since = None

    @staticmethod
    def changed(current, reference):
        import cv2
        import numpy as np

        differences = cv2.absdiff(current, reference) >= 10
        # 小范围变化也会唤醒，避免以全图均值淹没远处的人或手。
        return bool(np.any(differences.reshape(6, 8, 8, 12).sum(axis=(1, 3)) >= 8))

    def quiet_ms(self, frame, at):
        import cv2

        sample = cv2.cvtColor(cv2.resize(frame, (96, 48), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        if (self.previous_at is None or not 0 < at - self.previous_at <= .6
                or self.changed(sample, self.previous) or self.changed(sample, self.anchor)):
            self.anchor = sample
            self.quiet_since = at
        self.previous = sample
        self.previous_at = at
        return min(60000, max(0, round((at - self.quiet_since) * 1000)))


class VisionCadence:
    # 保留足够的时间预算，供现有 1.5 秒的画面、收音和手势有效期使用。
    interval = .58
    minimum_quiet_ms = 4500
    settle_seconds = 4

    def __init__(self):
        self.checked_at = -math.inf
        self.active_until = -math.inf
        self.processed = self.skipped = 0

    def due(self, packet, now, *, force):
        quiet = packet.get('scene_quiet_ms')
        if (force or type(quiet) is not int or quiet < self.minimum_quiet_ms
                or now >= self.checked_at + self.interval or now < self.active_until):
            self.processed += 1
            return True
        self.skipped += 1
        return False

    def observed(self, now, *, clear):
        self.checked_at = now
        if not clear:
            self.active_until = now + self.settle_seconds

    def reset(self):
        self.checked_at = self.active_until = -math.inf

    def public(self):
        return {'processed': self.processed, 'skipped_static': self.skipped}
