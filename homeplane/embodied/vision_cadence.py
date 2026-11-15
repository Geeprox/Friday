"""静止空镜头的推理节奏。只使用本次采集的运动证据。"""

import math
from collections import deque

from .camera_audio import MAX_AGE


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
    wide_rescue_interval = 2.0
    # 限频决策需给候选帧等待下一次调度及下游发布留出余量。
    publishing_margin = .25

    def __init__(self, *, full_rate=None):
        self.full_rate = full_rate or (lambda: False)
        self.intent = None
        self.checked_at = -math.inf
        self.active_until = -math.inf
        self.processed = self.skipped = 0
        self.active_frames = self.unclear_frames = self.recent_motion_frames = 0
        self.deadline_frames = 0
        self.wide_rescue_deferred = 0
        self.wide_pose_rescue = True
        self.next_wide_rescue = -math.inf
        self.quiet_ms = None
        self.latest_capture = None
        self.latencies = deque(maxlen=5)

    def due(self, packet, now, *, force, intent=None):
        if intent != self.intent:
            self.reset()
            self.intent = intent
        quiet = packet.get('scene_quiet_ms')
        self.quiet_ms = quiet if type(quiet) is int else None
        quiet_empty = False
        self.wide_pose_rescue = True
        if self.full_rate() or force:
            self.active_frames += 1
        elif self.quiet_ms is None or self.quiet_ms < self.minimum_quiet_ms:
            self.recent_motion_frames += 1
        elif now < self.active_until or self.checked_at == -math.inf:
            self.unclear_frames += 1
        else:
            observed_age = now - self.latest_capture if self.latest_capture is not None else -1
            quiet_empty = 0 <= observed_age < MAX_AGE
            if now < self.checked_at + self.interval:
                required = self.publishing_margin + max(self.latencies, default=MAX_AGE)
                if quiet_empty and observed_age + required < MAX_AGE:
                    self.skipped += 1
                    return False
                self.deadline_frames += 1
        # 空场仍检查全图和明确的人形线索。只降低无目标的两侧裁切补检频率。
        if quiet_empty:
            self.wide_pose_rescue = now >= self.next_wide_rescue
            if self.wide_pose_rescue:
                self.next_wide_rescue = now + self.wide_rescue_interval
            else:
                self.wide_rescue_deferred += 1
        else:
            self.next_wide_rescue = -math.inf
        self.processed += 1
        return True

    def observed(self, now, *, clear, captured_at):
        self.checked_at = now
        if not clear:
            self.active_until = now + self.settle_seconds
        self.latest_capture = captured_at
        age = now - captured_at
        self.latencies.append(age if 0 <= age < MAX_AGE else MAX_AGE)

    def reset(self):
        self.checked_at = self.active_until = -math.inf
        self.next_wide_rescue = -math.inf
        self.wide_pose_rescue = True
        self.latest_capture = None
        self.latencies.clear()

    def public(self):
        return {'processed': self.processed, 'skipped_static': self.skipped,
                'active_path': self.active_frames, 'recent_motion': self.recent_motion_frames,
                'unclear': self.unclear_frames, 'deadline': self.deadline_frames, 'quiet_ms': self.quiet_ms,
                'wide_rescue_deferred': self.wide_rescue_deferred}
