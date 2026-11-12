"""把本机复核的近期结果和资源状态归纳为界面可读的状态。"""


def review_progress(windows, now, budget):
    if not windows:
        return 'waiting_input'
    for window in windows:
        last = window.last_result_at if window.last_result_at is not None else window.opened_at
        if now - last > budget:
            return 'delayed'
    return 'running' if all(window.last_result_at is not None for window in windows) else 'waiting'


def review_health(status):
    error = status.get('error')
    if not status['available'] or error == 'sound_labels_unavailable':
        return 'unavailable'
    if error in {'memory_capacity', 'memory_pressure', 'cpu_busy', 'sound_memory_pressure', 'sound_cpu_busy'}:
        return 'resource_limited'
    if error:
        return 'recovering'
    return status['progress']
