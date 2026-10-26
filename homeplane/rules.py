"""Small tested planning primitives. No native Graph compiler or deployment claim."""
import hashlib
from copy import deepcopy

from .contracts import Capability, ExternalRuleRecord


def preserve_external(rule_id: str, source: str, raw: str, *, simulated=False) -> ExternalRuleRecord:
    return ExternalRuleRecord(
        id=rule_id, source=source, raw=raw, fingerprint=hashlib.sha256(raw.encode()).hexdigest(),
        readable=True, coverage_note="Unsupported semantics preserved opaque; no round-trip conversion.",
        simulated=simulated,
    )


def remove_action(rule: dict, *, target: str, capability: str, expected_fingerprint: str) -> dict:
    """Only project-owned restricted fixture IR; never patch opaque vendor Graphs."""
    fingerprint = hashlib.sha256(__import__("json").dumps(rule, sort_keys=True).encode()).hexdigest()
    if fingerprint != expected_fingerprint:
        raise ValueError("source_conflict")
    if rule.get("trigger") != "arrival-transition" or rule.get("simulated") is not True:
        raise ValueError("unsupported")
    result = deepcopy(rule)
    result["actions"] = [a for a in result["actions"]
                         if not (a["target"] == target and a["capability"] == capability)]
    result["revision"] += 1
    return result


def minimum_light_action(capability: Capability, *, simulated: bool) -> dict:
    if not (capability.minimum_verified and capability.minimum is not None
            and capability.atomic_power_brightness and capability.no_bright_flash_verified):
        raise ValueError("unsupported_minimum_or_no_flash")
    if capability.verification != ("simulated" if simulated else "hardware"):
        raise ValueError("hardware_verification_required")
    return {"power": "on", "brightness": capability.minimum}
