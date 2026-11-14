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
        # 画面过暗、全黑或过曝都不能用于推断空场。空间配准有独立的准入条件。
        sample_range = int(sample.max()) - int(sample.min())
        gray = float(sample.mean())
        usable = 12 <= gray <= 243 and sample_range >= 22
        if (not usable or self.previous_at is None or not 0 < at - self.previous_at <= .8
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

    def __init__(self, *, full_rate=None):
        self.full_rate = full_rate or (lambda: False)
        self.checked_at = -math.inf
        self.active_until = -math.inf
        self.processed = self.skipped = 0
        self.active_frames = self.unclear_frames = self.recent_motion_frames = 0
        self.quiet_ms = None

    def due(self, packet, now, *, force):
        quiet = packet.get('scene_quiet_ms')
        self.quiet_ms = quiet if type(quiet) is int else None
        if self.full_rate() or force:
            self.active_frames += 1
        elif self.quiet_ms is None or self.quiet_ms < self.minimum_quiet_ms:
            self.recent_motion_frames += 1
        elif now < self.active_until or self.checked_at == -math.inf:
            self.unclear_frames += 1
        elif now < self.checked_at + self.interval:
            self.skipped += 1
            return False
        self.processed += 1
        return True

    def observed(self, now, *, clear):
        self.checked_at = now
        if not clear:
            self.active_until = now + self.settle_seconds

    def reset(self):
        self.checked_at = self.active_until = -math.inf

    def public(self):
        return {'processed': self.processed, 'skipped_static': self.skipped,
                'active_path': self.active_frames, 'recent_motion': self.recent_motion_frames,
                'unclear': self.unclear_frames, 'quiet_ms': self.quiet_ms}
