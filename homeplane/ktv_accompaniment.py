"""Explicit local-file accompaniment preparation; never captures a microphone.

The public report is a private local handoff to the listening UI. Operational
logs use Friday's bounded journal and contain no source/output paths or titles.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .runtime_log import RuntimeLog

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".artifacts/ktv-audio/separation/runtime/bin/python"
PACKAGE = "mlx-audio-separator"
PACKAGE_VERSION = "0.1.20"
MODEL = "mel-roformer-zfturbo-vocals-v1-mlx"
MODEL_REVISION = "01bef2daebedf39937f552d6da68340fc979c9a0"
PINNED_MODEL_ASSETS = {
    "config.json": {"bytes": 1016,
                    "sha256": "d110ed4713b403d27e2ae6c7796b1a864576232de4a2070e9762108ca197caa6"},
    "model.safetensors": {"bytes": 67402202,
                          "sha256": "ef4aa052845a868cfaff93611477bd8f54d8081bc32f2742a9b3c738f0821191"},
}
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION_SECONDS = 1800
SCHEMA = 1
CODES = frozenset({
    "none", "preparing", "cache_hit", "invalid_request", "output_outside_workspace",
    "unsafe_output", "source_unavailable", "source_too_large", "source_changed",
    "source_audio_unreadable", "source_duration_exceeded", "runtime_unavailable",
    "runtime_version_mismatch", "worker_failed", "model_or_separation_failed",
    "model_unavailable", "model_identity_mismatch", "runtime_gpu_unavailable",
    "output_unqualified", "preparation_timeout", "preparation_cancelled", "cache_busy",
    "logging_unavailable", "internal_error",
})


class PreparationError(ValueError):
    pass


def _hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise PreparationError("source_unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace(path: Path) -> Path:
    path = path.absolute()
    if not path.is_relative_to(ROOT) or path.resolve() != path:
        raise PreparationError("output_outside_workspace")
    return path


def _private_directory(path: Path) -> Path:
    path = _workspace(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PreparationError("unsafe_output")
    return path


def write_report(path: Path, value: dict) -> None:
    path = _workspace(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".ktv-report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as writer:
            json.dump(value, writer, ensure_ascii=False, indent=2, allow_nan=False)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _base(status: str, code: str, *, simulated: bool) -> dict:
    return {"schema_version": SCHEMA, "status": status, "code": code,
            "instrumental_path": None, "vocals_path": None, "source_sha256": None,
            "model": MODEL, "model_revision": MODEL_REVISION, "model_verified": False,
            "runtime_package": PACKAGE, "runtime_version": PACKAGE_VERSION,
            "simulated": simulated, "evidence_basis": "synthetic_input" if simulated else "selected_local_file",
            "processing_location": "local", "audio_uploaded": False, "microphone_captured": False}


def _stage_source(source: Path, job: Path, cancel: threading.Event) -> tuple[Path, str, tuple]:
    source = source.absolute()
    if source.is_symlink():
        raise PreparationError("source_unavailable")
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise PreparationError("source_unavailable") from error
    staged = job / ("selected-source" + source.suffix.lower())
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as reader, staged.open("xb") as writer:
        before = os.fstat(reader.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise PreparationError("source_unavailable")
        if not 1 <= before.st_size <= MAX_SOURCE_BYTES:
            raise PreparationError("source_too_large")
        total = 0
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            if cancel.is_set():
                raise PreparationError("preparation_cancelled")
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise PreparationError("source_too_large")
            writer.write(chunk)
            digest.update(chunk)
        after = os.fstat(reader.fileno())
    def signature(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if signature(before) != signature(after) or total != before.st_size:
        raise PreparationError("source_changed")
    os.chmod(staged, 0o400)
    return staged, digest.hexdigest(), signature(after)


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def _run_worker(request: Path, runtime: Path, cancel: threading.Event, timeout: float) -> None:
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT"}}
    temporary = request.parent / "tmp"
    temporary.mkdir(mode=0o700)
    environment.update({"TMPDIR": str(temporary), "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                        "HF_HOME": str(request.parent.parent.parent / "hf-cache"),
                        "XDG_CACHE_HOME": str(temporary / "cache"), "PYTHONUNBUFFERED": "1"})
    process = subprocess.Popen([str(runtime), str(ROOT / "scripts/ktv_prepare.py"),
                                "--worker-request", str(request)], cwd=ROOT, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if cancel.wait(0.1):
                raise PreparationError("preparation_cancelled")
            if time.monotonic() >= deadline:
                raise PreparationError("preparation_timeout")
        if process.returncode != 0:
            raise PreparationError("worker_failed")
    finally:
        _terminate(process)


def _artifact(path: Path, job: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.resolve().parent != job.resolve():
        raise PreparationError("output_unqualified")
    if path.suffix.lower() != ".wav" or not 44 <= path.stat().st_size <= MAX_OUTPUT_BYTES:
        raise PreparationError("output_unqualified")
    return {"filename": path.name, "sha256": _hash(path), "bytes": path.stat().st_size}


def _model_files(directory: Path) -> dict:
    result = {}
    selected = directory / MODEL
    if selected.is_symlink():
        raise PreparationError("model_identity_mismatch")
    if not selected.is_dir():
        raise PreparationError("model_unavailable")
    # Upstream's general download catalog may refresh independently. It is not
    # a model input and must not invalidate prepared songs for this pinned model.
    for name, expected in PINNED_MODEL_ASSETS.items():
        path = selected / name
        if path.is_symlink():
            raise PreparationError("model_identity_mismatch")
        if not path.is_file():
            raise PreparationError("model_unavailable")
        if path.stat().st_size != expected["bytes"]:
            raise PreparationError("model_identity_mismatch")
        digest = _hash(path)
        if digest != expected["sha256"]:
            raise PreparationError("model_identity_mismatch")
        result[str(path.relative_to(directory))] = digest
    return result


def _cached(cache: Path, key: str, source_hash: str, model_dir: Path, simulated: bool) -> dict | None:
    entry = cache / "prepared" / key
    try:
        manifest_path = entry / "manifest.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 65536:
            return None
        manifest = json.loads(manifest_path.read_text())
        model_binding = _model_files(model_dir)
        stored_binding = manifest.get("model_files", {})
        if (manifest.get("source_sha256") != source_hash or manifest.get("model") != MODEL
                or manifest.get("runtime_version") != PACKAGE_VERSION
                or manifest.get("simulated") is not simulated or not isinstance(stored_binding, dict)
                or any(stored_binding.get(name) != digest for name, digest in model_binding.items())):
            return None
        for stem in ("instrumental", "vocals"):
            item = manifest["artifacts"][stem]
            if _artifact(entry / item["filename"], entry) != item:
                return None
        result = _base("ready", "cache_hit", simulated=simulated)
        return {**result, "source_sha256": source_hash, "cache_key": key,
                "model_verified": True,
                "instrumental_path": str(entry / manifest["artifacts"]["instrumental"]["filename"]),
                "vocals_path": str(entry / manifest["artifacts"]["vocals"]["filename"]),
                "manifest_path": str(manifest_path), "cache_hit": True}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def prepare(source: Path, cache_dir: Path, report_path: Path, *, runtime: Path = RUNTIME,
            timeout: float = 1800, cancel: threading.Event | None = None, simulated: bool = False) -> dict:
    """Prepare one selected file. SIGTERM/SIGINT callers set cancel; no inference on import."""
    cancel = cancel if cancel is not None else threading.Event()
    result = _base("unavailable", "internal_error", simulated=simulated)
    started = time.monotonic()
    journal = None
    job = None
    lock = None
    try:
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise PreparationError("invalid_request")
        cache = _private_directory(cache_dir)
        report_path = _workspace(report_path)
        journal = RuntimeLog(cache / "logs")
        correlation = secrets.token_hex(16)
        journal.record("service.start", "start", correlation=correlation)
        if not runtime.is_file():
            raise PreparationError("runtime_unavailable")
        lock_path = cache / "prepare.lock"
        lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreparationError("cache_busy") from error
        jobs = _private_directory(cache / "jobs")
        job = Path(tempfile.mkdtemp(prefix="prepare-", dir=jobs))
        staged, source_hash, signature = _stage_source(source, job, cancel)
        result["source_sha256"] = source_hash
        model_dir = _private_directory(cache / "models")
        # Validate even for an empty/new song cache. A changed model must never
        # turn into a fresh inference while still claiming the fixed revision.
        model_binding = _model_files(model_dir)
        key = hashlib.sha256((source_hash + MODEL + PACKAGE_VERSION + str(simulated)).encode()).hexdigest()
        hit = _cached(cache, key, source_hash, model_dir, simulated)
        if hit is not None:
            result = hit
        else:
            result.update(status="preparing", code="preparing", cache_key=key)
            write_report(report_path, result)
            request = job / "request.json"
            write_report(request, {"source": str(staged), "output": str(job), "model_dir": str(model_dir)})
            _run_worker(request, runtime, cancel, timeout)
            outcome = json.loads((job / "worker-result.json").read_text())
            if outcome.get("code") != "none":
                code = outcome.get("code")
                raise PreparationError(code if code in CODES else "model_or_separation_failed")
            if cancel.is_set():
                raise PreparationError("preparation_cancelled")
            if _model_files(model_dir) != model_binding:
                raise PreparationError("model_identity_mismatch")
            artifacts = {stem: _artifact(job / outcome[stem], job) for stem in ("instrumental", "vocals")}
            entry = cache / "prepared" / key
            entry.parent.mkdir(mode=0o700, exist_ok=True)
            if entry.exists():
                # Preserve an altered cache as evidence, never silently overwrite it.
                entry.rename(entry.with_name(key + ".replaced-" + secrets.token_hex(4)))
            entry.mkdir(mode=0o700)
            for item in artifacts.values():
                (job / item["filename"]).rename(entry / item["filename"])
                os.chmod(entry / item["filename"], 0o600)
            manifest = {**_base("ready", "none", simulated=simulated), "source_sha256": source_hash,
                        "cache_key": key, "artifacts": artifacts, "model_files": model_binding,
                        "model_verified": True,
                        "duration_seconds": outcome["duration_seconds"], "sample_rate_hz": outcome["sample_rate_hz"]}
            write_report(entry / "manifest.json", manifest)
            result = {**manifest, "instrumental_path": str(entry / artifacts["instrumental"]["filename"]),
                      "vocals_path": str(entry / artifacts["vocals"]["filename"]),
                      "manifest_path": str(entry / "manifest.json"), "cache_hit": False}
        if cancel.is_set():
            raise PreparationError("preparation_cancelled")
        current = source.stat()
        now = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        if now != signature or _hash(source) != source_hash:
            raise PreparationError("source_changed")
    except PreparationError as error:
        result.update(status="unavailable", code=error.args[0] if error.args[0] in CODES else "internal_error")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result.update(status="unavailable", code="internal_error")
    finally:
        if lock is not None:
            os.close(lock)
        if job is not None:
            shutil.rmtree(job)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if result["status"] != "ready":
        result.update(instrumental_path=None, vocals_path=None, model_verified=False)
    if journal is not None:
        code = result["code"]
        journal.record("service.stop", "result", correlation=correlation,
                       result="ok" if result["status"] == "ready" else "cancelled" if code == "preparation_cancelled"
                       else "timeout" if code == "preparation_timeout" else "unavailable",
                       code="none" if result["status"] == "ready" else "flow_cancelled" if code == "preparation_cancelled"
                       else "deadline_exceeded" if code == "preparation_timeout" else "internal_error",
                       elapsed_ms=result["elapsed_seconds"] * 1000)
        result["logging_available"] = journal.disk_available is True
    else:
        result["logging_available"] = False
    write_report(report_path, result)
    return result


def _load_runtime():
    """Stop at the first native import failure; retrying a partial MLX import can abort."""
    from importlib import import_module

    try:
        import_module("mlx.core")
        audio = import_module("mlx_audio_io")
        separator = import_module("mlx_audio_separator")
        return audio.info, separator.Separator
    except (ImportError, OSError, RuntimeError) as error:
        # Inspect only for a fixed native failure signature. Never retain the
        # exception text, which can contain paths, in reports or shared logs.
        text = str(error).lower()
        code = "runtime_gpu_unavailable" if "metal" in text and "device" in text else "runtime_unavailable"
        raise PreparationError(code) from error


def worker(request_path: Path) -> int:
    """Isolated MLX worker. Raw library output is discarded by its owner."""
    outcome = {"code": "model_or_separation_failed"}
    output = request_path.parent
    try:
        from importlib.metadata import version

        if version(PACKAGE) != PACKAGE_VERSION:
            raise PreparationError("runtime_version_mismatch")
        request = json.loads(request_path.read_text())
        source, output, model_dir = (Path(request[name]) for name in ("source", "output", "model_dir"))
        pinned_before = _model_files(model_dir)
        info, Separator = _load_runtime()
        try:
            metadata = info(str(source))
            duration, rate = float(metadata.duration), int(metadata.sample_rate)
        except Exception as error:
            raise PreparationError("source_audio_unreadable") from error
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_SECONDS:
            raise PreparationError("source_duration_exceeded")
        if not 1 <= metadata.channels <= 2 or not 8000 <= rate <= 192000:
            raise PreparationError("source_audio_unreadable")
        separator = Separator(output_dir=str(output), model_file_dir=str(model_dir), output_format="WAV")
        separator.load_model(MODEL)
        if _model_files(model_dir) != pinned_before:
            raise PreparationError("model_identity_mismatch")
        files = separator.separate(str(source))
        if _model_files(model_dir) != pinned_before:
            raise PreparationError("model_identity_mismatch")
        stems = {}
        for filename in files:
            path = Path(filename)
            if not path.is_absolute():
                path = output / path
            lower = path.name.lower()
            if "instrumental" in lower:
                stems["instrumental"] = path.name
            elif "vocals" in lower:
                stems["vocals"] = path.name
        if set(stems) != {"instrumental", "vocals"}:
            raise PreparationError("output_unqualified")
        output_rates = []
        for filename in stems.values():
            stem_info = info(str(output / filename))
            if abs(float(stem_info.duration) - duration) > .25 or not 1 <= stem_info.channels <= 2:
                raise PreparationError("output_unqualified")
            output_rates.append(int(stem_info.sample_rate))
        if output_rates[0] != output_rates[1]:
            raise PreparationError("output_unqualified")
        outcome = {"code": "none", **stems, "duration_seconds": duration,
                   "source_sample_rate_hz": rate, "sample_rate_hz": output_rates[0]}
    except PreparationError as error:
        outcome = {"code": error.args[0] if error.args[0] in CODES else "model_or_separation_failed"}
    except Exception:
        outcome = {"code": "model_or_separation_failed"}
    write_report(output / "worker-result.json", outcome)
    return 0
