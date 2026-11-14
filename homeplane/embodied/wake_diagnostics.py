"""在私有运行日志内关联唤醒和云台发送。"""

from .camera_audio import WakeCameraCue


def record_wake_motor(journal, cue, direction, *, result=None, sent=True, rejected=False):
    if not isinstance(cue, WakeCameraCue):
        return
    if result is None:
        journal.record('embodied.wake_motion', 'start', code='wake_motor_' + direction,
                       correlation=cue.correlation)
        return
    code = ('wake_motor_acknowledged' if result == 'ok' else
            'wake_motor_rejected' if rejected else
            'wake_motor_result_unknown' if sent else 'wake_motor_not_sent')
    journal.record('embodied.wake_motion', 'result', result=result, code=code,
                   correlation=cue.correlation)
