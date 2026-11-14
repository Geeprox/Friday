"""把复核进度及受限原因归纳为首页状态。"""


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
    if error in {'daily_visual_review_reached', 'daily_requests_reached', 'daily_text_tokens_reached',
                 'api_quota_exhausted'}:
        return 'quota_limited'
    if error in {'api_timeout', 'api_rate_limited', 'api_unavailable'}:
        return error
    if error in {'api_key_rejected', 'api_model_unavailable', 'api_request_rejected'}:
        return 'unavailable'
    if error:
        return 'recovering'
    return status['progress']
