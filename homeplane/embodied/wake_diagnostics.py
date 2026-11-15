"""在私有运行日志内关联唤醒和云台发送。"""

import time

from .camera_audio import WakeCameraCue


BLOCKED_CODES = {
    'privacy_shutter_closed': 'wake_motor_privacy_closed',
    'camera_state_unavailable': 'wake_motor_privacy_unavailable',
    'motor_privacy_timeout': 'wake_motor_privacy_timeout',
    'motor_request_expired': 'wake_motor_dispatch_expired',
    'stale_input': 'wake_motor_observation_changed',
}


def record_wake_motor(journal, cue, direction, *, result=None, sent=True, rejected=False, counts=None,
                      submitted_at=None, acknowledged_at=None, blocked=None):
    if not isinstance(cue, WakeCameraCue):
        return
    if result is None:
        now = time.monotonic() if submitted_at is None else submitted_at
        if counts is not None:
            counts.submitted(cue.confirmed_mono, now)
        journal.record('embodied.wake_motion', 'start', code='wake_motor_' + direction,
                       correlation=cue.correlation)
        return now
    if result == 'ok' and counts is not None:
        counts.acknowledged_at(submitted_at, time.monotonic() if acknowledged_at is None else acknowledged_at)
    code = ('wake_motor_acknowledged' if result == 'ok' else
            'wake_motor_rejected' if rejected else
            'wake_motor_result_unknown' if sent else BLOCKED_CODES.get(blocked, 'wake_motor_not_sent'))
    journal.record('embodied.wake_motion', 'result', result=result, code=code,
                   correlation=cue.correlation)
