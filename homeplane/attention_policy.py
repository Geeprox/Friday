"""Qualified attention gates; gaze contributes evidence but does not authorize actions."""

import math
import time
from dataclasses import dataclass

from .interaction_contracts import AttentionAdmission, InteractionBinding


@dataclass(frozen=True)
class AttentionDecision:
    admitted: bool
    code: str
    gaze: str = "unavailable"
    producer_state: str = "unavailable"


def attention_policy(
    *, evidence, admission: AttentionAdmission | None, binding: InteractionBinding | None,
    explicit_command: bool, wake: bool, wake_required: bool, threshold: float,
    max_age_seconds: float, allow_simulated=False, now=None,
):
    now = time.time() if now is None else now
    if wake_required and not wake:
        return AttentionDecision(False, "say_friday")
    if not explicit_command:
        return AttentionDecision(False, "unsupported_command")
    if (
        evidence is None or not evidence.live_speaker or evidence.people_count != 1
        or not math.isfinite(evidence.attention) or evidence.attention < threshold
    ):
        return AttentionDecision(False, "attention_uncertain")
    if (admission is None or not admission.matches(binding)
        or admission.source_kind == "fixture" and not allow_simulated):
        return AttentionDecision(False, "ambient_admission_unavailable")
    producer_state = "simulated" if admission.source_kind == "fixture" else "admitted"
    if (
        admission.source_kind not in {"live", "fixture"} or not admission.final_transcript
        or not admission.addressed_to_system or not admission.quality_ok
        or not -2 <= now - admission.captured_at <= max_age_seconds
        or now >= admission.expires_at
    ):
        return AttentionDecision(False, "attention_uncertain", admission.gaze, producer_state)
    # Neither not-looking nor unavailable is a hard reject of admitted commands.
    return AttentionDecision(True, "qualified_explicit_command", admission.gaze, producer_state)
