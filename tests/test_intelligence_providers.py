"""仅注入合成传输，不使用真实账号、云端模型或设备。"""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from homeplane.arl import ARLError
from homeplane.decision import DecisionRequest
from homeplane.intelligence.decisions import OpenAIDecisionProvider
from homeplane.intelligence.runtime import IntelligenceRuntime, verified_planning_result


def runtime(tmp_path, handler, *, arl=None):
    owner = IntelligenceRuntime(
        arl or AsyncMock(),
        tmp_path,
        transport=httpx.MockTransport(handler),
        key_resolver=lambda: "SIMULATED-api-key",
    )
    owner.settings.update({"provider": "openai", "cloud_text": True, "allow_arl_fallback": False})
    return owner


def completion(text='{"kind":"clarify"}'):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


async def test_responses_contract_and_distinct_receipt(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["store"] is False and body["text"]["format"]["type"] == "json_object"
        assert "tools" not in body and body["max_output_tokens"] == 2048
        return httpx.Response(200, json=completion())

    owner = runtime(tmp_path, handler)
    result = await owner.complete_scenario(
        "home_planning", "模拟问题", request_id="a", deadline=time.monotonic() + 5
    )
    assert verified_planning_result(result, "a")
    assert not verified_planning_result(result, "different")
    assert "native_acknowledgements_verified" not in result
    assert owner.status()["usage"]["used"]["text_tokens"] == 30
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,code,reason",
    [
        (429, "insufficient_quota", "api_quota_exhausted"),
        (429, "rate_limit_exceeded", "api_rate_limited"),
        (401, "invalid_api_key", "api_key_rejected"),
        (403, "permission_denied", "api_model_unavailable"),
        (404, "model_not_found", "api_model_unavailable"),
        (503, "unknown", "api_unavailable"),
    ],
)
async def test_api_failures_are_redacted_bounded_and_fall_back(tmp_path, status, code, reason):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"code": code, "message": "SIMULATED-sensitive-upstream-error"}}
        )

    arl = AsyncMock()
    arl.complete_scenario.return_value = {"text": "simulated ARL result"}
    owner = runtime(tmp_path, handler, arl=arl)
    owner.settings.update({"provider": "auto", "allow_arl_fallback": True})
    for i in range(2):
        assert (
            await owner.complete_scenario(
                "home_planning", "模拟问题", request_id=str(i), deadline=time.monotonic() + 5
            )
            == arl.complete_scenario.return_value
        )
    assert len(calls) == 1
    assert arl.complete_scenario.await_count == 2
    assert owner.last_route == {"provider": "arl", "reason": reason}
    assert "sensitive" not in json.dumps(owner.status())


async def test_budget_blocks_before_network_and_arl_is_optional(tmp_path):
    calls = []
    owner = runtime(tmp_path, lambda request: calls.append(request))
    owner.settings.update({"daily_requests": 0})
    with pytest.raises(ARLError, match="daily_requests"):
        await owner.complete_scenario(
            "home_planning", "模拟问题", request_id="a", deadline=time.monotonic() + 5
        )
    assert not calls and not owner.arl.complete_scenario.called
    owner.settings.update({"provider": "local"})
    with pytest.raises(ARLError, match="local_only"):
        await owner.complete_scenario(
            "home_planning", "模拟问题", request_id="b", deadline=time.monotonic() + 5
        )
    owner.settings.update({"provider": "arl"})
    await owner.complete_scenario("home_planning", "模拟问题", request_id="c", deadline=time.monotonic() + 5)
    assert owner.arl.complete_scenario.await_count == 1 and not calls


async def test_cancellation_keeps_unknown_reservation_without_retry(tmp_path):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Future()

    owner = runtime(tmp_path, handler)
    task = asyncio.create_task(
        owner.complete_scenario("home_planning", "模拟问题", request_id="a", deadline=time.monotonic() + 5)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.status()["usage"]["uncertain_requests"] == 1
    assert owner.status()["usage"]["used"]["text_tokens"] > 2048


async def test_decisions_preserve_original_calibration_and_authority_gate(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/decisions" and body["model"] == "gpt-6-luna"
        assert body["questions"][0]["type"] == "choice"
        return httpx.Response(
            200,
            json={
                "answers": [
                    {
                        "type": "choice",
                        "name": "needs_grounding",
                        "choice": "yes",
                        "probabilities": [
                            {"value": "yes", "probability": 0.99},
                            {"value": "no", "probability": 0.005},
                            {"value": "unknown", "probability": 0.005},
                        ],
                    }
                ],
                "usage": {"input_tokens": 40},
            },
        )

    owner = runtime(tmp_path, handler)
    result = await OpenAIDecisionProvider(owner).decide(
        DecisionRequest("needs_grounding", {"text": "这个打开"}, time.monotonic() + 5)
    )
    assert result.metrics["shadow_prediction"] == "yes"
    assert result.unknown_reason == "uncalibrated_task" and result.shadow and not result.actionable
    assert result.label == "unknown" and result.token_count == 40


async def test_configuration_revocation_suppresses_late_api_result(tmp_path):
    owner = None

    async def handler(request):
        owner.settings.update({"cloud_text": False})
        return httpx.Response(200, json=completion())

    owner = runtime(tmp_path, handler)
    owner.settings.update({"allow_arl_fallback": True})
    with pytest.raises(ARLError, match="configuration_changed"):
        await owner.complete_scenario(
            "home_planning", "模拟问题", request_id="a", deadline=time.monotonic() + 5
        )
    assert not owner.arl.complete_scenario.called
    assert owner.status()["usage"]["used"]["text_tokens"] == 30


@pytest.mark.parametrize("capability", ["responses", "realtime"])
async def test_revocation_while_waiting_for_budget_prevents_upstream_dispatch(tmp_path, capability):
    calls = []
    owner = runtime(tmp_path, lambda request: calls.append(request))
    owner.settings.update({"cloud_voice": True, "allow_arl_fallback": True})
    reserve = owner.ledger.reserve

    def revoke(*args, **kwargs):
        reserve(*args, **kwargs)
        owner.settings.update({"provider": "local"})

    owner.ledger.reserve = revoke
    if capability == "responses":
        with pytest.raises(ARLError, match="configuration_changed"):
            await owner.complete_scenario(
                "home_planning", "模拟问题", request_id="queued", deadline=time.monotonic() + 5
            )
    else:
        from homeplane.intelligence.budget import EnhancementUnavailable
        from homeplane.intelligence.voice import RealtimeVoice

        def connector(*args, **kwargs):
            pytest.fail("关闭后不应打开上游连接")

        async def audio():
            pytest.fail("关闭后不应读取音频")
            yield b""

        emit = AsyncMock()
        with pytest.raises(EnhancementUnavailable, match="configuration_changed"):
            await RealtimeVoice(owner, connector=connector).transcribe(audio(), emit)
        emit.assert_not_called()
    assert not calls and not owner.arl.complete_scenario.called
    assert owner.status()["usage"]["used"]["text_tokens"] == 0


async def test_api_timeout_keeps_time_for_arl_fallback_without_replaying_cloud(tmp_path):
    from types import SimpleNamespace

    arl = AsyncMock()
    arl.complete_scenario.return_value = {"text": "fallback"}
    owner = runtime(tmp_path, lambda request: None, arl=arl)
    owner.settings.update({"allow_arl_fallback": True})
    deadline = time.monotonic() + 20

    async def unavailable(*args, **kwargs):
        from homeplane.intelligence.budget import EnhancementUnavailable

        assert kwargs["deadline"] <= deadline - 4
        raise EnhancementUnavailable("api_timeout")

    owner._client = SimpleNamespace(complete=unavailable)
    assert await owner.complete_scenario("home_planning", "问题", request_id="one", deadline=deadline) == {
        "text": "fallback"
    }
    assert arl.complete_scenario.call_args.kwargs["deadline"] == deadline


async def test_legacy_planning_minimizes_attributes_and_local_route_never_invokes_models(tmp_path):
    from friday.config import Settings
    from friday.providers import ARLPlanner, ARLRouteDecision

    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"summary":"需明确目标","steps":[]}'))

    owner = runtime(tmp_path, handler)
    context = {
        "request": "调亮客厅",
        "model_request_id": "legacy-test",
        "camera_room": "客厅",
        "private_extension": "PRIVATE-extension",
        "devices": [
            {
                "id": "light.synthetic",
                "name": "合成灯",
                "room": "客厅",
                "state": "on",
                "capabilities": {"turn_on": {}},
                "domain": "light",
                "attributes": {"brightness": 100, "token": "PRIVATE-token", "entity_picture": "PRIVATE-url"},
            }
        ],
    }
    await ARLPlanner(Settings(arl_model_tasks_enabled=True), intelligence=owner).plan(context, {})
    assert "PRIVATE" not in calls[0]["input"] and '"brightness": 100' in calls[0]["input"]
    owner.settings.update({"provider": "local"})
    result = await ARLRouteDecision(Settings(), intelligence=owner).classify("开灯")
    assert result["provider"] == "local" and result["confidence"] == 0
    assert len(calls) == 1 and not owner.arl.complete.called


async def test_selected_local_mode_blocks_special_host_analysis_before_submission(tmp_path):
    from homeplane.arl_rule_planning import RuleAnalysisApproval
    from tests.test_homeplane_arl_rule_planning import request, setup

    core, manager, peer, arl, service = setup(tmp_path / "host")
    owner = runtime(tmp_path / "ai", lambda request: None, arl=arl)
    try:
        review = await service.prepare(request(service))
        service.model_allowed = owner.allows_arl_analysis
        owner.settings.update({"provider": "local"})
        result = await service.analyze(
            RuleAnalysisApproval(review_id=review.id, approval_digest=review.approval_digest)
        )
        assert result.code == "analysis_channel_unavailable" and peer.submissions == 0
    finally:
        service.close()
        await arl.close()
        core.store.close()
