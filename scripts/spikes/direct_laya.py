"""Bounded, shadow-only local semantic decisions. Never grants device authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

TASKS = {
    "addressed_to_home": ("Is this utterance addressed to the home assistant?", ("yes", "no", "unknown")),
    "intent": ("Classify the user's home intent, preserving negation.",
               ("turn_on", "turn_off", "adjust", "query", "rule_edit", "other", "unknown")),
    "needs_grounding": ("Does the utterance need a visual or conversational target?", ("yes", "no", "unknown")),
    "response_policy": ("Choose a considerate response policy.",
                        ("silent", "acknowledge", "clarify", "notify", "unknown")),
}


@dataclass(frozen=True)
class DecisionRequest:
    task_id: str
    compact_features: dict
    deadline: float  # time.monotonic(), not wall time
    context_snapshot_ref: str = "simulated"
    task_version: str = "1"


@dataclass(frozen=True)
class CalibrationProfile:
    task_id: str
    version: str = "uncalibrated-shadow-v1"
    temperature: float = 1.0
    minimum_score: float = .9
    minimum_margin: float = .2
    holdout_validated: bool = False

    def __post_init__(self):
        if (not math.isfinite(self.temperature) or self.temperature < 1 or
                not .9 <= self.minimum_score <= 1 or not .2 <= self.minimum_margin <= 1):
            raise ValueError("Calibration cannot sharpen scores or lower initial safety floors")


@dataclass
class DecisionResult:
    task_id: str
    label: str = "unknown"
    scores: dict[str, float] = field(default_factory=dict)
    calibrated_scores: dict[str, float] = field(default_factory=dict)
    abstained: bool = True
    unknown_reason: str | None = None
    calibration_version: str = "uncalibrated-shadow-v1"
    model: str = "laya-multilingual"
    model_fingerprint: str | None = None
    task_version: str = "1"
    input_version: str = "compact-features-v1"
    input_hash: str = ""
    token_count: int | None = None
    token_budget: int = 1024
    latency_ms: float = 0
    shadow: bool = True
    actionable: bool = False
    metrics: dict = field(default_factory=dict)


class FastDecisionEngine(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionResult: ...


def baseline(task_id: str, features: dict) -> str:
    """Conservative exact grammar baseline; labels alone never trigger control."""
    text = features.get("text", "").strip().lower()
    explicit = {"客厅灯打开": "turn_on", "turn on the living room light": "turn_on",
                "客厅灯关闭": "turn_off", "turn off the living room light": "turn_off",
                "亮一点": "adjust", "以后回家不开空调": "rule_edit"}
    if task_id == "intent":
        return explicit.get(text, "unknown")
    if task_id == "needs_grounding":
        return "yes" if text in {"这个打开", "亮一点"} else "no" if text in explicit else "unknown"
    if task_id == "addressed_to_home":
        return "yes" if text in explicit else "unknown"
    return "unknown"


def inspect_input(model_dir: Path, request: DecisionRequest) -> tuple[int, dict]:
    """Count actual checkpoint tokens including full question/options, without truncation."""
    from tokenizers import Tokenizer
    if request.task_id not in TASKS or request.task_version != "1":
        raise ValueError("unsupported_task")
    cfg = json.loads((model_dir / "rl_agent_config.json").read_text())
    if cfg.get("max_len") != 1024:
        raise ValueError("unverified_runtime_budget")
    tok = Tokenizer.from_file(str(model_dir / "tokenizer/tokenizer.json"))
    tok.no_padding()
    tok.no_truncation()
    tok_cfg = json.loads((model_dir / "tokenizer/tokenizer_config.json").read_text())
    mask = tok_cfg["mask_token"]
    mask = mask["content"] if isinstance(mask, dict) else mask
    instruction, labels = TASKS[request.task_id]
    def count(value):
        return len(tok.encode(value, add_special_tokens=False).ids)
    head = count("choice question: " + instruction)
    options = [1 + count(" " + label) for label in labels]
    # Upstream truncates options to 48 tokens and head to fit head_max_len. Refuse instead.
    if any(n > 49 for n in options) or head + sum(options) > cfg["head_max_len"]:
        raise ValueError("context_overflow")
    state = json.dumps(request.compact_features, ensure_ascii=False)
    if mask in state:
        raise ValueError("reserved_token_in_input")
    total = 4 + head + sum(options) + count(state)
    if total > 1024:
        raise ValueError("context_overflow")
    return total, {request.task_id: {"type": "choice", "instructions": instruction, "criteria": list(labels)}}


async def exchange_bounded(proc, payload: bytes, limit: int = 65536) -> bytes:
    proc.stdin.write(payload)
    await proc.stdin.drain()
    proc.stdin.close()
    output = bytearray()
    while True:
        chunk = await proc.stdout.read(min(8192, limit + 1 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > limit:
            raise ValueError("worker_output_overflow")
    await proc.wait()
    return bytes(output)


class LayaMLXDecisionProvider:
    """Per-request isolated worker, no credentials, local files only, deadline kills child.

    Cold process is intentional for first milestone; model residency optimization is pending.
    Shadow is hard-coded until independent task holdout approval exists outside this provider.
    """
    def __init__(self, model_dir: str | Path, *, profiles: dict | None = None,
                 worker_module: str = "scripts.spikes.decision_worker", timeout: float = 30):
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.profiles = profiles or {}
        self.worker_module = worker_module
        self.timeout = min(timeout, 60)
        self._gate = asyncio.Lock()

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        start = time.monotonic()
        result = DecisionResult(task_id=request.task_id, task_version=request.task_version)
        payload = json.dumps(request.compact_features, ensure_ascii=False, sort_keys=True)
        result.input_hash = hashlib.sha256(payload.encode()).hexdigest()
        profile = self.profiles.get(request.task_id, CalibrationProfile(request.task_id))
        result.calibration_version = profile.version
        if profile.task_id != request.task_id:
            result.unknown_reason = "calibration_task_mismatch"
            return result
        if request.deadline <= start:
            result.unknown_reason = "expired"
            return result
        if self._gate.locked():
            result.unknown_reason = "worker_busy"
            return result
        if len(payload.encode()) > 65536:
            result.unknown_reason = "context_overflow"
            return result
        required = ["model.safetensors", "rl_agent_config.json", "encoder/config.json",
                    "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json"]
        if not all((self.model_dir / f).is_file() for f in required):
            result.unknown_reason = "local_model_missing"
            return result
        async with self._gate:
            env = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR", "SYSTEMROOT") if key in os.environ}
            env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
            proc = None
            try:
                message = {"model_dir": str(self.model_dir), "task_id": request.task_id,
                           "task_version": request.task_version, "features": request.compact_features}
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", self.worker_module,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=env)
                remaining = min(self.timeout, request.deadline - time.monotonic())
                stdout = await asyncio.wait_for(exchange_bounded(proc, json.dumps(message).encode()), timeout=max(.001, remaining))
                if time.monotonic() >= request.deadline:
                    result.unknown_reason = "expired"
                elif proc.returncode:
                    result.unknown_reason = "worker_unavailable"
                else:
                    data = json.loads(stdout)
                    result.token_count = data.get("token_count")
                    result.metrics = data.get("metrics", {})
                    result.model_fingerprint = data.get("model_fingerprint")
                    result.unknown_reason = data.get("error")
                    if not result.unknown_reason:
                        if not isinstance(result.token_count, int) or not 0 < result.token_count <= 1024:
                            raise ValueError("invalid_token_count")
                        scores = data["scores"]
                        labels = TASKS[request.task_id][1]
                        if (set(scores) != set(labels) or any(not isinstance(v, (float, int)) or
                                not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()) or
                                not .99 <= sum(scores.values()) <= 1.01):
                            raise ValueError("invalid_scores")
                        result.scores = scores
                        scaled = {k: max(v, 1e-12) ** (1 / profile.temperature) for k, v in scores.items()}
                        total = sum(scaled.values())
                        result.calibrated_scores = {k: v / total for k, v in scaled.items()}
                        ranking = sorted(result.calibrated_scores, key=result.calibrated_scores.get, reverse=True)
                        winner = ranking[0]
                        result.metrics["shadow_prediction"] = winner
                        if not profile.holdout_validated:
                            result.unknown_reason = "uncalibrated_task"
                        elif (winner == "unknown" or result.calibrated_scores[winner] < profile.minimum_score or
                              result.calibrated_scores[winner] - result.calibrated_scores[ranking[1]] < profile.minimum_margin):
                            result.unknown_reason = "insufficient_evidence"
                        else:
                            result.label, result.abstained = winner, False
            except asyncio.TimeoutError:
                result.unknown_reason = "expired" if time.monotonic() >= request.deadline else "worker_timeout"
            except asyncio.CancelledError:
                raise
            except (ValueError, KeyError, TypeError, OSError):
                result.unknown_reason = "worker_protocol_error"
            finally:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                result.latency_ms = round((time.monotonic() - start) * 1000, 3)
        return result


def result_dict(result: DecisionResult) -> dict:
    return asdict(result)
