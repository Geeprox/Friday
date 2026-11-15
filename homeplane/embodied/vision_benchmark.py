"""私有进程内对照窗口。只有同一批已运行的四路摄像头参与。"""

import math
import re
import time
from bisect import bisect_left
from dataclasses import dataclass
from secrets import token_hex


LATENCY_LIMITS_MS = (5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 750, 1000,
                     1500, 2000, 3000, 5000, 10000, 20000, 45000)
TIMING_KEYS = {'vision_request', 'native_processing', 'asr_request', 'voice_segment_to_wake_confirm',
               'wake_confirm_to_motor_submit', 'motor_submit_to_ack'}


class LatencyHistogram:
    """每段只保留固定大小的计数，不保存帧号、事件编号或时间点。"""

    def __init__(self):
        self.bins = [0] * (len(LATENCY_LIMITS_MS) + 1)

    def record_ms(self, milliseconds):
        valid = type(milliseconds) is int or (type(milliseconds) is float and math.isfinite(milliseconds))
        if valid and milliseconds >= 0:
            self.bins[bisect_left(LATENCY_LIMITS_MS, milliseconds)] += 1


def timing_deltas(previous, current):
    if previous is None and current is None:
        return {}
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or previous.keys() != current.keys() or not previous.keys() <= TIMING_KEYS):
        raise ValueError('measurement_timing_changed')
    result = {}
    for key, before in previous.items():
        after = current[key]
        if not all(isinstance(bins, list) and len(bins) == len(LATENCY_LIMITS_MS) + 1
                   and all(type(value) is int and value >= 0 for value in bins) for bins in (before, after)):
            raise ValueError('invalid_measurement_timing')
        delta = [end - start for start, end in zip(before, after)]
        if min(delta) < 0:
            raise ValueError('measurement_counter_reset')
        result[key] = delta
    return result


def summarize_timing(buckets):
    samples = sum(buckets)
    threshold = (samples * 95 + 99) // 100
    total = 0
    p95 = None
    if samples:
        for limit, count in zip(LATENCY_LIMITS_MS, buckets):
            total += count
            if total >= threshold:
                p95 = limit
                break
    return {'samples': samples, 'p95_upper_ms': p95, 'above_range': buckets[-1]}


def comparison_scope(report, *, require_fresh_preview=False):
    cameras = report['cameras']
    if len(cameras) != 4:
        raise ValueError('four_live_cameras_required')
    # 新样本以采集来源为准。识别和预览的短时停滞都是对照结果。
    has_capture = any('capture_epoch' in row for row in cameras)
    if has_capture:
        if any(type(row.get('capture_epoch')) is not int or row['capture_epoch'] < 1
               or row['state'] in {'stopped', 'shutter_closed'}
               or require_fresh_preview and row.get('preview_fresh') is not True for row in cameras):
            raise ValueError('four_live_cameras_required')
    elif any(row['state'] != 'live' or row['recognition'] != 'running' for row in cameras):
        raise ValueError('four_live_cameras_required')
    if report['interactive_session']:
        raise ValueError('interactive_session_active')
    if any(not isinstance(row['session'], str) or not isinstance(row['mode_revision'], str)
           or type(row['inference']) is not int for row in cameras):
        raise ValueError('vision_measurements_unavailable')
    if has_capture:
        return (report['worker'], *(f"{row['capture_epoch']}:{row['session']}:{row['mode_revision']}"
                                    for row in cameras))
    return (report['worker'], *(row['session'] + ':' + row['mode_revision'] for row in cameras))


def camera_sample(slot, state, recognition, session, mode_revision, counters, frames, latency_ms, voice, *,
                  timings=None, body_rescue_passes=None):
    sample = dict(slot=slot, state=state, recognition=recognition, session=session, mode_revision=mode_revision,
                  inference=counters.get('processed') if counters else None,
                  skipped=counters.get('skipped_static') if counters else None,
                  quiet_ms=counters.get('quiet_ms') if counters else None,
                  frames=frames, latency_ms=latency_ms, voice=voice, body_rescue_passes=body_rescue_passes)
    if timings is not None:
        sample['timings'] = {key: histogram.bins.copy() for key, histogram in timings.items()}
    return sample


@dataclass
class FullVisionWindow:
    token: str
    scope: tuple
    deadline: float


class VisionBenchmark:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.window: FullVisionWindow | None = None
        self.primary_timings = {}

    def active(self):
        if self.window is not None and self.clock() >= self.window.deadline:
            self.window = None
        return self.window is not None

    def status(self, report):
        if self.active():
            try:
                current = comparison_scope(report)
            except ValueError:
                current = None
            if current != self.window.scope:
                self.window = None
        return dict(report, full_rate=self.active())

    def begin(self, seconds, report, scope):
        if self.active():
            raise ValueError('vision_benchmark_busy')
        if type(seconds) is not int or not 1 <= seconds <= 75:
            raise ValueError('invalid_benchmark_duration')
        current = comparison_scope(report, require_fresh_preview=True)
        if current != scope:
            raise ValueError('vision_benchmark_source_changed')
        self.window = FullVisionWindow(token_hex(16), current, self.clock() + seconds)
        return self.window.token

    def cancel(self, token):
        if self.active() and self.window.token != token:
            raise ValueError('vision_benchmark_busy')
        self.window = None


def valid_token(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value) is not None


class WakeMotionCounts:
    def __init__(self):
        self.sent = self.acknowledged = 0
        self.timings = {'wake_confirm_to_motor_submit': LatencyHistogram(), 'motor_submit_to_ack': LatencyHistogram()}

    def submitted(self, confirmed_at, at):
        self.sent += 1
        if confirmed_at is not None:
            self.timings['wake_confirm_to_motor_submit'].record_ms((at - confirmed_at) * 1000)

    def acknowledged_at(self, submitted_at, at):
        self.acknowledged += 1
        if submitted_at is not None:
            self.timings['motor_submit_to_ack'].record_ms((at - submitted_at) * 1000)

    def public(self):
        return {'wake_motor_sent': self.sent, 'wake_motor_acknowledged': self.acknowledged}
