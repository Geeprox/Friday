"""语音运行状态与临时指令回执的独立输出。"""

VOICE_RECEIPT_SECONDS = 60

VOICE_LABELS = {
    'not_started': '语音未开启',
    'stopped': '语音未开启',
    'loading_local_models': '语音准备中',
    'ready': '等待语音指令',
    'transcribing': '正在识别',
    'paused': '语音输入已暂停',
    'unavailable': '语音暂不可用',
}


def voice_health(report):
    """只投影通用状态消费者所需的固定字段。"""
    report = report if isinstance(report, dict) else {}
    status = report.get('voice')
    status = status if isinstance(status, str) and status in VOICE_LABELS else 'unknown'
    listening = status == 'ready' and report.get('voice_activity') is True
    result = dict(enabled=report.get('enabled') is True, voice=status, voice_activity=listening,
                  reason='听到人声，请继续' if listening else VOICE_LABELS.get(status, '等待语音状态'))
    for name in ('audio_input', 'audio_analysis'):
        value = report.get(name)
        if isinstance(value, dict) and value.get('status') in ('ready', 'waiting', 'unavailable'):
            result[name] = {'status': value['status']}
            if name == 'audio_input' and value.get('reason') == 'input_stale':
                result[name]['reason'] = 'input_stale'
    return result


def home_status(report):
    """保留互动舞台需要的实时注视、窗帘位置和手势进度。"""
    report = report if isinstance(report, dict) else {}
    result = voice_health(report)
    owner = report.get('owner_track')
    result['owner_track'] = owner if type(owner) is int or (
        isinstance(owner, str) and owner.isascii() and owner.isdigit()) else None
    curtain = report.get('curtain')
    if isinstance(curtain, dict):
        status, position = curtain.get('status'), curtain.get('position')
        result['curtain'] = {
            'status': status if status in ('ready', 'moving', 'connecting', 'unconfirmed', 'unavailable',
                                           'checking', 'fault') else 'waiting',
            'position': position if type(position) in (int, float) and 0 <= position <= 100 else None,
        }
    gesture = report.get('window_gesture')
    if isinstance(gesture, dict):
        progress = gesture.get('progress')
        if type(progress) in (float, int) and 0 <= progress <= 1:
            result['window_gesture'] = {'progress': progress}
    attention = report.get('attention')
    if isinstance(attention, list) and result['owner_track'] is not None:
        for row in attention:
            if isinstance(row, dict) and row.get('person_id') == owner:
                result['attention'] = [
                    dict(person_id=owner, qualified=row.get('qualified') is True,
                         state='checking' if row.get('state') == 'checking' else 'waiting',
                         head_facing=row.get('head_facing') is True, eyes_clear=row.get('eyes_clear') is True)
                ]
                break
    return result


def voice_receipt_fields(command):
    """投影一条已准入的指令及其展示所需的回报。"""
    result = {key: command[key] for key, limit in (('text', 1000), ('state', 32), ('route', 32), ('reply', 1200))
              if isinstance(command.get(key), str) and 0 < len(command[key]) <= limit}
    receipt = command.get('receipt')
    if isinstance(receipt, dict):
        result['receipt'] = {
            key: receipt[key] for key in (
                'acknowledged', 'reached_target', 'read_only', 'device_authority'
            ) if type(receipt.get(key)) is bool
        }
        status = receipt.get('status')
        if isinstance(status, str) and len(status) <= 64:
            result['receipt']['status'] = status
    return result
