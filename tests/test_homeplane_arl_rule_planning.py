"""SIMULATED SDK/provider receipts. No household, account or real model access.

The real installed ARL typed helper and planner validator run against an explicit
provider double; real-shaped simulated=False exercises receipt parsing only.
"""

import asyncio
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
pytest.importorskip("arl.friday_planning", reason="Optional ARL typed planning SDK")
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.arl import ARLRuntime
from homeplane.arl_rule_planning import ARLRuleChanges, RuleAnalysisApproval, RuleAnalysisRequest
from homeplane.core_rules import CoreRules
from homeplane.rule_text import RuleTextChanges
from tests.test_homeplane_rule_deployment import close, rig


class SyntheticPeer:
    def __init__(self):
        self.calls, self.submissions, self.cancelled = [], 0, 0
        self.corrupt, self.abstain, self.lost = None, None, False
        self.wait, self.release = False, threading.Event()
        self.choose_light = False

    def connect(self, **_kwargs):
        self.calls.append("connect")

    def provider(self, args, _context):
        req = json.loads(args["input"].split("Context: ", 1)[1])
        choice = next(
            c for c in req["choices"] if ("light" if self.choose_light else "climate") in c["action_label"]
        )
        proposal = {
            "schema_version": 1,
            "status": self.abstain or "proposal",
            "record_id": choice["record_id"],
            "action_id": choice["action_id"],
            "expected_fingerprint": choice["expected_fingerprint"],
            "summary": "SIMULATED 删除空调动作，保留其他动作",
        }
        if self.abstain:
            proposal.update(record_id=None, action_id=None, expected_fingerprint=None)
        text = json.dumps(proposal, ensure_ascii=False)
        return {
            "schema_version": 1,
            "format": "codex.appserver.text.v1",
            "provider": "codex_app_server",
            "model": args["model"],
            "request_sha256": hashlib.sha256(args["input"].encode()).hexdigest(),
            "text": text,
            "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "simulated": False,
            "token_hard_cap_supported": False,
            "usage": {"tokens": None, "cost": None},
            "thread_id": "SIMULATED-thread",
            "turn_id": "SIMULATED-turn",
        }

    def _complete(self):
        from arl.friday_planning import FridayRemovalPlanner

        context = SimpleNamespace(
            row={"owner": "friday", "key": self.payload["key"], "id": "SIMULATED-job"},
            check_cancelled=lambda: None,
            remaining=lambda maximum=30: maximum,
        )
        result = FridayRemovalPlanner(SimpleNamespace(run=self.provider)).run(self.payload["args"], context)
        if self.corrupt:
            key, value = self.corrupt
            result[key] = value
        return {
            "id": "SIMULATED-job",
            "tool": "friday.rule-removal.plan.v1",
            "state": "succeeded",
            "result": result,
        }

    def rpc(self, name, payload=None):
        from arl.contracts import Fault
        from arl.friday_planning import NAME, PERMISSIONS, SCHEMA

        self.calls.append(name)
        if name == "capabilities":
            return {
                "protocol": "arl/1",
                "version": 1,
                "tools": [
                    {
                        "name": NAME,
                        "version": 1,
                        "inputSchema": SCHEMA,
                        "permissions": list(PERMISSIONS),
                        "idempotent": False,
                        "availability": "adapter",
                    }
                ],
            }
        if name == "submit":
            self.submissions += 1
            self.payload = payload
            if self.wait:
                return {"id": "SIMULATED-job", "tool": NAME, "state": "running"}
            result = self._complete()
            if self.lost:
                raise Fault("disconnected")
            return result
        if name == "get":
            return (
                self._complete()
                if self.release.is_set()
                else {"id": "SIMULATED-job", "tool": NAME, "state": "running"}
            )
        if name == "cancel":
            self.cancelled += 1
            return {}
        raise AssertionError("Unexpected SDK method " + name)


def setup(tmp_path, *, enabled=True):
    core, manager = rig(tmp_path)
    peer = SyntheticPeer()
    runtime = ARLRuntime(lambda: tmp_path, client_factory=lambda _home, _start: peer)
    service = ARLRuleChanges(
        RuleTextChanges(manager, runtime),
        CoreRules(core, manager),
        runtime,
        authorizer=(lambda _review: True) if enabled else None,
    )
    return core, manager, peer, runtime, service


def request(service, **extra):
    return RuleAnalysisRequest(
        text="以后我回家可以不开空调了",
        expected_context=service.context().fingerprint,
        idempotency_key="SIMULATED-" + str(uuid4()),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        **extra,
    )


def approval(review):
    return RuleAnalysisApproval(review_id=review.id, approval_digest=review.approval_digest)


def cleanup(core, manager, service):
    service.close()
    close(core, manager)


@pytest.mark.asyncio
async def test_real_sdk_candidate_into_existing_diff_preserves_unrequested_program(tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path)
    try:
        before = manager.backend.records()
        review = await service.prepare(request(service))
        assert not review.model_inference_attempted and peer.calls == []
        assert set(review.request) == {
            "schema_version",
            "execution_id",
            "text",
            "context_fingerprint",
            "captured_at_ms",
            "expires_at_ms",
            "choices",
        }
        assert all(
            set(c) == {"record_id", "action_id", "expected_fingerprint", "rule_label", "action_label"}
            for c in review.request["choices"]
        )
        assert (
            review.request["text"]
            not in core.store.connection.execute("SELECT body FROM arl_rule_reviews").fetchone()[0]
        )
        result = await service.analyze(approval(review))
        assert result.status == "planned", result
        assert result.baseline_comparison == "matched"
        assert result.planner_call_attempted and result.model_inference_attempted
        assert result.context.simulated and not result.real_write_attempted
        assert manager.backend.records() == before  # Plan only, no apply/toggle/effect.
        plan = result.plan
        assert [a.id for a in plan.new_rule.actions] == ["hall-light-on"]
        assert plan.new_rule.actions[0] == plan.old.rule.actions[0]
        assert plan.new_rule.trigger == plan.old.rule.trigger
        assert plan.new_rule.conditions == plan.old.rule.conditions
        assert plan.new_rule.safety == plan.old.rule.safety
        assert (await service.analyze(approval(review))).plan.id == plan.id
        assert peer.submissions == 1 and peer.cancelled == 0 and not runtime._busy.locked()
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_default_off_approval_never_calls_sdk_or_provider(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path, enabled=False)
    try:
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.code == "analysis_controlled_actor_not_authorized"
        assert not result.planner_call_attempted and not result.model_inference_attempted and peer.calls == []
        assert not service.context().analysis_enabled
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("simulated", True),
        ("owner", "other"),
        ("model", "other"),
        ("execution_id", "other"),
        ("request_sha256", "0" * 64),
        ("prompt_sha256", "0" * 64),
        ("captured_at_ms", 1),
        ("usage", {"tokens": 100, "cost": 0}),
        ("token_hard_cap_supported", True),
        ("thread_id", ""),
        ("turn_id", "bad\nreceipt"),
        ("action_authority", True),
        ("shell", "SIMULATED malicious extra field"),
    ],
)
async def test_drifted_receipt_rejected_never_replayed_or_applied(tmp_path, field, value):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        before = manager.backend.records()
        review = await service.prepare(request(service))
        peer.corrupt = (field, value)
        result = await service.analyze(approval(review))
        assert result.status == "needs_review" and result.plan is None
        assert result.model_inference_attempted is None
        assert (await service.analyze(approval(review))).code == "analysis_previous_attempt_not_replayed"
        assert peer.submissions == 1 and manager.backend.records() == before
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["clarification", "unsupported"])
async def test_abstention_is_not_a_plan(tmp_path, status):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        peer.abstain = status
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.status == status and result.plan is None and result.model_inference_attempted
        assert len(manager.backend.records()) == 1
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_lost_submission_receipt_and_restart_cannot_resubmit(tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path)
    try:
        review = await service.prepare(request(service))
        peer.lost = True
        result = await service.analyze(approval(review))
        assert result.status == "needs_review" and result.model_inference_attempted is None
        service.close()
        service = ARLRuleChanges(service.text, service.core_rules, runtime, authorizer=lambda _review: True)
        assert (await service.analyze(approval(review))).code == "analysis_review_not_resumable"
        assert peer.submissions == 1
        assert (
            core.store.connection.execute("SELECT status FROM arl_rule_reviews").fetchone()[0]
            == "needs_review"
        )
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_fresh_loop_projection_revokes_context_and_cancel_does_not_hold_core_lock(tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path)
    try:
        original = service.context
        owner_thread = threading.get_ident()

        def context_on_owner_loop():
            assert threading.get_ident() == owner_thread
            return original()

        service.context = context_on_owner_loop
        peer.wait = True
        review = await service.prepare(request(service))
        task = asyncio.create_task(service.analyze(approval(review)))
        for _ in range(100):
            if peer.submissions:
                break
            await asyncio.sleep(0.01)
        assert peer.submissions == 1
        record = manager.backend.records()[0]
        await manager.backend.toggle(record.id, record.fingerprint, False)
        result = await asyncio.wait_for(task, 1)
        assert result.status == "needs_review" and result.plan is None and peer.cancelled == 1
        assert not runtime._busy.locked()
        # No SDK worker reads SQLite: domain context runs on the owner loop.
        assert service.context().fingerprint != review.context.fingerprint
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_cancellation_unknown_is_journaled_and_busy_gate_drains(tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path)
    try:
        peer.wait = True
        review = await service.prepare(request(service))
        task = asyncio.create_task(service.analyze(approval(review)))
        for _ in range(100):
            if peer.submissions:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not runtime._busy.locked():
                break
            await asyncio.sleep(0.01)
        assert not runtime._busy.locked() and peer.cancelled == 1
        assert (await service.analyze(approval(review))).code == "analysis_previous_attempt_not_replayed"
        assert peer.submissions == 1
    finally:
        cleanup(core, manager, service)


def test_http_review_has_explicit_default_off_and_no_public_enablement(tmp_path):
    app = create_app(
        db_path=tmp_path / "SIMULATED-http.sqlite", settings_path=tmp_path / "SIMULATED-settings.json"
    )
    with TestClient(
        app, base_url="http://127.0.0.1:8740", headers={"Origin": "http://127.0.0.1:8740"}
    ) as client:
        context = client.get("/api/rules/analysis/context").json()
        assert context["available"] and not context["analysis_enabled"]
        body = request(app.state.rule_analysis).model_dump(mode="json")
        assert (
            client.post(
                "/api/rules/analysis/reviews", json=body, headers={"Origin": "https://untrusted.invalid"}
            ).status_code
            == 403
        )
        assert client.post("/api/rules/analysis/reviews", json={**body, "model": "other"}).status_code == 422
        review = client.post("/api/rules/analysis/reviews", json=body).json()
        result = client.post(
            "/api/rules/analysis/analyze",
            json={"review_id": review["id"], "approval_digest": review["approval_digest"]},
        ).json()
        assert (
            result["code"] == "analysis_controlled_actor_not_authorized"
            and not result["model_inference_attempted"]
        )
        assert (
            client.post(
                "/api/rules/analysis/analyze",
                json={
                    "review_id": review["id"],
                    "approval_digest": review["approval_digest"],
                    "enabled": True,
                },
            ).status_code
            == 422
        )
        assert len(app.state.rules.backend.records()) == 1


@pytest.mark.asyncio
async def test_model_disagreeing_with_deterministic_baseline_cannot_plan(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        peer.choose_light = True
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.status == "rejected" and result.baseline_comparison == "disagreed"
        assert result.model_inference_attempted and result.plan is None
        assert len(manager.backend.records()) == 1
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_review_copy_cannot_replace_approved_payload_and_bad_digest_cannot_call(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        review = await service.prepare(request(service))
        bad = approval(review).model_copy(update={"approval_digest": "0" * 64})
        assert (await service.analyze(bad)).code == "analysis_approval_mismatch" and peer.calls == []
        review.request["text"] = "SIMULATED mutated caller text"
        review.request["choices"].clear()
        result = await service.analyze(approval(review))
        assert result.status == "planned"
        sent = json.loads(peer.payload["args"]["request_json"])
        assert sent["text"] == "以后我回家可以不开空调了" and len(sent["choices"]) == 2
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_expired_review_drops_before_sdk_and_is_not_recreated_with_same_key(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        req = request(service)
        review = await service.prepare(req)
        service.deadlines[review.id] = 0
        assert (await service.analyze(approval(review))).status == "expired"
        with pytest.raises(ValueError, match="analysis_request_not_resumable"):
            await service.prepare(req)
        assert peer.calls == []
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_scope_and_wire_budget_reject_without_truncating_or_inference(tmp_path):
    from homeplane.arl import ARLError
    from homeplane.rule_deployment import record

    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        with pytest.raises(ValueError, match="analysis_scope_unavailable"):
            await service.prepare(request(service, record_ids=["SIMULATED-unknown"]))
        original = manager.backend.records()[0]
        for i in range(8):
            rule = original.rule.model_copy(update={"id": "SIMULATED-extra-" + str(i)})
            row = await manager.backend.create_disabled("SIMULATED-record-" + str(i), rule)
            await manager.backend.toggle(row.id, row.fingerprint, True)
        with pytest.raises(ValueError, match="analysis_scope_too_broad"):
            await service.prepare(request(service))
        selected = await service.prepare(request(service, record_ids=[original.id]))
        assert len(selected.choices) == 2 and peer.calls == []
        # Maximum valid label length still has a UTF8 budget. Do not trim choices.
        long = record(original.id, original.rule.model_copy(update={"intent": "测" * 120}), True)
        with core.store.connection:
            core.store.connection.execute(
                "UPDATE simulated_rules SET body=?,fingerprint=? WHERE id=?",
                (long.model_dump_json(), long.fingerprint, long.id),
            )
        ids = [original.id] + ["SIMULATED-record-" + str(i) for i in range(7)]
        with core.store.connection:
            for identifier in ids:
                row = manager.backend.read(identifier)
                updated = record(identifier, row.rule.model_copy(update={"intent": "测" * 120}), True)
                core.store.connection.execute(
                    "UPDATE simulated_rules SET body=?,fingerprint=? WHERE id=?",
                    (updated.model_dump_json(), updated.fingerprint, identifier),
                )
        with pytest.raises(ARLError, match="planning_context_overflow"):
            await service.prepare(request(service, record_ids=ids))
        assert peer.calls == []
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_public_cancel_invalidates_live_projection_without_waiting_for_inference(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        first = await service.prepare(request(service))
        assert service.cancel(approval(first)).code == "analysis_cancelled_before_call"
        assert (await service.analyze(approval(first))).code == "analysis_cancelled_before_call"
        assert peer.calls == []
        peer.wait = True
        review = await service.prepare(request(service))
        task = asyncio.create_task(service.analyze(approval(review)))
        for _ in range(100):
            if peer.submissions:
                break
            await asyncio.sleep(0.01)
        cancelled = service.cancel(approval(review))
        assert cancelled.status == "needs_review" and cancelled.model_inference_attempted is None
        result = await asyncio.wait_for(task, 1)
        assert result.plan is None and peer.cancelled == 1 and peer.submissions == 1
        service.deadlines[review.id] = 0
        later = await service.analyze(approval(review))
        assert later.status == "needs_review" and later.model_inference_attempted is None
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_changed_source_after_valid_sdk_result_retains_inference_evidence_but_no_plan(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        review = await service.prepare(request(service))
        original = core.snapshot

        async def changed_snapshot():
            core.adapter.fixture["entities"][0]["name"] += " SIMULATED source changed"
            return await original()

        core.snapshot = changed_snapshot
        result = await service.analyze(approval(review))
        assert result.status == "needs_review" and result.plan is None
        assert result.model_inference_attempted is True and peer.submissions == 1
        assert len(manager.backend.records()) == 1
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_durable_claim_failure_prevents_even_sdk_connect(tmp_path):
    core, manager, peer, runtime, service = setup(tmp_path)
    try:
        review = await service.prepare(request(service))
        core.store.connection.execute(
            "CREATE TRIGGER SIMULATED_abort_claim BEFORE UPDATE OF status ON arl_rule_reviews WHEN NEW.status='analyzing' BEGIN SELECT RAISE(ABORT,'SIMULATED journal unavailable'); END"
        )
        result = await service.analyze(approval(review))
        assert result.code == "analysis_journal_unavailable" and not result.planner_call_attempted
        assert peer.calls == [] and not service.projections and not runtime._busy.locked()
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_result_journal_failure_keeps_unknown_attempt_and_never_returns_plan(tmp_path):
    core, manager, peer, _runtime, service = setup(tmp_path)
    try:
        review = await service.prepare(request(service))
        core.store.connection.execute(
            "CREATE TRIGGER SIMULATED_abort_result BEFORE UPDATE OF status ON arl_rule_reviews WHEN NEW.status!='analyzing' BEGIN SELECT RAISE(ABORT,'SIMULATED journal unavailable'); END"
        )
        result = await service.analyze(approval(review))
        assert result.status == "needs_review" and result.code == "analysis_journal_unavailable"
        assert result.plan is None and result.model_inference_attempted
        assert (await service.analyze(approval(review))).code == "analysis_previous_attempt_not_replayed"
        assert peer.submissions == 1
        core.store.connection.execute("DROP TRIGGER SIMULATED_abort_result")
    finally:
        cleanup(core, manager, service)


@pytest.mark.asyncio
async def test_production_core_rule_candidate_uses_existing_core_plan_not_simulator_saga(tmp_path):
    from tests.test_homeplane_core_rules import cleanup as close_core
    from tests.test_homeplane_core_rules import enable, fixture, proposal

    core, _rows, calls, _fault, _path, _grant, owner, manager, runtime_rules = await fixture(tmp_path)
    service = None
    try:
        snapshot = await core.snapshot()
        light = next(e for e in snapshot.entities if e.kind == "light")
        climate = next(e for e in snapshot.entities if e.kind == "climate")
        actions = [
            {
                "id": "SIMULATED-light-retained",
                "type": "device.set",
                "target": light.id,
                "operation": "light.set",
                "desired": {"power": "on"},
            },
            {
                "id": "SIMULATED-climate-removed",
                "type": "device.set",
                "target": climate.id,
                "operation": "climate.mode",
                "desired": {"hvac_mode": "cool"},
            },
        ]
        installed = await enable(manager, await proposal(core, manager, actions=actions))
        old = manager.read(installed.new.id)
        peer = SyntheticPeer()
        runtime = ARLRuntime(lambda: tmp_path, client_factory=lambda _home, _start: peer)
        service = ARLRuleChanges(
            RuleTextChanges(owner, runtime), manager, runtime, authorizer=lambda _review: True
        )
        review = await service.prepare(request(service))
        result = await service.analyze(approval(review))
        assert result.status == "planned", result
        assert result.plan.new.rule.actions == [old.rule.actions[0]]
        assert result.plan.new.rule.trigger == old.rule.trigger
        assert result.plan.new.rule.safety == old.rule.safety
        assert not result.context.simulated and not result.real_write_attempted
        assert manager.read(old.id) == old and len(manager.records()) == 1 and calls == []
        assert result.plan.code and result.baseline_comparison == "not_applicable"
    finally:
        if service:
            service.close()
        await close_core(core, owner, runtime_rules)
