"""采样已运行的四路摄像头，可选短时对照全速。输出仅包含汇总数。"""

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import stat
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from homeplane.embodied.vision_benchmark import comparison_scope, valid_token

ENDPOINT = '/api/internal/vision-benchmark'
WAKE_COUNTS = ('asr_attempts', 'wake_confirmed', 'wake_motor_sent', 'wake_motor_acknowledged')
COUNTERS = ('inference', 'skipped', 'frames')


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalPort:
    def __init__(self, data, console_port):
        from homeplane.embodied.paths import configuration

        private = data / 'private'
        port = configuration(private / 'config.json')['viewer_port']
        if type(port) is not int or not 1 <= port <= 65535 or not 1 <= console_port <= 65535:
            raise ValueError('invalid_loopback_port')
        self.viewer = f'http://127.0.0.1:{port}'
        self.console = f'http://127.0.0.1:{console_port}'
        self.query = urlencode({'k': self.key(private / 'viewer-key')})
        self.headers = {'X-Friday-Worker': self.key(private / 'control-key')}

    @staticmethod
    def key(path):
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('measurement_control_key_unavailable')
            raw = stream.read(514)
        key = raw.strip()
        if len(raw) > 513 or not 32 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
            raise ValueError('measurement_control_key_unavailable')
        return key

    def read(self, url, headers, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers = headers | {'Content-Type': 'application/json'}
        request = Request(url, data=payload, headers=headers)
        try:
            with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=2) as response:
                raw = response.read(32769)
        except HTTPError as error:
            # 只输出状态码，避免含授权参数的 URL 或服务响应进入记录。
            code = error.code
            error.close()
            raise ValueError(f'loopback_http_{code}') from None
        if len(raw) > 32768:
            raise ValueError('measurement_response_too_large')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('measurement_response_invalid')
        return result

    def worker(self, body=None):
        return self.read(self.viewer + ENDPOINT + '?' + self.query, self.headers, body)

    def resources(self):
        return self.read(self.console + '/api/home/resources', {})


def summarize(rows):
    cameras = []
    for lane in zip(*(sample['cameras'] for sample, _ in rows)):
        start, end = lane[0], lane[-1]
        latency = sorted(row['latency_ms'] for row in lane if row['latency_ms'] is not None)
        cameras.append(dict(slot=end['slot'],
            inference_submitted=end['inference']-start['inference'],
            skipped_static=end['skipped']-start['skipped'],
            recognized_frames=end['frames']-start['frames'],
            quiet_samples=sum(row['quiet_ms'] is not None and row['quiet_ms'] >= 4500 for row in lane),
            sampled_frame_age_median_ms=statistics.median(latency) if latency else None,
            sampled_frame_age_p95_ms=latency[math.ceil(len(latency)*.95)-1] if latency else None,
            voice={key: end['voice'].get(key, 0)-start['voice'].get(key, 0) for key in WAKE_COUNTS}))
    hosts = {sample['sampled_at']: sample for _, sample in rows if sample.get('available')}
    cpu = [row['cpu_percent'] for row in hosts.values() if row.get('cpu_percent') is not None]
    memory = [row['memory_used_bytes'] for row in hosts.values() if row.get('memory_used_bytes') is not None]
    if len(cpu) < 2 or len(memory) < 2 or any(
            min(row['inference_submitted'], row['skipped_static'], row['recognized_frames'], *row['voice'].values()) < 0
            or row['recognized_frames'] == 0 for row in cameras):
        raise ValueError('measurement_discontinuous')
    return dict(seconds=round(rows[-1][0]['sampled_mono']-rows[0][0]['sampled_mono'], 2), samples=len(rows),
                host_cpu_percent_median=statistics.median(cpu), host_samples=len(hosts),
                host_memory_used_bytes_median=statistics.median(memory), host_memory_used_bytes_peak=max(memory),
                cameras=cameras)


def measure(port, pool, seconds, scope, *, full_rate):
    rows = []
    start = time.monotonic()
    for offset in range(0, seconds+1, 2):
        time.sleep(max(0, start+offset-time.monotonic()))
        host = pool.submit(port.resources)
        sample = port.worker()
        if comparison_scope(sample) != scope or sample['full_rate'] is not full_rate:
            raise ValueError('comparison_source_or_rate_changed')
        if rows:
            if sample['sampled_mono'] <= rows[-1][0]['sampled_mono']:
                raise ValueError('measurement_clock_discontinuous')
            for previous, row in zip(rows[-1][0]['cameras'], sample['cameras']):
                if (any(row[k] < previous[k] for k in COUNTERS)
                        or any(row['voice'].get(k, 0) < previous['voice'].get(k, 0) for k in WAKE_COUNTS)):
                    raise ValueError('measurement_counter_reset')
        rows.append((sample, host.result()))
    return summarize(rows)


def run(port, seconds, compare):
    sample = port.worker()
    scope = comparison_scope(sample)
    if sample['full_rate']:
        raise ValueError('vision_benchmark_busy')
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        print('正在采样按活动降频的四路画面。', file=sys.stderr, flush=True)
        adaptive = measure(port, pool, seconds, scope, full_rate=False)
        result = {'type': 'observational', 'adaptive': adaptive, 'physical_movement_verified': False}
        if not compare:
            return result
        token = port.worker({'full_rate_seconds': seconds+14, 'scope': scope}).get('token')
        if not valid_token(token):
            raise ValueError('benchmark_token_unconfirmed')
        try:
            print('正在采样同四路的全速推理。', file=sys.stderr, flush=True)
            # 等待完整的两个资源采样间隔，避免把前一阶段计入全速 CPU。
            time.sleep(5)
            result['full_rate'] = measure(port, pool, seconds, scope, full_rate=True)
        finally:
            restored = port.worker({'cancel': token})
            if restored.get('full_rate') is not False or port.worker().get('full_rate') is not False:
                raise ValueError('benchmark_cancellation_unconfirmed')
        result.update(type='paired_live_camera_comparison', restored_adaptive=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_data = Path.home() / 'Library/Application Support/Friday/data/embodied'
    parser.add_argument('--data', type=Path, default=Path(os.environ.get('FRIDAY_EMBODIED_DATA', default_data)))
    parser.add_argument('--console-port', type=int, default=8740)
    parser.add_argument('--seconds', type=int, choices=range(10, 59, 2), default=24, metavar='10–58 偶数')
    parser.add_argument('--compare', action='store_true', help='在同一四路上临时启用全速并恢复')
    args = parser.parse_args()
    try:
        result = run(LocalPort(args.data, args.console_port), args.seconds, args.compare)
    except (KeyError, OSError, TypeError, ValueError) as error:
        reason = str(error) if type(error) is ValueError and str(error).replace('_','').isalnum() else 'measurement_unavailable'
        print(json.dumps({'status': 'unavailable', 'reason': reason}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
