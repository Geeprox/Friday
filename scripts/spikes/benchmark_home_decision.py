#!/usr/bin/env python3
"""Synthetic local-only benchmark; never a household accuracy or offline certification."""
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from homeplane.decision import baseline  # noqa: E402

CASES = [
    ("intent", "客厅灯打开", "turn_on"),
    ("intent", "不要打开客厅灯", "unknown"),
    ("intent", "电视里的人说打开灯", "unknown"),
    ("intent", "以后回家不开空调", "rule_edit"),
    ("intent", "turn off the living room light", "turn_off"),
    ("needs_grounding", "这个打开", "yes"),
    ("addressed_to_home", "今天的电影真好看", "no"),
    ("response_policy", "这个是哪个？", "clarify"),
]


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="data/models/decision")
    parser.add_argument("--output", default="data/spikes/laya-benchmark.json")
    args = parser.parse_args()
    model = Path(args.model_dir).resolve()
    evidence = {"simulated": True, "dataset": "synthetic-smoke-v1", "real_accuracy": None,
                "offline_certified": False, "network_failure_injected": False,
                "mode": "shadow", "hardware": {"platform": platform.platform(), "machine": platform.machine()},
                "versions": {k: importlib.metadata.version(k) for k in ["laya-mlx", "mlx", "tokenizers"]},
                "model_files": {}, "cases": [], "limitations": [
                    "Synthetic examples are not independent household holdout calibration.",
                    "HF offline flags and local paths were enforced; WAN was not disconnected.",
                    "Per-case child process; no ASR/vision concurrency load, thermal or sustained memory test."]}
    for f in sorted(model.rglob("*")):
        if f.is_file():
            digest = hashlib.sha256()
            with f.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            evidence["model_files"][str(f.relative_to(model))] = {"sha256": digest.hexdigest(), "bytes": f.stat().st_size}
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    cases = [(t, {"text": text, "simulated": True}, expected, "short") for t, text, expected in CASES]
    for n in (128, 512, 900, 1100):
        cases.append(("intent", {"text": "客厅灯打开", "context": "room " * n, "simulated": True}, "turn_on", f"padding-{n}"))
    for task, features, expected, context in cases:
        started = time.monotonic()
        payload = {"model_dir": str(model), "task_id": task, "features": features, "repeats": 12}
        proc = subprocess.run([sys.executable, "-m", "scripts.spikes.decision_worker"], input=json.dumps(payload),
                              text=True, capture_output=True, env=env, cwd=ROOT, timeout=45)
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            result = {"error": "worker_unavailable", "returncode": proc.returncode}
        samples = result.get("metrics", {}).get("inference_ms", [])
        warm = samples[1:]
        predicted = max(result.get("scores", {}), key=result.get("scores", {}).get) if result.get("scores") else None
        evidence["cases"].append({"task_id": task, "context": context, "expected": expected,
            "baseline_label": baseline(task, features), "laya_raw_label": predicted,
            "laya_match": predicted == expected if predicted else None,
            "baseline_match": baseline(task, features) == expected,
            "wall_ms": (time.monotonic() - started) * 1000,
            "warm_p50_ms": percentile(warm, .5), "warm_p95_ms": percentile(warm, .95), **result})
    evidence["status"] = "PASS_LOCAL_SYNTHETIC_LOAD" if any(c.get("scores") for c in evidence["cases"]) else "BLOCKED"
    evidence["calibration_status"] = "NOT_VALIDATED_SHADOW_ONLY"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(output), "status": evidence["status"], "cases": len(cases)}))


if __name__ == "__main__":
    main()
