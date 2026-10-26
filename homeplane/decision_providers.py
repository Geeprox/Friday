"""Home task calibration gates; provider execution is exclusively delegated to ARL."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .decision import TASKS, CalibrationProfile, DecisionRequest, DecisionResult


def registry_descriptors():
    return [{"provider_id": name, "runtime": "arl", "availability": "contract_pending" if name == "laya_mlx" else "unavailable",
             "reason": reason, "automatic_fallback": False, "action_authority": False}
            for name, reason in (
                ("laya_mlx", "ARL currently exposes route classification, not calibrated home-task scores/token accounting"),
                ("jev", "ARL provider currently disabled; no Friday direct cloud adapter"),
                ("codex_app_server", "ARL provider currently disabled; no Friday app-server process"),
                ("openai_decision_api", "No verified ARL capability or official public contract"))]


class ARLDecisionProvider:
    """Formal FastDecisionEngine boundary. Never reinterpret routing confidence as task scores.

    ARL 0.1 exposes model.complete and a simple/complex/ignore decision only. Until a
    verified task-score/token-accounting operation exists, return unknown without inference.
    """
    def __init__(self, runtime, provider="laya_mlx", model="convaiinnovations/laya-multilingual"):
        self.runtime, self.provider, self.model = runtime, provider, model

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        import hashlib
        import json

        result = DecisionResult(task_id=request.task_id, task_version=request.task_version, model=self.model)
        result.metrics = {"runtime": "arl", "provider_id": self.provider, "inference_attempted": False}
        if not math.isfinite(request.deadline) or time.monotonic() >= request.deadline:
            result.unknown_reason = "expired"
            return result
        if request.task_id not in TASKS or request.task_version != "1":
            result.unknown_reason = "unsupported_task_version"
            return result
        try:
            payload = json.dumps(request.compact_features, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            result.input_hash = hashlib.sha256(payload).hexdigest()
        except (TypeError, ValueError):
            result.unknown_reason = "invalid_input"
            return result
        # Current ARL model input has a 1024-byte cap. This is not a tokenizer estimate.
        if len(payload) > 1024:
            result.unknown_reason = "context_overflow"
            return result
        report = await self.runtime.status(timeout=min(3, max(.001, request.deadline-time.monotonic())))
        if time.monotonic() >= request.deadline:
            result.unknown_reason = "expired"
        elif report.get("ready") is not True:
            result.unknown_reason = "arl_unavailable"
        else:
            result.unknown_reason = "arl_task_decision_contract_unavailable"
        return result

@dataclass(frozen=True)
class ProviderTaskCalibration:
    provider_id: str
    model: str
    task_version: str
    profile: CalibrationProfile


class ConservativeDecisionGate:
    """Reusable score/identity/calibration gate. Labels never authorize a device action."""
    def __init__(self, provider_id: str, model: str, profiles: dict[str, ProviderTaskCalibration] | None = None):
        self.provider_id, self.model = provider_id, model
        self.profiles = profiles or {}

    def check_profile(self, request: DecisionRequest) -> str | None:
        entry = self.profiles.get(request.task_id)
        if entry and (entry.provider_id != self.provider_id or entry.model != self.model or
                      entry.task_version != request.task_version or entry.profile.task_id != request.task_id):
            return "calibration_scope_mismatch"
        return None

    def apply(self, request: DecisionRequest, scores: dict, result: DecisionResult) -> DecisionResult:
        result.shadow, result.actionable, result.abstained = True, False, True
        result.label, result.unknown_reason = "unknown", None
        result.scores, result.calibrated_scores = {}, {}
        if request.task_id not in TASKS or request.task_version != "1":
            result.unknown_reason = "unsupported_task_version"
            return result
        if time.monotonic() >= request.deadline:
            result.unknown_reason = "expired"
            return result
        reason = self.check_profile(request)
        if reason:
            result.unknown_reason = reason
            return result
        labels = TASKS[request.task_id][1]
        if (not isinstance(scores, dict) or set(scores) != set(labels) or
                any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in scores.values()) or not .999 <= sum(scores.values()) <= 1.001):
            result.unknown_reason = "invalid_scores"
            return result
        result.scores = dict(scores)
        entry = self.profiles.get(request.task_id)
        profile = entry.profile if entry else CalibrationProfile(request.task_id)
        result.calibration_version = profile.version
        scaled = {label: max(score, 1e-12) ** (1 / profile.temperature) for label, score in scores.items()}
        total = sum(scaled.values())
        result.calibrated_scores = {label: score / total for label, score in scaled.items()}
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
        # Explicitly reassert invariants even when a caller provided a mutable result.
        result.shadow, result.actionable = True, False
        return result
