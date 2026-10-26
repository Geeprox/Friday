"""Protocol/failure fixtures only: no real HA, credentials or lamp writes."""
import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_homeplane_ha import Socket

from homeplane.action_contracts import LightAuthority, LightPlanRequest
from homeplane.actions import ActionController
from homeplane.api import create_app
from homeplane.core import HomeCore
from homeplane.ha import HAReadOnlyAdapter
from homeplane.simulator import Simulator
from homeplane.store import TraceStore


def rig(tmp_path):
    row = {"entity_id":"light.simulated_study", "state":"off", "attributes":{
        "friendly_name":"Simulated study lamp", "brightness":40, "supported_color_modes":["brightness"]}}
    post = []
    fault = {"send":None, "readback":None, "offline":False}
    rows = [row]
    def handle(request):
        if fault["offline"]:
            return httpx.Response(503)
        if request.method == "POST":
            post.append(request)
            if fault["send"] == "timeout":
                raise httpx.ReadTimeout("SIMULATED-secret-do-not-echo")
            if fault["send"] == "cancel":
                raise asyncio.CancelledError()
            if fault["send"] == "deny":
                return httpx.Response(401, json={"secret":"SIMULATED-do-not-echo"})
            body = json.loads(request.content)
            if fault["readback"] != "mismatch":
                row["state"] = "off" if request.url.path.endswith("turn_off") else "on"
                row["attributes"]["brightness"] = body.get("brightness", row["attributes"]["brightness"])
            return httpx.Response(200, json=[{"entity_id":"light.unrelated_changed"}])
        if request.url.path == "/api/states":
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/services":
            return httpx.Response(200, json=[{"domain":"light","services":{"turn_on":{},"turn_off":{}}}])
        if fault["readback"] == "unavailable":
            return httpx.Response(503)
        return httpx.Response(200, json=row)
    ha = HAReadOnlyAdapter("http://simulated.invalid", "SIMULATED-private-secret", "simulated-instance",
                          transport=httpx.MockTransport(handle), ws_connect=lambda *_a, **_k: Socket(row["entity_id"]))
    store = TraceStore(tmp_path / "simulated.sqlite")
    core = HomeCore(ha, store)
    path = tmp_path / "simulated-authority.json"
    authority = LightAuthority(enabled=True, authority_ref="SIMULATED-authorized-study-only",
                ha_url=ha.url, ha_instance=ha.instance_id, target="ha:simulated-instance:stable-registry-id",
                external_entity_id=row["entity_id"], registry_device_id="physical-1", allow_brightness=True,
                expires_at=datetime.now(UTC) + timedelta(hours=1))
    path.write_text(authority.model_dump_json())
    path.chmod(0o600)
    core.actions = ActionController(core, path)
    return core, row, rows, post, fault, path, authority


async def plan(core, *, desired=None, **changes):
    snapshot = await core.snapshot()
    entity = snapshot.entities[0]
    request = LightPlanRequest(target=entity.id, desired=desired or {"power":"on","brightness":8},
                              expected_state_version=entity.state_version,
                              expires_at=datetime.now(UTC) + timedelta(seconds=30), idempotency_key="SIMULATED-key")
    return await core.actions.plan(request.model_copy(update=changes))


async def close(core):
    core.actions.close()
    await core.adapter.close()
    core.store.close()


@pytest.mark.asyncio
async def test_absolute_combined_write_durable_stages_and_reported_not_physical(tmp_path):
    core, _, _, post, _, _, _ = rig(tmp_path)
    trace = await plan(core)
    assert trace.status == "planned" and not post and not trace.real_write_attempted
    result = await core.actions.execute(trace.action_id)
    assert result.status == "reported_match" and not result.physically_confirmed
    assert [e.payload["status"] for e in result.events] == ["planned","sent","accepted","reported_match"]
    assert len(post) == 1 and post[0].url.path == "/api/services/light/turn_on"
    assert json.loads(post[0].content) == {"entity_id":"light.simulated_study","brightness":8}
    assert "SIMULATED-private-secret" not in result.model_dump_json()
    assert core.store.light_action(trace.action_id) == result
    assert await core.actions.execute(trace.action_id) == result and len(post) == 1
    await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_name,expected", [("timeout","unknown_outcome"),("deny","failed")])
async def test_send_failure_no_retry_or_secret_echo(tmp_path, fault_name, expected):
    core, _, _, post, fault, _, _ = rig(tmp_path)
    trace = await plan(core)
    fault["send"] = fault_name
    result = await core.actions.execute(trace.action_id)
    assert result.status == expected and result.real_write_attempted and len(post) == 1
    assert "secret" not in result.model_dump_json()
    fault["send"] = None
    assert await core.actions.execute(trace.action_id) == result and len(post) == 1
    await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_name", ["mismatch","unavailable"])
async def test_readback_failure_unknown_not_physical_success(tmp_path, fault_name):
    core, _, _, post, fault, _, _ = rig(tmp_path)
    trace = await plan(core)
    fault["readback"] = fault_name
    result = await core.actions.execute(trace.action_id)
    assert result.status == "unknown_outcome" and not result.physically_confirmed
    assert len(post) == 1
    await close(core)


@pytest.mark.asyncio
async def test_target_state_conflict_but_unrelated_updates_do_not_block_scoped_plan(tmp_path):
    core, row, rows, post, _, _, _ = rig(tmp_path)
    trace = await plan(core)
    rows.append({"entity_id":"sensor.simulated_unrelated","state":"1","attributes":{}})
    assert (await core.actions.execute(trace.action_id)).status == "reported_match"
    other = await plan(core, desired={"power":"off"}, idempotency_key="SIMULATED-other-key")
    row["attributes"]["brightness"] = 9
    assert (await core.actions.execute(other.action_id)).code == "source_conflict"
    assert len(post) == 1
    await close(core)


@pytest.mark.asyncio
async def test_revocation_expiry_identity_and_simulator_disable_writer(tmp_path):
    core, row, _, post, _, path, authority = rig(tmp_path)
    trace = await plan(core)
    path.write_text(authority.model_copy(update={"enabled":False}).model_dump_json())
    assert (await core.actions.execute(trace.action_id)).code == "authority_changed"
    path.write_text(authority.model_dump_json())
    expired = await plan(core, expires_at=datetime.now(UTC) - timedelta(seconds=1), idempotency_key="SIMULATED-expired")
    assert expired.status == "expired"
    foreign = await plan(core, target="light.simulated_foreign", idempotency_key="SIMULATED-foreign")
    assert foreign.code == "unauthorized"
    unavailable = await plan(core, idempotency_key="SIMULATED-unavailable")
    row["state"] = "unavailable"
    assert (await core.actions.execute(unavailable.action_id)).status == "rejected"
    assert not post
    await close(core)


@pytest.mark.asyncio
async def test_parallel_duplicate_and_key_conflict_send_once(tmp_path):
    core, _, _, post, _, _, _ = rig(tmp_path)
    trace = await plan(core)
    with pytest.raises(ValueError, match="source_conflict"):
        await plan(core, desired={"power":"off"})
    a, b = await asyncio.gather(core.actions.execute(trace.action_id), core.actions.execute(trace.action_id))
    assert a == b and len(post) == 1
    await close(core)


@pytest.mark.asyncio
async def test_restart_expires_pending_and_reconciles_sent_without_resend(tmp_path):
    core, _, _, post, _, path, _ = rig(tmp_path)
    pending = await plan(core)
    sent = await plan(core, idempotency_key="SIMULATED-sent")
    core.actions._stage(sent, "sent", "send_claim_recorded")
    core.actions.close()
    core.actions = ActionController(core, path)
    result = await core.actions.execute(sent.action_id)
    assert result.status == "unknown_outcome" and result.code == "interrupted_send_requires_readback"
    assert (await core.actions.execute(pending.action_id)).code == "restart_requires_new_plan"
    assert not post
    await close(core)


@pytest.mark.asyncio
async def test_second_owner_cannot_act_or_mark_live_owner_interrupted(tmp_path):
    core, _, _, post, _, path, _ = rig(tmp_path)
    pending = await plan(core)
    core.actions._stage(pending, "sent", "send_claim_recorded")
    other = ActionController(core, path)
    assert other.authority() == (None, None)
    assert core.store.light_action(pending.action_id).status == "sent"
    other.close()
    assert not post
    await close(core)


@pytest.mark.asyncio
async def test_cancelled_after_send_and_storage_failure_do_not_replay(tmp_path, monkeypatch):
    core, _, _, post, fault, _, _ = rig(tmp_path)
    trace = await plan(core)
    fault["send"] = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await core.actions.execute(trace.action_id)
    assert core.store.light_action(trace.action_id).status == "unknown_outcome"
    other = await plan(core, idempotency_key="SIMULATED-storage")
    def fail(*_a, **_kw):
        raise sqlite3.OperationalError("SIMULATED disk full")
    monkeypatch.setattr(core.store, "save_light_action", fail)
    with pytest.raises(sqlite3.OperationalError):
        await core.actions.execute(other.action_id)
    assert len(post) == 1
    await close(core)


@pytest.mark.asyncio
async def test_readonly_reconcile_after_response_loss_does_not_send_or_invent_cause(tmp_path):
    core, row, _, post, fault, _, _ = rig(tmp_path)
    trace = await plan(core)
    fault["send"] = "timeout"
    unknown = await core.actions.execute(trace.action_id)
    assert unknown.status == "unknown_outcome"
    # Hardware might have received a timed-out request; fixture reports matching state.
    row["state"], row["attributes"]["brightness"] = "on", 8
    reconciled = await core.actions.reconcile(trace.action_id)
    assert reconciled.status == "reported_match" and not reconciled.physically_confirmed
    assert reconciled.code == "readback_match_causality_unconfirmed"
    assert "cannot prove" in reconciled.reported["causality"] and len(post) == 1
    assert await core.actions.execute(trace.action_id) == reconciled and len(post) == 1
    await close(core)


@pytest.mark.asyncio
async def test_pause_revokes_pending_plan_and_does_not_touch_external_rules(tmp_path):
    core, _, _, post, _, path, _ = rig(tmp_path)
    trace = await plan(core)
    report = core.actions.pause()
    assert report["external_rules_changed"] is False
    assert not LightAuthority.model_validate_json(path.read_text()).enabled
    assert (await core.snapshot()).action_targets == []
    assert (await core.actions.execute(trace.action_id)).status == "rejected"
    assert not post
    await close(core)


@pytest.mark.asyncio
async def test_monotonic_deadline_and_private_authority_fail_closed(tmp_path):
    core, _, _, post, _, path, _ = rig(tmp_path)
    trace = await plan(core)
    core.actions._deadlines[trace.action_id] = 0
    assert (await core.actions.execute(trace.action_id)).status == "expired"
    path.chmod(0o644)
    assert core.actions.authority() == (None, None)
    assert (await core.snapshot()).action_targets == [] and not post
    await close(core)


@pytest.mark.parametrize("desired", [{"power":"toggle"},{"power":"on","brightness":0},
    {"power":"on","brightness":True},{"power":"on","brightness":256},
    {"power":"off","brightness":2},{"power":"on","service":"shell"}])
def test_unsupported_parameters_never_reach_actuator(desired):
    with pytest.raises(ValueError):
        LightPlanRequest(target="simulated", desired=desired, expected_state_version="simulated",
                         expires_at=datetime.now(UTC), idempotency_key="simulated")


def test_http_replay_origin_and_simulator_have_no_actuator(tmp_path):
    app = create_app(db_path=tmp_path / "simulated.sqlite", settings_path=tmp_path / "settings.json")
    with TestClient(app, base_url="http://testserver") as client:
        assert client.post("/api/actions/plan", json={}).status_code == 403
        assert client.post("/api/actions/execute", json={"plan_id":"simulated"}, headers={"origin":"http://evil.invalid"}).status_code == 403
        assert client.get("/api/snapshot").json()["action_targets"] == []
        body = {"target":"light.hall", "desired":{"power":"on"}, "expected_state_version":"simulated",
                "expires_at":(datetime.now(UTC) + timedelta(seconds=30)).isoformat(), "idempotency_key":"simulated"}
        result = client.post("/api/actions/plan", json=body, headers={"origin":"http://testserver"}).json()
        assert result["status"] == "rejected" and not result["real_write_attempted"]
        assert client.post("/api/actions/execute", json={"plan_id":result["action_id"]}, headers={"origin":"http://testserver"}).json() == result
        assert isinstance(app.state.core.adapter, Simulator)
