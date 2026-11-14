"""对照只改变已运行四路的推理频率，回执不含家庭数据。"""

from copy import deepcopy
import secrets
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
import httpx
import pytest

from homeplane.embodied.vision_benchmark import (
    LatencyHistogram, VisionBenchmark, WakeMotionCounts, camera_sample, comparison_scope,
    summarize_timing, timing_deltas,
)
from scripts.embodied import compare_vision
from tests.embodied.test_camera_intent import actual_worker_functions


def new_report(sample=0):
    rows = [
        camera_sample(n, 'live', 'running', f'source-{n}', 'revision',
                      {'processed': 20, 'skipped_static': 12, 'quiet_ms': 6000},
                      20, 400, {'asr_attempts': 0, 'wake_confirmed': 0})
        for n in range(1, 5)
    ]
    return dict(worker='synthetic-worker', sampled_mono=sample, cameras=rows, interactive_session=False)


@pytest.mark.asyncio
async def test_worker_endpoint_authority_and_full_rate_window(tmp_path):
    now = [30]
    report = new_report()
    measurement = VisionBenchmark(clock=lambda: now[0])

    space = dict(PRIVATE=tmp_path, secrets=secrets, vision_measurement=measurement, HTTPException=HTTPException,
                 vision_measurement_sample=lambda **_kwargs: report, valid_token=compare_vision.valid_token, Request=Request)
    actual_worker_functions({'vision_benchmark', 'vision_benchmark_status', 'benchmark_full_vision',
                             'identity_authorized'}, space)
    app = FastAPI()
    app.add_api_route(compare_vision.ENDPOINT, space['vision_benchmark_status'], methods=['GET'])
    app.add_api_route(compare_vision.ENDPOINT, space['vision_benchmark'], methods=['POST'])
    target = compare_vision.ENDPOINT
    start = dict(full_rate_seconds=10, scope=comparison_scope(report))
    key_file = tmp_path/'control-key'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        assert (await client.get(target)).status_code == 403
        assert (await client.post(target, json=start)).status_code == 403
        key_file.write_text('')
        assert (await client.get(target)).status_code == 403
        key_file.write_text('synthetic-worker-only-for-fixture')
        client.headers['X-Friday-Worker'] = key_file.read_text()
        assert (await client.get(target)).json()['full_rate'] is False
        assert (await client.post(target, json={'full_rate_seconds': 0})).status_code == 422
        report['cameras'][-1]['state'] = 'shutter_closed'
        blocked = await client.post(target, json=start)
        assert blocked.json()['detail'] == 'four_live_cameras_required'
        report['cameras'][-1]['state'] = 'live'
        token = (await client.post(target, json=start)).json()['token']
        assert space['benchmark_full_vision']() is True
        assert (await client.post(target, json=start)).status_code == 409
        assert (await client.post(target, json={'cancel': 'c'*32})).status_code == 409
        report['cameras'][-1]['session'] = 'replacement'
        assert space['benchmark_full_vision']() is False
        assert (await client.get(target)).json()['full_rate'] is False
        assert (await client.post(target, json=start)).json()['detail'] == 'vision_benchmark_source_changed'
        next_token = (await client.post(target, json={**start, 'scope': comparison_scope(report)})).json()['token']
        assert next_token != token
        assert (await client.post(target, json={'cancel': token})).status_code == 409
        now[0] += 10
        assert space['benchmark_full_vision']() is False
        assert (await client.post(target, json={'cancel': next_token})).json() == {'full_rate': False}


def test_worker_copies_separate_per_camera_timings_only_for_diagnostic_queries():
    measurement = VisionBenchmark()
    primary = LatencyHistogram()
    secondary = LatencyHistogram()
    primary.record_ms(39)
    secondary.record_ms(129)
    measurement.primary_timings = {'vision_request': primary}
    cameras = [{'id': 'synthetic-primary'}, {'id': 'synthetic-secondary'}]
    chosen = cameras[0]
    capture = SimpleNamespace(returncode=None)
    report = {
        'state': 'live', 'recognition': 'running', 'vision_cadence': {'processed': 2},
        'frames': 2, 'latency_ms': 300,
    }
    item = {
        'recognizer': SimpleNamespace(session='secondary-run', vision=SimpleNamespace(
            timings={'vision_request': secondary})),
        'mode_revision': 'secondary-mode', 'wake_motion_counts': WakeMotionCounts(),
    }
    scope = dict(
        time=SimpleNamespace(monotonic=lambda: 10), state=dict(status='live', processed_frames=2),
        last_frame=9.9, capture=capture, camera_sample=camera_sample, identities=SimpleNamespace(session='primary-run'),
        camera_planner=None, home=SimpleNamespace(vision_benchmark_voice=lambda: {}, wake_motion_counts=WakeMotionCounts()),
        wall=SimpleNamespace(cameras=cameras), SELECTED=chosen, secondary_homes={}, vision_measurement=measurement,
        previews=SimpleNamespace(sessions={cameras[1]['id']: item}, status=lambda _: report),
        games=SimpleNamespace(mode='home'), party_camera=SimpleNamespace(window=lambda _: (None, None, False)),
        practice=SimpleNamespace(active=lambda: None), SESSION='worker-synthetic',
    )
    actual_worker_functions({'vision_measurement_sample'}, scope)
    sample = scope['vision_measurement_sample']
    assert all('timings' not in lane for lane in sample()['cameras'])
    lanes = sample(include_timings=True)['cameras']
    primary.record_ms(1000)
    assert [summarize_timing(lane['timings']['vision_request']) for lane in lanes] == [
        {'samples': 1, 'p95_upper_ms': 50, 'above_range': 0},
        {'samples': 1, 'p95_upper_ms': 150, 'above_range': 0},
    ]


class SyntheticPort:
    def __init__(self, clock, fault=None):
        self.clock = clock
        self.fault = fault
        self.report = new_report()
        self.owner = VisionBenchmark(clock=lambda: clock.at)
        self.writes = []
        self.full_start = None
        self.timings = [LatencyHistogram() for _ in range(4)]
        for histogram in self.timings:
            histogram.record_ms(10000)

    def worker(self, body=None):
        if body:
            self.writes.append(body)
            if 'cancel' in body:
                self.owner.cancel(body['cancel'])
                return {'full_rate': False}
            self.full_start = self.clock.at
            return {'token': self.owner.begin(body['full_rate_seconds'], self.report, body['scope'])}
        self.report['sampled_mono'] = self.clock.at
        for row, histogram in zip(self.report['cameras'], self.timings):
            physical_slot = row['slot']
            row['inference'] += 4 if self.owner.active() else 1
            row['skipped'] += 0 if self.owner.active() else 2
            row['frames'] += 3
            row['latency_ms'] = 325 + physical_slot*20
            histogram.record_ms(230 if self.owner.active() else 64)
            row['timings'] = {'vision_request': histogram.bins.copy()}
        if self.owner.active() and self.clock.at - self.full_start >= 7:
            if self.fault == 'reconnected':
                self.report['cameras'][3]['session'] = 'different-session'
            if self.fault == 'timing_reset':
                self.report['cameras'][3]['timings']['vision_request'] = LatencyHistogram().bins
            self.report['cameras'][0]['voice']['wake_confirmed'] += 1
        return self.owner.status(deepcopy(self.report))

    def resources(self):
        return dict(available=True, sampled_at=str(self.clock.at), cpu_percent=58 if self.owner.active() else 33,
                    memory_used_bytes=16_000 if self.owner.active() else 12_000)


@pytest.mark.parametrize('fault', [None, 'reconnected', 'timing_reset'])
def test_compare_script_pairs_same_four_streams_and_restores_on_source_change(monkeypatch, fault):
    clock = SimpleNamespace(at=10)

    def sleep(seconds):
        clock.at += seconds

    monkeypatch.setattr(compare_vision, 'time', SimpleNamespace(monotonic=lambda: clock.at, sleep=sleep))
    port = SyntheticPort(clock, fault)
    if fault:
        reason = 'measurement_counter_reset' if fault == 'timing_reset' else 'comparison_source_or_rate_changed'
        with pytest.raises(ValueError, match=reason):
            compare_vision.run(port, 10, compare=True)
    else:
        result = compare_vision.run(port, 10, compare=True)
        assert result['type'] == 'paired_live_camera_comparison'
        assert result['physical_movement_verified'] is False
        assert result['adaptive']['host_cpu_percent_median'] == 33
        assert result['full_rate']['host_cpu_percent_median'] == 58
        assert [row['inference_submitted'] for row in result['adaptive']['cameras']] == [5]*4
        assert [row['inference_submitted'] for row in result['full_rate']['cameras']] == [20]*4
        assert result['full_rate']['cameras'][0]['voice']['wake_confirmed'] == 5
        for mode, upper in [('adaptive', 75), ('full_rate', 300)]:
            for row in result[mode]['cameras']:
                assert row['timings']['vision_request'] == {'samples': 5, 'p95_upper_ms': upper, 'above_range': 0}
                assert row['sampled_frame_age_p95_ms'] == 325 + row['slot'] * 20
        assert 'synthetic-worker' not in str(result) and 'source-' not in str(result)
    assert port.writes[0]['full_rate_seconds'] == 24 and len(port.writes) == 2
    assert 'cancel' in port.writes[1] and not port.owner.active()


def test_timing_bounds_and_legacy_worker_reports():
    assert timing_deltas(None, None) == {}
    timing = LatencyHistogram()
    for invalid in (None, True, float('inf'), float('nan'), -1, '100'):
        timing.record_ms(invalid)
    assert summarize_timing(timing.bins) == {'samples': 0, 'p95_upper_ms': None, 'above_range': 0}
    timing.record_ms(50001)
    assert summarize_timing(timing.bins) == {'samples': 1, 'p95_upper_ms': None, 'above_range': 1}
    with pytest.raises(ValueError, match='measurement_timing_changed'):
        timing_deltas(None, {'vision_request': timing.bins})


def test_wake_counts_require_actual_cue_and_do_not_claim_device_movement():
    from unittest.mock import Mock

    from homeplane.embodied.camera_audio import WakeCameraCue
    from homeplane.embodied.wake_diagnostics import record_wake_motor

    log = Mock()
    counts = WakeMotionCounts()
    cue = WakeCameraCue(5, 'synthetic', ('source', 'context'), 1, confirmed_mono=6.0)
    record_wake_motor(log, None, 'left', counts=counts)
    assert counts.public() == {'wake_motor_sent': 0, 'wake_motor_acknowledged': 0}
    record_wake_motor(log, cue, 'left', counts=counts, submitted_at=6.07)
    record_wake_motor(log, cue, 'left', result='unavailable', sent=True, counts=counts)
    assert counts.public() == {'wake_motor_sent': 1, 'wake_motor_acknowledged': 0}
    submitted = record_wake_motor(log, cue, 'right', counts=counts, submitted_at=6.12)
    record_wake_motor(log, cue, 'right', result='ok', counts=counts, submitted_at=submitted, acknowledged_at=6.165)
    assert counts.public() == {'wake_motor_sent': 2, 'wake_motor_acknowledged': 1}
    assert summarize_timing(counts.timings['wake_confirm_to_motor_submit'].bins)['samples'] == 2
    assert summarize_timing(counts.timings['motor_submit_to_ack'].bins) == {
        'samples': 1, 'p95_upper_ms': 50, 'above_range': 0,
    }
