"""Optional native usage in reviewed rule receipts; no host or model execution."""

import pytest

pytest.importorskip("arl.codex_context", reason="Optional ARL native usage contract")

from arl.codex_context import token_receipt
from arl.friday_planning_client import plan_removal

from tests.test_homeplane_arl_rule_planning import approval, cleanup, request, setup

COUNTS = {
    "inputTokens": 100,
    "outputTokens": 37,
    "reasoningOutputTokens": 8,
    "cachedInputTokens": 20,
    "totalTokens": 137,
}


async def analyze_with_usage(tmp_path, usage):
    core, manager, peer, runtime, service = setup(tmp_path)
    try:
        before = manager.backend.records()

        async def executor(runtime, reviewed, **kwargs):
            # The frozen legacy planner still rejects native usage upstream.
            # Exercise Friday's separately supported, trusted executor port.
            receipt = await plan_removal(runtime, reviewed, **kwargs)
            receipt["usage"] = usage
            return receipt

        service.planning_executor = executor
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        again = await service.analyze(approval(review))
        assert peer.submissions == 1
        assert manager.backend.records() == before
        assert not result.real_write_attempted and not result.physically_confirmed
        return result, again
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", [COUNTS, dict.fromkeys(COUNTS, 0)])
async def test_native_usage_survives_the_reviewed_executor_without_granting_effects(tmp_path, counts):
    native = token_receipt(
        {
            "threadId": "SIMULATED-thread",
            "turnId": "SIMULATED-turn",
            "tokenUsage": {"last": counts, "modelContextWindow": 128000},
        },
        "SIMULATED-thread",
        "SIMULATED-turn",
    )
    assert native is not None
    usage = {"tokens": native["last"], "cost": None}
    result, again = await analyze_with_usage(tmp_path, usage)
    assert result.status == "planned", result
    assert [action.id for action in result.plan.new_rule.actions] == ["hall-light-on"]
    assert result.model_receipt["usage"] == usage
    assert result.model_receipt["token_hard_cap_supported"] is False
    assert again.model_receipt == result.model_receipt
    usage["tokens"]["totalTokens"] = 999
    assert result.model_receipt["usage"]["tokens"]["totalTokens"] == counts["totalTokens"]
    assert again.model_receipt == result.model_receipt


@pytest.mark.asyncio
async def test_absent_native_usage_stays_unknown_instead_of_becoming_zero(tmp_path):
    result, again = await analyze_with_usage(tmp_path, {"tokens": None, "cost": None})
    assert result.status == "planned", result
    assert result.model_receipt["usage"] == {"tokens": None, "cost": None}
    assert result.model_receipt["token_hard_cap_supported"] is False
    assert again.model_receipt == result.model_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"tokens": None},
        {"tokens": COUNTS, "cost": 0},
        {"tokens": True, "cost": None},
        {"tokens": 137, "cost": None},
        {"tokens": {}, "cost": None},
        {"tokens": {"input_tokens": 100}, "cost": None},
        {"tokens": {k: v for k, v in COUNTS.items() if k != "totalTokens"}, "cost": None},
        {"tokens": {**COUNTS, "unexpected": 1}, "cost": None},
        {"tokens": {**COUNTS, "inputTokens": True}, "cost": None},
        {"tokens": {**COUNTS, "inputTokens": 1.0}, "cost": None},
        {"tokens": {**COUNTS, "inputTokens": -1}, "cost": None},
        {"tokens": {**COUNTS, "inputTokens": "100"}, "cost": None},
        {"tokens": {**COUNTS, "inputTokens": None}, "cost": None},
        {"tokens": {**COUNTS, "inputTokens": float("nan")}, "cost": None},
        {"tokens": None, "cost": None, "unexpected": 1},
    ],
)
async def test_malformed_usage_cannot_admit_a_plan_or_replay_inference(tmp_path, usage):
    result, again = await analyze_with_usage(tmp_path, usage)
    assert result.status == "needs_review" and result.plan is None
    assert result.model_receipt is None
    assert again.code == "analysis_previous_attempt_not_replayed"
