"""私有进程内对照窗口。只有同一批已运行的四路摄像头参与。"""

import re
import time
from dataclasses import dataclass
from secrets import token_hex


def comparison_scope(report):
    cameras = report['cameras']
    if len(cameras) != 4 or any(row['state'] != 'live' or row['recognition'] != 'running' for row in cameras):
        raise ValueError('four_live_cameras_required')
    if report['interactive_session']:
        raise ValueError('interactive_session_active')
    if any(not isinstance(row['session'], str) or not isinstance(row['mode_revision'], str)
           or type(row['inference']) is not int for row in cameras):
        raise ValueError('vision_measurements_unavailable')
    return (report['worker'], *(row['session'] + ':' + row['mode_revision'] for row in cameras))


def camera_sample(slot, state, recognition, session, mode_revision, counters, frames, latency_ms, voice):
    return dict(slot=slot, state=state, recognition=recognition, session=session, mode_revision=mode_revision,
                inference=counters.get('processed') if counters else None,
                skipped=counters.get('skipped_static') if counters else None,
                quiet_ms=counters.get('quiet_ms') if counters else None,
                frames=frames, latency_ms=latency_ms, voice=voice)


@dataclass
class FullVisionWindow:
    token: str
    scope: tuple
    deadline: float


class VisionBenchmark:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.window: FullVisionWindow | None = None

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
        current = comparison_scope(report)
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

    def public(self):
        return {'wake_motor_sent': self.sent, 'wake_motor_acknowledged': self.acknowledged}
