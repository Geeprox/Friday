"""Mechanical capability proof and pure Simulator evaluation. No vendor codegen."""

import hashlib
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from .rule_contracts import (
    ClimateInstruction,
    DecisionCondition,
    DelayInstruction,
    DeviceInstruction,
    EventTrigger,
    FeedbackInstruction,
    LightInstruction,
    ModeCondition,
    NumericCondition,
    RuleCompilation,
    RuleEvaluation,
    TimeCondition,
)
from .rules import minimum_light_action


def digest(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def compile_rule(rule, snapshot):
    simulated = (
        snapshot.mode == "simulator"
        and rule.simulated
        and all(e.simulated for e in snapshot.entities + snapshot.derived_entities)
    )
    result = RuleCompilation(
        rule_id=rule.id,
        revision=rule.revision,
        fingerprint=digest(rule),
        status="unsupported",
        backend=rule.execution.preferred_backend,
        reasons=[],
        simulated=simulated,
    )
    errors, dependencies, bindings, instructions = [], [], {}, []
    entities = {e.id: e for e in snapshot.entities + snapshot.derived_entities}

    def capability(target, name, access):
        entity = entities.get(target)
        cap = (
            next(
                (
                    c
                    for c in entity.capabilities
                    if c.name == name and c.access == access and c.support == "supported"
                ),
                None,
            )
            if entity
            else None
        )
        if cap is None:
            errors.append("unbound_or_unsupported:" + target + ":" + name)
        else:
            bindings[target] = [b.model_dump(include={"source", "external_id"}) for b in entity.bindings]
        return cap

    # Event edges have an explicit registered capability; read support alone is not evidence.
    trigger_cap = (
        rule.trigger.capability
        if isinstance(rule.trigger, EventTrigger)
        else rule.trigger.capability + ".transition"
    )
    trigger = capability(rule.trigger.entity, trigger_cap, "event")
    wan_known = True
    for condition in rule.conditions:
        if isinstance(condition, NumericCondition):
            cap = capability(condition.entity, condition.capability, "read")
            if cap and cap.unit != condition.unit:
                errors.append("unit_mismatch:" + condition.entity)
            wan_known &= bool(cap and cap.wan_dependency == "no")
            if any(e.id == condition.entity for e in snapshot.derived_entities):
                dependencies.append("core_canonical_quality:" + condition.entity)
        elif isinstance(condition, ModeCondition):
            # No native gateway lease expiry has been verified for this project.
            dependencies.append("core_mode_lease:" + condition.mode)
        elif isinstance(condition, DecisionCondition):
            dependencies.append("arl_decision:" + condition.provider)
            # Provider name alone cannot prove ARL deployment locality/offline readiness.
            wan_known = False
        elif isinstance(condition, TimeCondition):
            dependencies.append("core_timezone_schedule")

    elapsed = 0
    for action in rule.actions:
        if isinstance(action, (LightInstruction, ClimateInstruction, DeviceInstruction)):
            if action.target in rule.safety.prohibited_targets:
                errors.append("prohibited_target:" + action.target)
            operation = action.operation if isinstance(action, DeviceInstruction) else action.type
            cap = capability(action.target, operation, "write")
            wan_known &= bool(cap and cap.wan_dependency == "no")
            desired = action.desired if isinstance(action, DeviceInstruction) else {"power": action.power}
            if isinstance(action, DeviceInstruction):
                from .ha_control import parameter_error

                if cap is not None and (error := parameter_error(cap, operation, desired)):
                    errors.append(error + ":" + action.target)
            if isinstance(action, LightInstruction) and cap:
                if action.brightness == "minimum_verified":
                    try:
                        desired = minimum_light_action(cap, simulated=simulated)
                        value = desired["brightness"]
                        if (
                            not isinstance(value, (int, float))
                            or value != int(value)
                            or not 1 <= value <= 255
                        ):
                            raise ValueError("unqualified_api_level")
                        desired["brightness"] = int(value)
                    except ValueError:
                        errors.append("unqualified_minimum_or_no_flash:" + action.target)
                elif action.brightness is not None:
                    if (
                        cap.minimum is None
                        or cap.maximum is None
                        or not cap.minimum <= action.brightness <= cap.maximum
                    ):
                        errors.append("brightness_outside_bound_capability:" + action.target)
                    desired["brightness"] = action.brightness
                if action.require_no_bright_flash and not (
                    cap.atomic_power_brightness
                    and cap.no_bright_flash_verified
                    and cap.verification == ("simulated" if simulated else "hardware")
                ):
                    errors.append("unqualified_no_flash:" + action.target)
            instructions.append(
                {
                    "id": action.id,
                    "type": action.type,
                    **({"operation": operation} if isinstance(action, DeviceInstruction) else {}),
                    "target": action.target,
                    "desired": desired,
                    "after_ms": elapsed,
                }
            )
        elif isinstance(action, DelayInstruction):
            elapsed += action.milliseconds
            dependencies.append("core_cancellable_delay")
        elif isinstance(action, FeedbackInstruction):
            dependencies.append("core_output_endpoint_unqualified:" + action.endpoint)
            if action.route == "cloud_tts":
                wan_known = False
            instructions.append(action.model_dump(mode="json"))
        else:
            dependencies.append("core_cancel_same_rule_only")
            instructions.append(action.model_dump(mode="json"))
    if elapsed > 30000:
        errors.append("sequence_delay_budget_exceeded")
    wan_known &= bool(trigger and trigger.wan_dependency == "no")
    if rule.execution.require_wan_independent and not wan_known:
        errors.append("wan_independence_unproven")
    # Sleep/manual protections are enforced at our managed execution point, never external rules.
    dependencies.append("core_managed_sleep_and_manual_guards")
    if rule.safety.allow_during_sleep and (
        not any(isinstance(c, ModeCondition) and c.mode == "home.sleep" and c.equals for c in rule.conditions)
        or any(
            not isinstance(
                a,
                (
                    LightInstruction,
                    DelayInstruction,
                ),
            )
            for a in rule.actions
        )
        or any(
            isinstance(a, LightInstruction)
            and (a.brightness != "minimum_verified" or not a.require_no_bright_flash)
            for a in rule.actions
        )
    ):
        errors.append("sleep_exception_requires_qualified_minimum_lighting")
    if rule.execution.require_core_independent:
        errors.append("core_independence_unproven")
    if rule.execution.preferred_backend in {"ha", "mijia_geek"}:
        errors.append("native_rule_backend_not_qualified")
    if rule.execution.preferred_backend == "simulator" and not simulated:
        errors.append("simulator_requires_simulated_inventory_and_rule")
    result.bindings = bindings
    result.wan_independent = wan_known and not errors
    result.reasons = list(dict.fromkeys(errors + dependencies))
    if not errors:
        result.status = (
            "supported" if simulated and rule.execution.preferred_backend == "simulator" else "requires_core"
        )
        result.instructions = instructions
    return result


def remove_instruction(rule, action_id, expected_fingerprint):
    if digest(rule) != expected_fingerprint:
        raise ValueError("source_conflict")
    if not any(a.id == action_id for a in rule.actions):
        raise ValueError("not_found")
    # Validate again; removing the last effect must not silently disable an entire rule.
    body = rule.model_dump(mode="json")
    body["actions"] = [a for a in body["actions"] if a["id"] != action_id]
    body["revision"] += 1
    return type(rule).model_validate(body)


def evaluate_rule(rule, snapshot, trial, *, now, allow_core=False, preview=False):
    event = trial.event
    compilation = compile_rule(rule, snapshot)

    def result(outcome, reasons, commands=None):
        return RuleEvaluation(
            rule_id=rule.id,
            event_id=event.event_id,
            outcome=outcome,
            reasons=reasons,
            commands=commands or [],
            simulated=not allow_core or preview,
        )

    core_scope = (
        allow_core
        and snapshot.mode == "ha-read-only"
        and not rule.simulated
        and rule.execution.preferred_backend == "home_core"
        and compilation.status == "requires_core"
        and not compilation.simulated
        and event.simulated == preview
    )
    if not core_scope and (
        compilation.status != "supported" or not compilation.simulated or not event.simulated
    ):
        return result("unsupported", compilation.reasons or ["simulation_only"])
    if trial.replay:
        return result("suppressed", ["replay_never_actuates"])
    age = (now - event.wall_time).total_seconds() * 1000
    if (
        age < 0
        or age > rule.execution.max_event_age_ms
        or event.boot_id not in {e.boot_id for e in snapshot.events}
    ):
        return result("suppressed", ["expired_future_or_foreign_boot_event"])
    expected_kind = "source-report" if isinstance(rule.trigger, EventTrigger) else "state-transition"
    if event.kind != expected_kind:
        return result("not_triggered", ["snapshot_and_data_edges_are_not_triggers"])
    p, t = event.payload, rule.trigger
    if p.get("entity_id") != t.entity or p.get("capability") != t.capability:
        return result("not_triggered", ["different_trigger"])
    pairs = (
        [(p.get("value"), t.value)]
        if isinstance(t, EventTrigger)
        else [(p.get("from"), t.from_value), (p.get("to"), t.to_value)]
    )
    if any(type(actual) is not type(expected) or actual != expected for actual, expected in pairs):
        return result("not_triggered", ["different_transition_or_event_value"])
    reasons = []
    # Explicit protection must be known; unknown-origin off never creates a sleep mode.
    for mode in ("home.sleep", "home.manual"):
        lease = trial.modes.get(mode)
        if lease is None or not lease.issued_at <= now < lease.valid_until:
            reasons.append("guard_unknown:" + mode)
        elif lease.value and not (mode == "home.sleep" and rule.safety.allow_during_sleep):
            reasons.append("protected_mode:" + mode)
    for condition in rule.conditions:
        if isinstance(condition, ModeCondition):
            lease = trial.modes.get(condition.mode)
            if lease is None or not lease.issued_at <= now < lease.valid_until:
                reasons.append("unknown_mode_lease:" + condition.mode)
            elif lease.value != condition.equals:
                reasons.append("condition_false:" + condition.mode)
        elif isinstance(condition, NumericCondition):
            state = next(
                (s for s in snapshot.states if s.id == condition.entity + ":" + condition.capability), None
            )
            if (
                state is None
                or state.simulated != (not core_scope)
                or state.quality != "healthy"
                or state.valid_until is None
                or now >= state.valid_until
                or state.as_of > now
                or type(state.value) not in {int, float}
                or state.unit != condition.unit
            ):
                reasons.append("unknown_numeric_quality:" + condition.entity)
            else:
                from .quality import compare_numeric

                value = compare_numeric(state, condition.operator, condition.value, now)
                if value == "unknown":
                    reasons.append("unknown_numeric_quality:" + condition.entity)
                elif value == "false":
                    reasons.append("condition_false:" + condition.entity)
        elif isinstance(condition, TimeCondition):
            local = now.astimezone(ZoneInfo(condition.timezone)).strftime("%H:%M")
            start, end = condition.start, condition.end
            inside = start <= local < end if start < end else local >= start or local < end
            if not inside:
                reasons.append("outside_time_window")
        else:
            reasons.append("decision_result_unqualified_or_missing")
    commands = compilation.instructions
    elapsed = max((c.get("after_ms", 0) for c in commands), default=0)
    for action in rule.actions:
        if isinstance(action, (LightInstruction, ClimateInstruction, DeviceInstruction)):
            states = [s for s in snapshot.states if s.id.startswith(action.target + ":")]
            if not any(
                s.quality == "healthy"
                and s.valid_until is not None
                and s.as_of <= now < s.valid_until - timedelta(milliseconds=elapsed)
                for s in states
            ):
                reasons.append("unknown_target_state:" + action.target)
    if now + timedelta(milliseconds=elapsed) > event.wall_time + timedelta(
        milliseconds=rule.execution.max_event_age_ms
    ):
        reasons.append("delayed_action_would_be_stale")
    if any(c["type"] == "feedback.speak" for c in commands):
        reasons.append("output_endpoint_unqualified")
    return result(
        "suppressed" if reasons else "matched",
        reasons
        or ["evaluated_intents_only_no_actuator" if core_scope else "SIMULATED_intents_only_no_actuator"],
        [] if reasons else commands,
    )
