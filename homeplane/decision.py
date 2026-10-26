"""Bounded, shadow-only local semantic decisions. Never grants device authority."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
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



def result_dict(result: DecisionResult) -> dict:
    return asdict(result)
