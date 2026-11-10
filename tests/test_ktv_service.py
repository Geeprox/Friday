from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
import wave

import pytest
from fastapi.testclient import TestClient

from homeplane.ktv_service import JobRequest, Queue, create_app, local_host, transcode_pcm

KEY = "s" * 43
HEADERS = {"X-KTV-Key": KEY}


def wav(path, frames=4800):
    with wave.open(str(path), "wb") as writer:
        writer.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0\0\1\0" * frames)


def wait(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get("/v1/jobs/" + job_id, headers=HEADERS).json()
        if result["state"] in {"ready", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    pytest.fail("queue did not settle")


def request(song_id="123", request_id=None):
    return {"song_id": song_id, "title": "Synthetic song", "request_id": request_id or str(uuid.uuid4())}


@pytest.fixture
def service(tmp_path):
    source = tmp_path / "selected.wav"
    wav(source)
    calls = []

    def acquire(song_id, folder, cancel):
        calls.append(song_id)
        return source

    def transcode(source, output, cancel):
        shutil.copyfile(source, output)

    queue = Queue(
        tmp_path / "runtime", acquire=acquire, separate=lambda s, f, c: s, transcode=transcode, simulated=True
    )
    with TestClient(
        create_app(queue, KEY, lambda: [{"song_id": "123", "title": "Synthetic song"}])
    ) as client:
        yield client, queue, source, calls


def test_complete_request_and_authenticated_media_contract(service):
    client, queue, source, calls = service
    reply = client.post("/v1/jobs", json=request(), headers=HEADERS)
    assert reply.status_code == 202
    value = wait(client, reply.json()["id"])
    assert value["state"] == "ready" and value["simulated"] is True
    assert calls == ["123"]
    assert "source_path" not in str(value) and str(source) not in str(value)
    for kind in ("original", "instrumental"):
        item = value["media"][kind]
        response = client.get(item["url"], headers=HEADERS)
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
        assert len(response.content) == item["bytes"]
        assert item["sample_rate"] == 48000
        assert response.headers["cache-control"] == "no-store"
        partial = client.get(item["url"], headers={**HEADERS, "Range": "bytes=40-47"})
        assert partial.status_code == 206 and partial.content == response.content[40:48]
        suffix = client.get(item["url"], headers={**HEADERS, "Range": "bytes=-4"})
        assert suffix.content == response.content[-4:]
        head = client.head(item["url"], headers=HEADERS)
        assert not head.content and int(head.headers["content-length"]) == item["bytes"]
        assert client.get(item["url"], headers={**HEADERS, "Range": "bytes=0-1,3-4"}).status_code == 416
        assert client.get(item["url"], headers={**HEADERS, "Range": "bytes=99999999-"}).status_code == 416
        assert (
            client.get(item["url"], headers={**HEADERS, "Range": "bytes=0-1", "If-Range": "old"}).status_code
            == 200
        )
    assert client.get("/v1/catalog?q=Synthetic", headers=HEADERS).json()["songs"][0]["song_id"] == "123"


def test_direct_monitor_diagnostics_are_bounded_counters_only(service):
    client = service[0]
    payload = {"direct_monitor_result": 1, "direct_monitor_elapsed_ms": 15000,
               "direct_monitor_peak": 2621, "direct_monitor_output_type": 2,
               "direct_monitor_voice_frames": 720000, "direct_monitor_output_api": 1,
               "direct_monitor_output_only": 0, "direct_monitor_mode": 1,
               "direct_monitor_output_api_actual": 2, "direct_monitor_output_mmap": 1,
               "direct_monitor_output_timestamp_available": 1,
               "direct_monitor_output_timestamp_position": 710400,
               "direct_monitor_output_timestamp_age_us": 10000,
               "direct_monitor_output_pending_available": 1,
               "direct_monitor_output_pending_frames": 9120,
               "direct_monitor_input_timestamp_available": 0,
               "direct_monitor_input_timestamp_error": 895,
               "direct_monitor_snapshot_final": 1, "direct_monitor_mmap_policy_before": 2,
               "direct_monitor_mmap_policy_restored": 1,
               "direct_monitor_output_sample_rate_actual": 48000,
               "direct_monitor_input_sample_rate_actual": 96000,
               "direct_monitor_baseline_elapsed_ms": 1001,
               "direct_monitor_baseline_missing": 256,
               "direct_monitor_gate_failures": 256,
               "direct_monitor_steady_start_ms": 1002,
               "direct_monitor_input_observed_samples": 1440000,
               "direct_monitor_input_full_scale_samples": 320,
               "direct_monitor_output_pending_valid_count": 68,
               "direct_monitor_output_pending_min_us": 42000,
               "direct_monitor_output_pending_max_us": 47000,
               "direct_monitor_input_xrun_available": 0,
               "direct_monitor_output_xrun_available": 1,
               "direct_monitor_output_xrun_delta": 2,
               "direct_monitor_clock_span_ms": 13900,
               "direct_monitor_legacy_gain_would_limit_frames": 52}
    assert client.post("/v1/tv/diagnostics", headers=HEADERS, json=payload).status_code == 200
    result = client.get("/v1/tv/diagnostics", headers=HEADERS).json()
    assert all(result[key] == value for key, value in payload.items())
    for invalid in ({"direct_monitor_audio": [1, 2]}, {"direct_monitor_peak": 32768},
                    {"direct_monitor_elapsed_ms": 20001}, {"direct_monitor_result": 8},
                    {"direct_monitor_output_type": 8}, {"direct_monitor_peak": True},
                    {"direct_monitor_voice_frames": -1}, {"direct_monitor_output_api": 3},
                    {"direct_monitor_error_stream": 3}, {"direct_monitor_quiet": 2},
                    {"direct_monitor_output_only": 2}, {"direct_monitor_mode": 3},
                    {"direct_monitor_output_mmap": 3}, {"direct_monitor_output_timestamp_available": 2},
                    {"direct_monitor_output_pending_frames": -1}, {"direct_monitor_output_timestamp_future": 2},
                    {"direct_monitor_snapshot_elapsed_ms": 20001}, {"direct_monitor_mmap_policy_before": 4},
                    {"direct_monitor_baseline_elapsed_ms": 20001}, {"direct_monitor_gate_failures": 1024},
                    {"direct_monitor_input_sample_rate_actual": 384001},
                    {"direct_monitor_steady_start_ms": 20001}, {"direct_monitor_clock_span_ms": 20001},
                    {"direct_monitor_input_full_scale_samples": -1},
                    {"direct_monitor_input_xrun_available": 2},
                    {"direct_monitor_output_xrun_available": True},
                    {"direct_monitor_output_pending_max_us": 10**12 + 1},
                    {"direct_monitor_output_abs_sum": [1, 2, 3]},
                    {"direct_monitor_output_timestamp_position": "720000"}):
        assert client.post("/v1/tv/diagnostics", headers=HEADERS, json=invalid).status_code == 400


def test_auth_request_boundaries_and_idempotency(service):
    client, queue, source, calls = service
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status?token=" + KEY).status_code == 401
    assert client.get("/v1/status", headers={**HEADERS, "Origin": "https://example.com"}).status_code == 403
    assert client.post("/v1/jobs", content=b"x" * 4097, headers=HEADERS).status_code == 413
    payload = request()
    first = client.post("/v1/jobs", json=payload, headers=HEADERS).json()
    assert client.post("/v1/jobs", json=payload, headers=HEADERS).json()["id"] == first["id"]
    conflict = {**payload, "song_id": "124"}
    assert client.post("/v1/jobs", json=conflict, headers=HEADERS).status_code == 409
    for unsafe in (
        {**payload, "source_path": "/etc/passwd"},
        {**payload, "song_id": "../1"},
        {**payload, "request_id": "invalid"},
        {**payload, "song_id": 123},
    ):
        assert client.post("/v1/jobs", json=unsafe, headers=HEADERS).status_code == 422
    assert (
        client.post("/v1/jobs", json={**request(), "title": "bad\nname"}, headers=HEADERS).status_code == 400
    )


def test_duplex_diagnostics_keep_unknown_flags_and_counter_bounds(service):
    client = service[0]
    payload = {"duplex_attempted": True, "duplex_input_dropped_available": False,
               "duplex_open_code": 0, "duplex_start_code": 0,
               "duplex_input_raw_rate": 96000, "duplex_output_raw_rate": 48000,
               "duplex_output_mmap": 1, "duplex_input_mmap": 1,
               "duplex_input_callback_metrics_available": 0,
               "duplex_startup_drain_completed": 1, "duplex_schema_version": 1,
               "duplex_output_pending_raw_frames": 1920, "audio_mixed_peak": 0}
    assert client.post("/v1/tv/diagnostics", json=payload, headers=HEADERS).status_code == 200
    report = client.get("/v1/tv/diagnostics", headers=HEADERS).json()
    assert all(report[key] == item for key, item in payload.items())
    for invalid in ({"duplex_attempted": 1}, {"duplex_input_dropped_available": 0},
                    {"duplex_start_code": "native error"}, {"duplex_input_raw_rate": 384001},
                    {"duplex_output_mmap": 3}, {"duplex_startup_drain_completed": 2},
                    {"duplex_input_callback_metrics_available": True},
                    {"duplex_input_read_frames": -1}, {"duplex_input_state": 15},
                    {"duplex_output_pending_raw_frames": 10**12 + 1},
                    {"duplex_audio_samples": [0, 1]}, {"audio_mixed_peak": 32769},
                    {"audio_mixed_peak": True}, {"audio_mixed_peak": -1}):
        assert client.post("/v1/tv/diagnostics", json=invalid, headers=HEADERS).status_code == 400


def test_diagnostics_has_separate_bounded_body_budget(service):
    client = service[0]
    # A valid aggregate capability list larger than the general control budget.
    device = {"id": 1, "type": 22, "channels": [1] * 16,
              "vendor": 1, "product": 1, "interfaces": [1] * 16}
    payload = {"inputs": [device] * 16, "usb": [device] * 16,
               "direct_monitor_output_timestamp_available": 0}
    body = json.dumps(payload).encode()
    assert 4096 < len(body) < 24576
    headers = {**HEADERS, "Content-Type": "application/json"}
    assert client.post("/v1/tv/diagnostics", content=body, headers=headers).status_code == 200
    assert client.post("/v1/jobs", content=body, headers=headers).status_code == 413
    assert client.post("/v1/tv/diagnostics", content=b" " * 24577, headers=headers).status_code == 413
    assert client.post("/v1/tv/diagnostics", json={"raw_audio": [0]}, headers=headers).status_code == 400


def test_vendor_diagnostics_accept_only_scalar_operational_metadata(service):
    client = service[0]
    receipt = {"vendor_microphone_mode": True, "vendor_mic_enabled": True,
               "vendor_mic_permission": True, "vendor_mic_owned": True,
               "vendor_gain_setpoint": 20, "vendor_mic_start_calls": 1,
               "vendor_carrier_frames_written": 480000,
               "vendor_carrier_playback_head_frames": 476160,
               "software_voice_processing_active": False}
    assert client.post("/v1/tv/diagnostics", json=receipt, headers=HEADERS).status_code == 200
    observed = client.get("/v1/tv/diagnostics", headers=HEADERS).json()
    assert all(observed[key] == value for key, value in receipt.items())
    for invalid in ({"vendor_mic_raw_audio": [0]}, {"vendor_mic_state": {"pcm": []}},
                    {"vendor_carrier_frames_written": "480000"}, {"vendor_serial": "private"}):
        assert client.post("/v1/tv/diagnostics", json=invalid, headers=HEADERS).status_code == 400


def test_wet_diagnostics_keep_dry_path_and_physical_unknowns_separate(service):
    client = service[0]
    receipt = {"wet_active": True, "wet_ready": True, "wet_explicit_trial": True,
               "wet_requested_profile": 2, "wet_applied_profile": 2, "wet_pid": 1234,
               "wet_process_isolated": True, "wet_input_frames": 48000, "wet_output_frames": 47520,
               "wet_output_nonzero_samples": 2000, "wet_timestamp_available": False,
               "wet_input_timestamp_age_us": 0, "wet_cache_verified": False,
               "wet_capture_reopen_excluded": False, "wet_audition_verified": False,
               "software_voice_processing_active": True, "software_voice_processing_wet_only": True,
               "software_dry_voice_processing_active": False, "wet_reason_code": 0,
               "wet_native_hardware_rate": 96000, "wet_native_sample_rate": 48000,
               "wet_native_input_open_result_error": 899, "wet_native_fault": 6,
               "wet_cache_hook_requested": True, "wet_cache_enable_calls": 1,
               "wet_cache_disable_calls": 0, "wet_cache_hook_error": 0}
    assert client.post("/v1/tv/diagnostics", json=receipt, headers=HEADERS).status_code == 200
    observed = client.get("/v1/tv/diagnostics", headers=HEADERS).json()
    assert all(observed[key] == value for key, value in receipt.items())
    for invalid in ({"wet_raw_audio": [0]}, {"wet_reason_code": "upstream text"},
                    {"wet_requested_profile": 7}, {"wet_output_peak_pcm16": 32769},
                    {"wet_process_isolated": 1}, {"wet_input_frames": True},
                    {"wet_input_frames": -1}, {"wet_reason_code": 8},
                    {"wet_input_timestamp_age_us": 500}, {"wet_cache_hook_requested": 1},
                    {"wet_cache_hook_error": "vendor diagnostic text"}):
        assert client.post("/v1/tv/diagnostics", json=invalid, headers=HEADERS).status_code == 400


def test_software_effects_and_backend_selection_diagnostics_remain_typed(service):
    client = service[0]
    report = {"audio_backend_choice": 1, "audio_backend_selected": True,
              "audio_backend_selection_pending": False, "system_audio_contract_ready": False,
              "system_audio_silent_start": False, "system_audio_cache_only": False,
              "compat_effects_capable": True, "compat_effects_applied": False,
              "compat_effects_gated": True, "compat_voice_zero_gate": True,
              "compat_voice_gate_frames": 240, "wet_requested_profile": 6, "wet_applied_profile": 0}
    assert client.post("/v1/tv/diagnostics", json=report, headers=HEADERS).status_code == 200
    for key, value in (("audio_backend_choice", 3), ("audio_backend_selected", 1),
                       ("compat_effects_capable", "yes"), ("compat_voice_gate_frames", True)):
        assert client.post("/v1/tv/diagnostics", json={key: value}, headers=HEADERS).status_code == 400


def test_production_check_receipt_preserves_unknown_and_signed_drift(service):
    client = service[0]
    receipt = {"production_check_result": 1, "production_check_valid": 0,
               "production_check_quiet": 1, "production_check_backend": 2,
               "production_check_input_drop_available": 0, "production_check_input_dropped_frames": -1,
               "production_check_java_drop_available": 1, "production_check_java_drop_delta_frames": 0,
               "production_check_rate_ppm": -400, "production_check_native_12": -1,
               "production_check_native_38": 96000, "production_check_native_63": 1,
               "production_check_native_64": 20000, "production_check_native_84": 1,
               "production_check_native_88": 48000, "production_check_native_89": 1, "production_check_native_90": 1,
               "production_check_output_peak": 0, "production_check_stop_completed": 1,
               "production_check_duplex_attempted": 1, "production_check_duplex_open_code": 9001,
               "production_check_duplex_start_code": 0, "production_check_engine_end_code": 2}
    assert client.post("/v1/tv/diagnostics", json=receipt, headers=HEADERS).status_code == 200
    observed = client.get("/v1/tv/diagnostics", headers=HEADERS).json()
    assert all(observed[key] == item for key, item in receipt.items())
    for invalid in ({"production_check_native_91": 0}, {"production_check_raw_pcm": [1]},
                    {"production_check_engine_end_code": 11}, {"production_check_duplex_attempted": 2},
                    {"production_check_duplex_open_code": 100001},
                    {"production_check_rate_ppm": -1001}, {"production_check_rate_ppm": 1001},
                    {"production_check_valid": True}, {"production_check_valid": 2},
                    {"production_check_result": 8}, {"production_check_output_peak": 32769},
                    {"production_check_native_38": 384001}, {"production_check_native_63": 2},
                    {"production_check_native_01": -2}):
        assert client.post("/v1/tv/diagnostics", json=invalid, headers=HEADERS).status_code == 400


def test_tampered_ready_media_is_not_served(service):
    client, queue, source, calls = service
    value = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    path = queue.jobs_dir / value["id"] / "instrumental.wav"
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    assert client.get(value["media"]["instrumental"]["url"], headers=HEADERS).status_code == 409
    assert client.get("/v1/jobs/" + value["id"], headers=HEADERS).json()["state"] == "failed"


def test_symlink_media_fails_closed(service):
    client, queue, source, calls = service
    value = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    path = queue.jobs_dir / value["id"] / "original.wav"
    path.unlink()
    path.symlink_to(source)
    assert client.get(value["media"]["original"]["url"], headers=HEADERS).status_code == 409


def test_cancel_and_capacity_do_not_publish_late_audio(tmp_path):
    source = tmp_path / "source.wav"
    wav(source)
    started = threading.Event()

    def acquire(song_id, folder, cancel):
        started.set()
        cancel.wait(2)
        return source

    queue = Queue(
        tmp_path / "runtime",
        acquire=acquire,
        separate=lambda s, f, c: s,
        transcode=lambda s, o, c: shutil.copyfile(s, o),
        capacity=1,
    )
    with TestClient(create_app(queue, KEY)) as client:
        first = client.post("/v1/jobs", json=request(), headers=HEADERS).json()
        assert started.wait(1)
        assert client.post("/v1/jobs", json=request("124"), headers=HEADERS).status_code == 429
        client.post("/v1/jobs/" + first["id"] + "/cancel", headers=HEADERS)
        value = wait(client, first["id"])
        assert value["state"] == "cancelled" and not value["media"]
        assert not list((queue.jobs_dir / value["id"]).glob("*.wav"))


def test_restart_keeps_ready_and_marks_interrupted(service):
    client, queue, source, calls = service
    payload = request()
    value = wait(client, client.post("/v1/jobs", json=payload, headers=HEADERS).json()["id"])
    queue.close()
    next_queue = Queue(
        queue.root,
        acquire=lambda *a: pytest.fail("unexpected acquisition"),
        separate=lambda *a: None,
        transcode=lambda *a: None,
        simulated=True,
    )
    try:
        assert next_queue.get(value["id"])["state"] == "ready"
        assert next_queue.submit(JobRequest(**payload))["id"] == value["id"]
        stream, item = next_queue.media(value["id"], "original")
        stream.close()
    finally:
        next_queue.close()


def test_invalid_audio_never_becomes_ready(tmp_path):
    source = tmp_path / "source.wav"
    wav(source)

    def bad(source, output, cancel):
        output.write_bytes(b"RIFF" + b"\0" * 44)

    queue = Queue(tmp_path / "runtime", acquire=lambda *a: source, separate=lambda s, *a: s, transcode=bad)
    with TestClient(create_app(queue, KEY)) as client:
        value = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
        assert value["state"] == "failed" and not value["media"]


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "169.254.1.1", "example.com", "::1"])
def test_non_lan_binding_refused(host):
    with pytest.raises(ValueError):
        local_host(host)


def test_real_ffmpeg_transcode_contract_without_playback(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("optional ffmpeg unavailable")
    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setparams((1, 2, 44100, 0, "NONE", "none"))
        writer.writeframes(b"\0\0" * 4410)
    output = tmp_path / "canonical.wav"
    transcode_pcm(source, output, threading.Event(), ffmpeg=ffmpeg)
    with wave.open(str(output), "rb") as reader:
        assert reader.getparams()[:3] == (2, 2, 48000)
        assert reader.getnframes() == 4800
    assert output.stat().st_size == 19244
    assert output.read_bytes()[36:40] == b"data"


def test_strong_key_required(service):
    with pytest.raises(ValueError):
        create_app(service[1], "weak")


def test_remote_search_merges_local_and_reports_unavailability(service):
    client, queue, source, calls = service
    app = create_app(
        queue,
        KEY,
        lambda: [{"song_id": "123", "title": "Synthetic local"}],
        lambda q: [{"song_id": "123", "title": "Duplicate"}, {"song_id": "124", "title": "Synthetic remote"}],
    )
    with TestClient(app) as other:
        result = other.get("/v1/catalog?q=Synthetic", headers=HEADERS).json()
        assert result["search_status"] == "ready" and len(result["songs"]) == 2
        assert result["songs"][0]["title"] == "Synthetic local"


def test_storage_budget_refuses_work_without_acquisition(service, monkeypatch):
    from homeplane import ktv_service

    client, queue, source, calls = service
    monkeypatch.setattr(ktv_service, "MAX_STORAGE", 1)
    value = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    assert value["state"] == "failed" and value["code"] == "storage_full" and calls == []


def test_remove_only_own_terminal_artifacts_and_keep_source(service):
    client, queue, source, calls = service
    job = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    assert client.get("/v1/jobs/" + job["id"] + "/lyrics", headers=HEADERS).json()["status"] == "unavailable"
    removed = client.post("/v1/jobs/" + job["id"] + "/remove", json={}, headers=HEADERS)
    assert removed.json() == {"removed": True, "original_download_deleted": False}
    assert source.exists() and not (queue.jobs_dir / job["id"]).exists()
    assert client.get("/v1/jobs/" + job["id"], headers=HEADERS).status_code == 404


@pytest.mark.parametrize("origin", ["mv", "mv_direct", "song"])
def test_lyrics_do_not_claim_song_timeline_for_mv_audio(service, origin):
    client, queue, source, calls = service
    job = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    queue.jobs[job["id"]]["audio_origin"] = origin
    lyric_calls = []

    def lyrics(song_id):
        lyric_calls.append(song_id)
        return {"status": "ready", "lines": [{"start_ms": 0, "text": "Synthetic lyric"}]}

    with TestClient(create_app(queue, KEY, lyrics=lyrics)) as other:
        result = other.get("/v1/jobs/" + job["id"] + "/lyrics", headers=HEADERS).json()
    if origin == "song":
        assert lyric_calls == ["123"] and result["status"] == "ready"
    else:
        assert lyric_calls == [] and result == {"status": "unavailable", "lines": []}


@pytest.mark.parametrize("preference,origin,visible", [("auto", "mv", False), ("song", "mv", False),
                                                     ("auto", "song", True), ("mv", "mv", True),
                                                     ("mv_direct", "mv_direct", True), ("cloud", "cloud", True)])
def test_playback_listing_excludes_legacy_auto_mv_without_deleting_job(service, preference, origin, visible):
    client, queue, source, calls = service
    job = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    value = queue.jobs[job["id"]]
    value.update(source_preference=preference, audio_origin=origin)
    before = json.loads(json.dumps(value))
    delivered = client.get("/v1/jobs", headers=HEADERS).json()["jobs"]
    assert (job["id"] in {j["id"] for j in delivered}) is visible
    assert queue.jobs[job["id"]] == before
    assert client.get("/v1/jobs/" + job["id"], headers=HEADERS).json()["state"] == "ready"
    assert job["id"] in {j["id"] for j in queue.list()}
    assert (queue.jobs_dir / job["id"] / "original.wav").is_file()


def test_playback_listing_excludes_untagged_mv_timeline(service):
    client, queue, source, calls = service
    job = wait(client, client.post("/v1/jobs", json=request(), headers=HEADERS).json()["id"])
    queue.jobs[job["id"]]["video"] = {"method": "same_mv_audio"}
    assert client.get("/v1/jobs", headers=HEADERS).json()["jobs"] == []


def test_cancel_queued_then_remove_has_no_late_worker(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    source = tmp_path / "source.wav"
    wav(source)

    def acquire(song_id, folder, event):
        calls.append(song_id)
        started.set()
        release.wait(2)
        return source

    queue = Queue(
        tmp_path / "runtime",
        acquire=acquire,
        separate=lambda s, *a: s,
        transcode=lambda s, o, c: shutil.copyfile(s, o),
        download_workers=1,
    )
    try:
        first = queue.submit(JobRequest(**request("123")))
        assert started.wait(1)
        second = queue.submit(JobRequest(**request("124")))
        future = queue.futures[second["id"]]
        assert queue.cancel(second["id"])["state"] == "cancelled"
        assert future.cancelled()
        assert queue.remove(second["id"])["removed"] is True
        release.set()
        queue.futures[first["id"]].result(timeout=2)
        assert calls == ["123"]
    finally:
        release.set()
        queue.close()


def test_voice_protection_diagnostics_accept_only_bounded_scalars(service):
    client = service[0]
    receipt = {'voice_protection_ready': True, 'voice_protection_latched': False,
               'voice_feedback_frames': 48000, 'voice_feedback_sequence': 2,
               'voice_feedback_level': 2, 'voice_feedback_hz': 2000,
               'voice_feedback_peak': 15000, 'voice_feedback_clipped': 0,
               'voice_feedback_ready': 1, 'voice_feedback_latched': 0,
               'voice_input_discarded_frames': 256, 'voice_input_backlog_frames': 384,
               'voice_protection_gain_cap': 35, 'voice_protection_level': 2,
               'voice_protection_reason': 2, 'voice_protection_sequence': 2,
               'voice_requested_gain_setpoint': 70}
    assert client.post('/v1/tv/diagnostics', json=receipt, headers=HEADERS).status_code == 200
    observed = client.get('/v1/tv/diagnostics', headers=HEADERS).json()
    assert all(observed[k] == v for k, v in receipt.items())
    for invalid in ({'voice_pcm': [1, 2]}, {'voice_protection_ready': 1},
                    {'voice_feedback_hz': 12001}, {'voice_feedback_frames': True},
                    {'voice_feedback_peak': 32769}, {'voice_protection_gain_cap': 101},
                    {'voice_protection_level': 4}, {'voice_protection_reason': 'arbitrary error text'}):
        assert client.post('/v1/tv/diagnostics', json=invalid, headers=HEADERS).status_code == 400
