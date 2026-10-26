"""Production cache/lifecycle paths with explicitly synthetic worker artifacts."""
import hashlib
import importlib
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

from homeplane import ktv_accompaniment as audio


def wav(path, value=100):
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(int(value).to_bytes(2, "little", signed=True) * 800)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "ROOT", tmp_path)
    source = tmp_path / "private-song-title.wav"
    wav(source)
    runtime = tmp_path / "fake-runtime"
    runtime.write_text("explicit synthetic worker substitute")
    # Tests replace immutable expected pins, never bypass model verification
    # based on a public simulated flag.
    assets = {"config.json": b'{"synthetic":true}', "model.safetensors": b"synthetic weights, never loaded"}
    monkeypatch.setattr(audio, "PINNED_MODEL_ASSETS", {
        name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in assets.items()
    })
    selected = tmp_path / "cache" / "models" / audio.MODEL
    selected.mkdir(parents=True, mode=0o700)
    (tmp_path / "cache").chmod(0o700)
    (tmp_path / "cache" / "models").chmod(0o700)
    for name, data in assets.items():
        (selected / name).write_bytes(data)
    return source, tmp_path / "cache", tmp_path / "report.json", runtime


def fake_worker(request, runtime, cancel, timeout):
    payload = json.loads(request.read_text())
    output, models = Path(payload["output"]), Path(payload["model_dir"])
    selected = models / audio.MODEL
    selected.mkdir(exist_ok=True)
    (selected / "model.safetensors").write_bytes(b"synthetic weights, never loaded")
    (selected / "config.json").write_text('{"synthetic":true}')
    wav(output / "instrumental.wav", 50)
    wav(output / "vocals.wav", 25)
    audio.write_report(output / "worker-result.json", {"code": "none", "instrumental": "instrumental.wav",
                                                     "vocals": "vocals.wav", "duration_seconds": .1,
                                                     "sample_rate_hz": 8000})


def test_ready_manifest_cache_and_tamper_invalidation(environment, monkeypatch):
    source, cache, report, runtime = environment
    calls = []
    def run(*args):
        calls.append(1)
        fake_worker(*args)
    monkeypatch.setattr(audio, "_run_worker", run)
    ready = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert ready["status"] == "ready" and ready["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert ready["logging_available"] and not ready["audio_uploaded"]
    assert Path(ready["instrumental_path"]).stat().st_mode & 0o777 == 0o600
    hit = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert hit["cache_hit"] and len(calls) == 1
    Path(ready["instrumental_path"]).write_bytes(b"tampered")
    replaced = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert replaced["status"] == "ready" and not replaced["cache_hit"] and len(calls) == 2
    assert list((cache / "prepared").glob("*.replaced-*"))
    (cache / "models" / audio.MODEL / "model.safetensors").write_bytes(b"changed weights")
    refreshed = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert refreshed["status"] == "unavailable" and refreshed["code"] == "model_identity_mismatch"
    assert refreshed["model_verified"] is False and len(calls) == 2
    logs = b"".join(p.read_bytes() for p in (cache / "logs").glob("*.jsonl"))
    assert b"private-song-title" not in logs and str(source).encode() not in logs


@pytest.mark.parametrize("filename", ["model.safetensors", "config.json"])
def test_changed_model_rejected_before_inference_with_fresh_song_cache(environment, monkeypatch, filename):
    source, cache, report, runtime = environment
    path = cache / "models" / audio.MODEL / filename
    before = path.read_bytes()
    # Equal-size corruption must be caught by the pinned digest, not size or
    # comparison against an existing prepared-song manifest.
    path.write_bytes(bytes([before[0] ^ 1]) + before[1:])
    monkeypatch.setattr(audio, "_run_worker", lambda *_: pytest.fail("changed model must not infer"))
    result = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert result["code"] == "model_identity_mismatch" and result["status"] == "unavailable"
    assert result["instrumental_path"] is None and result["model_verified"] is False
    assert not (cache / "prepared").exists()


def test_missing_model_rejected_without_implicit_download(environment, monkeypatch):
    source, cache, report, runtime = environment
    (cache / "models" / audio.MODEL / "config.json").unlink()
    monkeypatch.setattr(audio, "_run_worker", lambda *_: pytest.fail("missing model must not load or download"))
    result = audio.prepare(source, cache, report, runtime=runtime)
    assert result["code"] == "model_unavailable" and result["instrumental_path"] is None


def test_model_change_during_inference_refuses_ready(environment, monkeypatch):
    source, cache, report, runtime = environment
    def changing(*args):
        fake_worker(*args)
        path = cache / "models" / audio.MODEL / "model.safetensors"
        data = path.read_bytes()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    monkeypatch.setattr(audio, "_run_worker", changing)
    result = audio.prepare(source, cache, report, runtime=runtime)
    assert result["code"] == "model_identity_mismatch" and result["instrumental_path"] is None
    assert not (cache / "prepared").exists()


def test_worker_reports_gpu_access_failure_without_native_import_retry(environment, monkeypatch):
    source, cache, report, runtime = environment
    request = report.with_name("worker-request.json")
    audio.write_report(request, {"source": str(source), "output": str(report.parent),
                                 "model_dir": str(cache / "models")})
    monkeypatch.setattr(importlib.metadata, "version", lambda _: audio.PACKAGE_VERSION)
    calls = []
    def unavailable(name):
        calls.append(name)
        raise ImportError("Metal device unavailable, private_path=/private/must-not-escape")
    monkeypatch.setattr(importlib, "import_module", unavailable)
    assert audio.worker(request) == 0
    result = json.loads((report.parent / "worker-result.json").read_text())
    assert result == {"code": "runtime_gpu_unavailable"}
    assert calls == ["mlx.core"]


def test_worker_reports_missing_dependency_as_runtime_failure(environment, monkeypatch):
    source, cache, report, runtime = environment
    request = report.with_name("worker-request.json")
    audio.write_report(request, {"source": str(source), "output": str(report.parent),
                                 "model_dir": str(cache / "models")})
    monkeypatch.setattr(importlib.metadata, "version", lambda _: audio.PACKAGE_VERSION)
    def unavailable(_):
        raise ImportError("private dependency location must not escape")
    monkeypatch.setattr(importlib, "import_module", unavailable)
    assert audio.worker(request) == 0
    assert json.loads((report.parent / "worker-result.json").read_text()) == {"code": "runtime_unavailable"}


def test_source_change_during_inference_refuses_ready(environment, monkeypatch):
    source, cache, report, runtime = environment
    def changing(*args):
        fake_worker(*args)
        wav(source, 200)
    monkeypatch.setattr(audio, "_run_worker", changing)
    result = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert result["status"] == "unavailable" and result["code"] == "source_changed"
    assert result["instrumental_path"] is None


@pytest.mark.parametrize("code", ["preparation_timeout", "preparation_cancelled", "worker_failed"])
def test_worker_failure_never_publishes_ready(environment, monkeypatch, code):
    source, cache, report, runtime = environment
    def fail(*_):
        raise audio.PreparationError(code)
    monkeypatch.setattr(audio, "_run_worker", fail)
    result = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert result["status"] == "unavailable" and result["code"] == code
    assert not list((cache / "jobs").iterdir()) and result["instrumental_path"] is None


def test_symlink_source_refused_and_library_not_started(environment, monkeypatch):
    source, cache, report, runtime = environment
    symlink = source.with_name("link.wav")
    symlink.symlink_to(source)
    monkeypatch.setattr(audio, "_run_worker", lambda *_: pytest.fail("must not infer"))
    result = audio.prepare(symlink, cache, report, runtime=runtime)
    assert result["code"] == "source_unavailable"


def test_terminate_kills_and_reaps_ignored_sigterm(tmp_path):
    process = subprocess.Popen(["/usr/bin/python3", "-c",
                                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"],
                               stdout=subprocess.PIPE, start_new_session=True)
    assert process.stdout.readline().strip() == b"ready"
    audio._terminate(process)
    assert process.returncode == -signal.SIGKILL


def test_pre_cancelled_job_does_not_start_inference(environment, monkeypatch):
    source, cache, report, runtime = environment
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(audio, "_run_worker", lambda *_: pytest.fail("must not infer"))
    result = audio.prepare(source, cache, report, runtime=runtime, cancel=cancel)
    assert result["code"] == "preparation_cancelled"


def test_logging_failure_visible_without_changing_ready(environment, monkeypatch):
    source, cache, report, runtime = environment
    monkeypatch.setattr(audio, "_run_worker", fake_worker)
    def fail_log(*_):
        raise OSError("private exception text must not escape")
    monkeypatch.setattr(audio.RuntimeLog, "_append", fail_log)
    result = audio.prepare(source, cache, report, runtime=runtime, simulated=True)
    assert result["status"] == "ready" and result["logging_available"] is False
    assert "private exception" not in report.read_text()


def test_actual_worker_poll_cancels_and_reaps_ignored_sigterm(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "ROOT", tmp_path)
    job = tmp_path / "job"
    job.mkdir()
    request = job / "request.json"
    request.write_text("{}")
    pid_file = tmp_path / "pid"
    runtime = tmp_path / "inert-worker"
    runtime.write_text(f"#!{sys.executable}\nimport os,signal,time\n"
                       "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                       f"open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
                       "time.sleep(30)\n")
    runtime.chmod(0o700)
    cancel = threading.Event()
    def cancel_when_ready():
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        cancel.set()
    trigger = threading.Thread(target=cancel_when_ready)
    trigger.start()
    try:
        with pytest.raises(audio.PreparationError, match="preparation_cancelled"):
            audio._run_worker(request, runtime, cancel, 5)
    finally:
        trigger.join(timeout=4)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
