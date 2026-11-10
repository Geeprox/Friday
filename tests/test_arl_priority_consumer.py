"""真实公开 SDK 的 v2 优先级消费；传输和模型结果均为合成数据。

这些检查不证明完整宿主的身份生产器、租约或真实模型可用。
"""

import asyncio
import copy
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("arl.friday_planning_client", reason="需显式公开 SDK 客户端输入")

from homeplane.arl_planning_priority import reviewed_priority_executor
from homeplane.arl_rule_planning import MODEL
from tests.arl_fixtures import require_priority_host_sdk
from tests.test_homeplane_arl_rule_planning import SyntheticPeer, approval, cleanup, request, setup


def profile():
    """固定合成宿主快照，交给真实 SDK 校验；不是实际授权。"""
    return dict(
        schema_version=2,
        profile_id="SIMULATED-v2",
        policy_epoch="SIMULATED-epoch",
        identity_bound=True,
        allowed_classes=["background", "interactive"],
        task_kinds=["friday.reviewed.plan"],
        limits=dict(
            queue=8,
            per_resource_inflight=1,
            burst_calls=10,
            burst_window_ms=1000,
            max_deadline_ms=30000,
            weight=1,
        ),
        resources={
            "fixture_pool": dict(
                kind="fixture_pool",
                slots=1,
                realtime_reserved=0,
                queue_limit=8,
                class_queue_limit=8,
                background_start_limit_ms=12000,
                cooperative_cancel=False,
                capacity_evidence="synthetic_only",
            )
        },
        cancel_ack_releases_slot=False,
        preemption="not_qualified",
        real_time_guarantee=False,
        reasons=[],
        task_bindings=[
            dict(
                submit_method="priority.submit.v1",
                tool="friday.rule-removal.plan.v1",
                task_kind="friday.reviewed.plan",
                resource_kind="fixture_pool",
                route=dict(provider="codex_app_server", model=MODEL, model_fingerprint=None),
                allowed_classes=["background", "interactive"],
                estimate_floor_ms=5,
                execution_budget_ms=1000,
            )
        ],
    )


class PriorityPeer(SyntheticPeer):
    def __init__(self):
        super().__init__()
        self.profile = profile()
        self.scheduling = None
        self.bad_receipt = False
        self.bad_get_receipt = False
        self.after_negotiate = lambda: None
        self.entered, self.finish = threading.Event(), threading.Event()
        self.hold_get = False

    def _complete(self):
        return {**super()._complete(), "scheduling": copy.deepcopy(self.scheduling)}

    def rpc(self, name, payload=None):
        if name == "capabilities":
            value = super().rpc(name, payload)
            value["tools"][0]["priority_required"] = True
            return value
        if name == "priority.negotiate.v2":
            self.calls.append(name)
            self.after_negotiate()
            return copy.deepcopy(self.profile)
        if name == "priority.submit.v1":
            self.calls.append(name)
            self.submissions += 1
            self.payload = copy.deepcopy(payload)
            req = payload["scheduling"]
            self.scheduling = {
                key: req[key]
                for key in [
                    "profile_id",
                    "policy_epoch",
                    "task_kind",
                    "requested_class",
                    "resource_kind",
                    "deadline_at_ms",
                ]
            }
            self.scheduling.update(
                schema_version=1,
                effective_class="interactive",
                estimate_ms=5,
                execution_budget_ms=1000,
                reasons=[],
                allow_interrupt_effective=False,
            )
            if self.bad_receipt:
                self.scheduling["policy_epoch"] = "SIMULATED-replaced"
            if self.wait:
                return dict(
                    id="SIMULATED-job",
                    tool="friday.rule-removal.plan.v1",
                    state="running",
                    scheduling=copy.deepcopy(self.scheduling),
                )
            return self._complete()
        if name == "get" and self.hold_get:
            self.entered.set()
            assert self.finish.wait(3), "合成读取必须在有限时间内释放"
        value = super().rpc(name, payload)
        if name == "get":
            value["scheduling"] = copy.deepcopy(self.scheduling)
            if self.bad_get_receipt:
                value["scheduling"]["policy_epoch"] = "SIMULATED-replaced"
        return value


@pytest.fixture
def composition(tmp_path):
    core, manager, _, runtime, service = setup(tmp_path)
    peer = PriorityPeer()
    runtime.client_factory = lambda *_: peer
    service.planning_executor = reviewed_priority_executor
    before = manager.backend.records()
    yield core, manager, peer, runtime, service
    peer.finish.set()
    assert manager.backend.records() == before
    cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_first_submit_uses_v2_and_preserves_review(composition):
    _, _, peer, _, service = composition
    review = await service.prepare(request(service))
    result = await service.analyze(approval(review))
    assert result.plan is not None and not result.real_write_attempted
    assert peer.calls.count("priority.negotiate.v2") == peer.calls.count("priority.submit.v1") == 1
    assert "submit" not in peer.calls
    assert peer.payload["key"] == review.id == review.request["execution_id"]
    assert peer.payload["scheduling"]["requested_class"] == "interactive"
    assert peer.payload["scheduling"]["deadline_at_ms"] <= review.request["expires_at_ms"]
    await service.analyze(approval(review))
    assert peer.submissions == 1


@pytest.mark.asyncio
async def test_missing_route_does_not_fall_back_to_legacy(composition):
    _, _, peer, _, service = composition
    peer.profile.update(task_bindings=[], task_kinds=[], resources={})
    review = await service.prepare(request(service))
    result = await service.analyze(approval(review))
    assert result.plan is None and peer.submissions == 0 and "submit" not in peer.calls
    assert "priority" in result.code


@pytest.mark.asyncio
async def test_unverified_initial_acceptance_does_not_target_an_untrusted_job(composition):
    _, _, peer, _, service = composition
    peer.bad_receipt = True
    review = await service.prepare(request(service))
    result = await service.analyze(approval(review))
    assert result.plan is None and peer.submissions == 1 and peer.cancelled == 0
    assert "priority_receipt_mismatch" in result.code
    assert "get" not in peer.calls


@pytest.mark.asyncio
async def test_changed_polled_acceptance_cancels_only_the_known_job(composition):
    _, _, peer, _, service = composition
    peer.wait = peer.bad_get_receipt = True
    review = await service.prepare(request(service))
    result = await service.analyze(approval(review))
    assert result.plan is None and peer.submissions == 1 and peer.cancelled == 1
    assert "priority_receipt_mismatch" in result.code


@pytest.mark.asyncio
async def test_context_revoked_after_negotiation_stops_before_submit(composition):
    _, _, peer, _, service = composition
    review = await service.prepare(request(service))
    peer.after_negotiate = lambda: service.projections[review.id].invalidate()
    result = await service.analyze(approval(review))
    assert result.plan is None and peer.submissions == 0
    assert "submit" not in peer.calls


@pytest.mark.asyncio
async def test_cancel_keeps_busy_gate_until_late_transport_returns(composition):
    _, _, peer, runtime, service = composition
    peer.wait = peer.hold_get = True
    review = await service.prepare(request(service))
    task = asyncio.create_task(service.analyze(approval(review)))
    assert await asyncio.to_thread(peer.entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._busy.locked()
    peer.finish.set()
    end = time.monotonic() + 2
    while runtime._busy.locked() and time.monotonic() < end:
        await asyncio.sleep(0.01)
    assert not runtime._busy.locked() and peer.cancelled == 1 and peer.submissions == 1
    assert (await service.analyze(approval(review))).code == "analysis_previous_attempt_not_replayed"


def test_host_integration_without_explicit_input_is_declared_optional(monkeypatch):
    monkeypatch.delenv("FRIDAY_TEST_ARL_PRIORITY_HOST_SOURCE", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_priority_host_sdk()


@pytest.mark.parametrize("source", ["different", "client_only"])
def test_explicit_wrong_host_input_fails_without_namespace_replacement(monkeypatch, tmp_path, source):
    arl = sys.modules["arl"]
    before = list(sys.path)
    root = tmp_path if source == "different" else Path(arl.__file__).resolve().parents[1]
    monkeypatch.setenv("FRIDAY_TEST_ARL_PRIORITY_HOST_SOURCE", str(root))
    with pytest.raises(pytest.fail.Exception):
        require_priority_host_sdk()
    assert sys.modules["arl"] is arl and sys.path == before
