"""Local-only worker. No HA, camera, shell tools or household credentials."""
import hashlib
import importlib.metadata
import json
import resource
import sys
import time
from pathlib import Path

from .direct_laya import DecisionRequest, inspect_input


def verify_model(model_dir):
    manifest = json.loads((Path(__file__).resolve().parents[2] / "homeplane/laya-model-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        digest = hashlib.sha256()
        with (model_dir / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("model_integrity_mismatch")
    return manifest["files"]["model.safetensors"]


def run(message):
    model_dir = Path(message["model_dir"])
    if importlib.metadata.version("laya-mlx") != "0.2.0":
        return {"error": "unverified_runtime_version"}
    try:
        fingerprint = verify_model(model_dir)
    except (ValueError, OSError):
        return {"error": "model_integrity_mismatch"}
    request = DecisionRequest(message["task_id"], message["features"], time.monotonic() + 60,
                              task_version=message.get("task_version", "1"))
    try:
        count, questions = inspect_input(model_dir, request)
    except ValueError as exc:
        return {"error": str(exc)}
    started = time.perf_counter()
    import laya_mlx
    import mlx.core as mx
    agent = laya_mlx.load(model_dir)
    load_ms = (time.perf_counter() - started) * 1000
    prepared, _ = agent.prepare(request.compact_features, questions)
    if len(prepared[0]["ids"]) != count:
        return {"error": "tokenizer_runtime_mismatch", "token_count": count}
    durations = []
    for _ in range(min(50, max(1, message.get("repeats", 1)))):
        tick = time.perf_counter()
        prediction = agent.system_one(request.compact_features, questions)
        durations.append((time.perf_counter() - tick) * 1000)
    return {"model_fingerprint": fingerprint, "scores": prediction["answers"][request.task_id]["probabilities"], "token_count": count,
            "metrics": {"load_ms": load_ms, "inference_ms": durations,
                        "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "mlx_peak_bytes": mx.get_peak_memory()}}


if __name__ == "__main__":
    try:
        message = json.loads(sys.stdin.buffer.read(65537))
        print(json.dumps(run(message)))
    except Exception as exc:
        # Error class is safe; native exceptions can include home input or local paths.
        print(json.dumps({"error": "worker_unavailable", "error_class": type(exc).__name__}))
