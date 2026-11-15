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
    primary_asr = LatencyHistogram()
    secondary_asr = LatencyHistogram()
    primary.record_ms(39)
    secondary.record_ms(129)
    primary_asr.record_ms(420)
    secondary_asr.record_ms(110)
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
            timings={'vision_request': secondary}, body_rescue_passes=11)),
        'mode_revision': 'secondary-mode', 'wake_motion_counts': WakeMotionCounts(),
        'process': SimpleNamespace(returncode=None),
    }
    scope = dict(
        time=SimpleNamespace(monotonic=lambda: 10), state=dict(status='live', processed_frames=2),
        last_frame=9.9, capture=capture, camera_sample=camera_sample, identities=SimpleNamespace(session='primary-run'),
        camera_planner=None, home=SimpleNamespace(vision_benchmark_voice=lambda: {},
            voice_timings={'asr_request': primary_asr}, wake_motion_counts=WakeMotionCounts()),
        wall=SimpleNamespace(cameras=cameras, preview_metrics=lambda camera, *, source_current: dict(
            preview_frames=9 if camera == chosen['id'] else 15, preview_fresh=source_current,
            capture_epoch=7 if source_current else None)),
        SELECTED=chosen, secondary_homes={cameras[1]['id']: SimpleNamespace(
            vision_benchmark_voice=lambda: {'voice_packets': 24}, voice_timings={'asr_request': secondary_asr})},
        vision_measurement=measurement,
        previews=SimpleNamespace(sessions={cameras[1]['id']: item}, status=lambda _: report,
                                 source_current=lambda _camera, process: process.returncode is None),
        games=SimpleNamespace(mode='home'), party_camera=SimpleNamespace(window=lambda _: (None, None, False)),
        practice=SimpleNamespace(active=lambda: None), SESSION='worker-synthetic',
    )
    actual_worker_functions({'vision_measurement_sample'}, scope)
    sample = scope['vision_measurement_sample']
    assert all('timings' not in lane for lane in sample()['cameras'])
    lanes = sample(include_timings=True)['cameras']
    primary.record_ms(1000)
    secondary_asr.record_ms(1000)
    assert [summarize_timing(lane['timings']['vision_request']) for lane in lanes] == [
        {'samples': 1, 'p95_upper_ms': 50, 'above_range': 0},
        {'samples': 1, 'p95_upper_ms': 150, 'above_range': 0},
    ]
    assert [summarize_timing(lane['timings']['asr_request'])['p95_upper_ms'] for lane in lanes] == [500, 150]
    assert all(summarize_timing(lane['timings']['asr_request'])['samples'] == 1 for lane in lanes)
    scope['last_frame'] = 7
    item['process'].returncode = 1
    primary_sample, secondary_sample = sample()['cameras']
    assert primary_sample['recognition'] == 'waiting' and primary_sample['capture_epoch'] == 7
    assert primary_sample['preview_fresh'] is True
    assert secondary_sample['capture_epoch'] is None and secondary_sample['preview_fresh'] is False
    assert [lane['preview_frames'] for lane in lanes] == [9, 15]
    assert [lane['voice'].get('voice_packets') for lane in lanes] == [None, 24]


class SyntheticPort:
    def __init__(self, clock, fault=None, *, capture_evidence=False):
        self.clock = clock
        self.fault = fault
        self.report = new_report()
        if capture_evidence:
            for row in self.report['cameras']:
                row.update(capture_epoch=1, preview_frames=0, preview_fresh=True)
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
        is_full_rate = self.owner.active()
        for row, histogram in zip(self.report['cameras'], self.timings):
            physical_slot = row['slot']
            row['inference'] += 4 if is_full_rate else 1
            row['skipped'] += 0 if is_full_rate else 2
            if 'capture_epoch' in row:
                row.update(preview_frames=row['preview_frames'] + 4, preview_fresh=True)
            if self.fault == 'recognition_stalls' and is_full_rate and physical_slot == 4:
                row.update(state='connecting', recognition='recovering')
            else:
                row['frames'] += 3
            row['latency_ms'] = 325 + physical_slot*20
            histogram.record_ms(230 if is_full_rate else 64)
            row['timings'] = {'vision_request': histogram.bins.copy()}
        if is_full_rate and self.clock.at - self.full_start >= 7:
            if self.fault == 'reconnected':
                self.report['cameras'][3]['session'] = 'different-session'
            if self.fault == 'timing_reset':
                self.report['cameras'][3]['timings']['vision_request'] = LatencyHistogram().bins
            if self.fault == 'capture_replaced':
                self.report['cameras'][3]['capture_epoch'] = 2
            if self.fault == 'source_retired':
                self.report['cameras'][3]['capture_epoch'] = None
            if self.fault == 'recognition_stalls' and self.clock.at - self.full_start == 7:
                self.report['cameras'][1]['preview_fresh'] = False
            self.report['cameras'][0]['voice']['wake_confirmed'] += 1
        return self.owner.status(deepcopy(self.report))

    def resources(self):
        return dict(available=True, sampled_at=str(self.clock.at), cpu_percent=58 if self.owner.active() else 33,
                    memory_used_bytes=16_000 if self.owner.active() else 12_000)


@pytest.mark.parametrize('fault, reason', [
    (None, None),
    ('reconnected', 'comparison_source_or_rate_changed'),
    ('timing_reset', 'measurement_counter_reset'),
    ('capture_replaced', 'comparison_source_or_rate_changed'),
    ('source_retired', 'four_live_cameras_required'),
])
def test_compare_script_pairs_same_four_streams_and_restores_on_source_change(monkeypatch, fault, reason):
    clock = SimpleNamespace(at=10)

    def sleep(seconds):
        clock.at += seconds

    monkeypatch.setattr(compare_vision, 'time', SimpleNamespace(monotonic=lambda: clock.at, sleep=sleep))
    port = SyntheticPort(clock, fault, capture_evidence=fault in {'capture_replaced', 'source_retired'})
    if fault:
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
        assert result['full_rate']['cameras'][0]['voice']['voice_packets'] is None
        assert result['full_rate']['cameras'][0]['preview_frames'] is None
        assert result['full_rate']['host_cpu_samples'] == result['adaptive']['host_cpu_samples'] == 4
        for mode, upper in [('adaptive', 75), ('full_rate', 300)]:
            for row in result[mode]['cameras']:
                assert row['timings']['vision_request'] == {'samples': 5, 'p95_upper_ms': upper, 'above_range': 0}
                assert row['sampled_frame_age_p95_ms'] == 325 + row['slot'] * 20
        assert 'synthetic-worker' not in str(result) and 'source-' not in str(result)
    assert port.writes[0]['full_rate_seconds'] == 24 and len(port.writes) == 2
    assert 'cancel' in port.writes[1] and not port.owner.active()


def test_same_capture_records_preview_gap_and_full_phase_recognition_stall(monkeypatch):
    clock = SimpleNamespace(at=10)

    def sleep(seconds):
        clock.at += seconds

    monkeypatch.setattr(compare_vision, 'time', SimpleNamespace(monotonic=lambda: clock.at, sleep=sleep))
    port = SyntheticPort(clock, 'recognition_stalls', capture_evidence=True)
    result = compare_vision.run(port, 10, compare=True)
    _, preview_gap, _, recognition_gap = result['full_rate']['cameras']
    assert preview_gap['preview_fresh_samples'] == 5 and preview_gap['recognition_samples']['running'] == 6
    assert recognition_gap['preview_frames'] == 20 and recognition_gap['recognized_frames'] == 0
    assert recognition_gap['preview_fresh_without_recognition_samples'] == 6
    assert recognition_gap['recognition_samples'] == {'running': 0, 'waiting': 0, 'recovering': 6}
    assert recognition_gap['sampled_frame_age_p95_ms'] is None
    assert result['restored_adaptive'] is True and not port.owner.active()


def test_full_rate_requires_current_previews_at_start():
    report = new_report()
    for camera in report['cameras']:
        camera.update(capture_epoch=1, preview_fresh=True)
    measurement = VisionBenchmark()
    scope = comparison_scope(report)
    report['cameras'][0]['preview_fresh'] = False
    assert comparison_scope(report) == scope
    with pytest.raises(ValueError, match='four_live_cameras_required'):
        measurement.begin(10, report, scope)
    assert not measurement.active()


def test_host_summary_excludes_pre_phase_and_cached_baselines_without_losing_preview_frames():
    rows = []
    for index, (sample_id, cpu) in enumerate([('stale', 100), ('mixed', 95), ('mixed', 95),
                                             ('in-phase-1', 25), ('in-phase-2', 35)]):
        worker = new_report(sample=index * 2)
        for camera in worker['cameras']:
            camera['frames'] += index
            camera.update(preview_frames=index * 5, preview_fresh=index != 2, body_rescue_passes=index * 2)
            camera['voice'].update(voice_packets=index * 3, voice_segments=index // 2, asr_results=index // 2)
        rows.append((worker, dict(available=True, sampled_at=sample_id, cpu_percent=cpu, memory_used_bytes=cpu * 100)))
    summary = compare_vision.summarize(rows)
    assert summary['host_cpu_percent_median'] == 30 and summary['host_cpu_samples'] == 2
    assert summary['host_memory_used_bytes_median'] == 3000 and summary['host_samples'] == 2
    for camera in summary['cameras']:
        assert (camera['preview_frames'], camera['preview_fresh_samples'], camera['recognized_frames']) == (20, 4, 4)
        assert camera['body_rescue_passes'] == 8
        assert (camera['voice']['voice_packets'], camera['voice']['voice_segments'], camera['voice']['asr_results']) == (12, 2, 2)
    with pytest.raises(ValueError, match='measurement_host_samples_unavailable'):
        compare_vision.summarize(rows[:-1])


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
