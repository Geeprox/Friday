"""Opt-in decision-provider contracts. No discovery of secrets and no automatic fallback.

Jev transport is contract-tested with synthetic mocks only. Default construction cannot
send data without an explicit immutable model and matching trusted token accountant.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from typing import Callable

import httpx

from homeplane.decision import TASKS, CalibrationProfile, DecisionRequest, DecisionResult

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    locality: str
    availability: str
    reason: str
    automatic_fallback: bool = False
    action_authority: bool = False


def registry_descriptors() -> list[dict]:
    return [asdict(item) for item in (
        ProviderDescriptor("laya_mlx", "local", "implemented", "Local runtime and pinned model required"),
        ProviderDescriptor("jev", "cloud", "mock_contract_verified",
                           "Requires explicit cloud opt-in, pinned model, credential and verified tokenizer"),
        ProviderDescriptor("openai_decision_api", "unknown", "unavailable",
                           "No verified official Decisions API contract; no endpoint implemented"),
    )]


@dataclass(frozen=True)
class TrustedTokenAccounting:
    """Trusted application dependency, never request-supplied token estimates.

    count_payload must implement the pinned model's exact full-input serialization,
    including state, task instructions, labels and provider-added special tokens.
    No production Jev tokenizer is shipped: absent accounting deliberately blocks upload.
    """
    model: str
    version: str
    count_payload: Callable[[dict], int]


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


class JevDecisionProvider:
    def __init__(self, *, model: str, cloud_opt_in: bool = False, api_key: str | None = None,
                 accounting: TrustedTokenAccounting | None = None,
                 profiles: dict[str, ProviderTaskCalibration] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10):
        # Version syntax is a caller pin, not a claim that a model/version exists upstream.
        if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{1,95}", model) or
                not any(c.isdigit() for c in model) or any(x in model.lower() for x in ("latest", "auto", "default"))):
            raise ValueError("explicit_versioned_model_required")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_timeout")
        self.model = model
        self.cloud_opt_in = cloud_opt_in is True
        self._api_key = api_key
        self.accounting = accounting
        self.gate = ConservativeDecisionGate("jev", model, profiles)
        self.transport = transport
        self.timeout = min(timeout, 30)
        self._busy = asyncio.Lock()

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        started = time.monotonic()
        result = DecisionResult(task_id=request.task_id, task_version=request.task_version, model=self.model)
        result.metrics = {"provider_id": "jev", "transport": "cloud", "upload_attempted": False}
        if not self.cloud_opt_in:
            result.unknown_reason = "cloud_opt_in_required"
            return result
        if not math.isfinite(request.deadline) or started >= request.deadline:
            result.unknown_reason = "expired"
            return result
        if request.task_id not in TASKS or request.task_version != "1":
            result.unknown_reason = "unsupported_task_version"
            return result
        if self._busy.locked():
            result.unknown_reason = "provider_busy"
            return result
        if not self._api_key or not isinstance(self._api_key, str) or len(self._api_key) > 8192:
            result.unknown_reason = "credential_required"
            return result
        if (self.accounting is None or self.accounting.model != self.model or not self.accounting.version):
            result.unknown_reason = "budget_unverified"
            return result
        scope_error = self.gate.check_profile(request)
        if scope_error:
            result.unknown_reason = scope_error
            return result
        try:
            state = json.dumps(request.compact_features, ensure_ascii=False, sort_keys=True, allow_nan=False)
            instructions, labels = TASKS[request.task_id]
            payload = {"model": self.model, "state": state, "questions": {request.task_id: {
                "type": "choice", "instructions": instructions,
                "criteria": {label: label for label in labels}}}}
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            result.input_hash = hashlib.sha256(encoded).hexdigest()
            if len(encoded) > 65536:
                result.unknown_reason = "context_overflow"
                return result
            count = self.accounting.count_payload(payload)
            if type(count) is not int or count <= 0:
                result.unknown_reason = "budget_unverified"
                return result
            result.token_count = count
            result.metrics["token_accounting_version"] = self.accounting.version
            if count > 1024:
                result.unknown_reason = "context_overflow"
                return result
        except Exception:
            result.unknown_reason = "input_or_token_accounting_invalid"
            return result
        if time.monotonic() >= request.deadline:
            result.unknown_reason = "expired"
            return result
        async with self._busy:
            try:
                remaining = min(self.timeout, request.deadline - time.monotonic())
                async with asyncio.timeout(max(.001, remaining)):
                    async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                                 follow_redirects=False, timeout=remaining) as client:
                        result.metrics["upload_attempted"] = True
                        async with client.stream("POST", JEV_ENDPOINT, content=encoded,
                            headers={"Authorization": "Bearer " + self._api_key,
                                     "Content-Type": "application/json"}) as response:
                            if response.status_code != 200:
                                result.unknown_reason = "provider_http_error"
                                return result
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(data) + len(chunk) > 65536:
                                    result.unknown_reason = "response_overflow"
                                    return result
                                data.extend(chunk)
                if time.monotonic() >= request.deadline:
                    result.unknown_reason = "expired"
                    return result
                body = json.loads(data)
                if body["model"] != self.model:
                    result.unknown_reason = "provider_model_version_mismatch"
                    return result
                usage = body["usage"]["input_tokens"]
                if type(usage) is not int or usage < 1 or usage > 1024:
                    result.unknown_reason = "response_token_budget_invalid"
                    return result
                if usage != result.token_count:
                    result.unknown_reason = "token_accounting_mismatch"
                    return result
                answer = body["answers"][request.task_id]
                if answer["type"] != "choice":
                    raise ValueError()
                result.metrics["reported_input_tokens"] = usage
                return self.gate.apply(request, answer["probabilities"], result)
            except TimeoutError:
                result.unknown_reason = "expired" if time.monotonic() >= request.deadline else "provider_timeout"
            except asyncio.CancelledError:
                raise
            except Exception:
                # No upstream body, exception text, request, endpoint credentials or recognized text escapes.
                result.unknown_reason = "provider_protocol_or_transport_error"
            finally:
                result.latency_ms = round((time.monotonic() - started) * 1000, 3)
        return result
