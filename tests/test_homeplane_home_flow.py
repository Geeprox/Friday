"""Real application/HTTP/widget probes, with local Simulator domains only."""

import asyncio
import itertools
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeplane.api import create_app

PHRASE = "以后我回家不要自动开空调"
sequence = itertools.count()


@asynccontextmanager
async def application(tmp_path, *, timezone="Asia/Shanghai", adapter=None, authority_path=None):
    # Resolve the macOS /tmp symlink without changing the original local DB guard.
    root = tmp_path.resolve()
    app = create_app(adapter=adapter, authority_path=authority_path,
                     db_path=root / "traces.sqlite", settings_path=root / "settings.json",
                     flow_household_timezone=timezone)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://testserver",
                                    headers={"Origin": "http://testserver"}) as client:
            yield app, client


async def bootstrap(client):
    response = await client.post("/api/home-flow/bootstrap")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["code"] == "flow_ready"
    return result


def request(session, *, operation="rules:1", **fields):
    number = next(sequence)
    return dict(session_id=session, operation_id=operation, turn_id=f"turn:{number}",
                idempotency_key=f"key:{number}", **fields)


async def turn(client, body):
    response = await client.post("/api/home-flow/turn", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def domain(response):
    value = response["domain"]
    return value["report"] if "report" in value else value


@pytest.mark.asyncio
async def test_real_rule_transaction_rotates_tokens_and_never_renews_session(tmp_path):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        session = ready["session_id"]
        first_body = request(session, text=PHRASE)
        preview = await turn(client, first_body)
        assert preview["phase"] == "preview", preview
        plan = app.state.rules.get(preview["plan_id"])
        assert plan.status == "awaiting_approval"
        duplicate = await turn(client, first_body)
        assert duplicate["code"] == "flow_duplicate_turn"
        done = await turn(client, request(session, kind="approve", plan_id=preview["plan_id"],
            approval_digest=preview["approval_digest"], expected_context=preview["context_token"]))
        result = domain(done)
        assert result == app.state.rules.get(plan.id).model_dump(mode="json")
        assert result["status"] == "verified" and result["simulated"]
        assert result["real_write_attempted"] is False
        assert result["physically_confirmed"] is False
        assert done["context_token"] != preview["context_token"]
        assert datetime.fromisoformat(done["expires_at"]) <= datetime.fromisoformat(ready["expires_at"])
        output = done["domain"]["output"]
        assert output["status"] == "unavailable" and output["attempted"] is False
        assert output["completed"] is False
        stale = await turn(client, request(session, kind="rollback", plan_id=plan.id,
                                         expected_context=preview["context_token"]))
        assert stale["code"] == "flow_context_changed"
        assert app.state.rules.get(plan.id).status == "verified"
        rolled = await turn(client, request(session, kind="rollback", plan_id=plan.id,
                                          expected_context=done["context_token"]))
        assert domain(rolled)["status"] == "rolled_back"
        current = (await client.post("/api/home-flow/context", json={"session_id": session})).json()
        assert current["expires_at"] == ready["expires_at"]
        assert current["code"] == "flow_current"
        assert app.state.home_flow.control.ports.controller is app.state.core.actions


@pytest.mark.asyncio
async def test_late_old_cancel_does_not_cancel_new_pending_operation(tmp_path, monkeypatch):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        session = ready["session_id"]
        first = await turn(client, request(session, text=PHRASE))
        assert first["phase"] == "preview"
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.rule_text.plan

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.state.rule_text, "plan", held)
        newer = asyncio.create_task(turn(client, request(session, operation="rules:2", text=PHRASE)))
        await asyncio.wait_for(entered.wait(), 2)
        stopped = await asyncio.wait_for(client.post("/api/home-flow/cancel", json=request(
            session, kind="cancel", expected_context=first["context_token"])), 2)
        assert stopped.status_code == 200
        assert stopped.json()["operation_id"] == "rules:1"
        assert app.state.home_flow._current(app.state.home_flow.sessions[session])
        assert not app.state.home_flow.sessions[session].operations["rules:2"].cancelled
        release.set()
        report = await asyncio.wait_for(newer, 2)
        assert report["phase"] == "preview" and report["operation_id"] == "rules:2"


@pytest.mark.asyncio
async def test_source_switch_fences_before_lock_and_late_snapshot_cannot_create_plan(tmp_path, monkeypatch):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.core.snapshot

        async def noncooperative():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await original()

        monkeypatch.setattr(app.state.core, "snapshot", noncooperative)
        sending = asyncio.create_task(turn(client, request(ready["session_id"], operation="control:1",
            lane="control", text="打开玄关灯")))
        await asyncio.wait_for(entered.wait(), 2)
        switching = asyncio.create_task(client.post("/api/source/simulator"))
        # The existing source endpoint invalidates synchronously before its lock.
        for _ in range(100):
            if app.state.home_flow.sessions[ready["session_id"]].invalidated:
                break
            await asyncio.sleep(.001)
        assert app.state.home_flow.sessions[ready["session_id"]].invalidated
        release.set()
        report = await asyncio.wait_for(sending, 2)
        changed = await asyncio.wait_for(switching, 2)
        assert changed.status_code == 200, changed.text
        assert report["phase"] == "cancelled" and report.get("plan_id") is None
        assert app.state.core.store.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0] == 0
        current = (await client.post("/api/home-flow/context", json={"session_id":ready["session_id"]})).json()
        assert current["code"] == "flow_session_unavailable"


@pytest.mark.asyncio
async def test_bootstrap_expiry_dispose_public_injection_and_restart(tmp_path):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        session = ready["session_id"]
        flow = app.state.home_flow
        assert flow.sessions[session].scope.rooms, "room choices come from original snapshot"
        response = await client.post("/api/home-flow/turn", json=request(session, text=PHRASE,
                                    authority={"permitted":True}))
        assert response.status_code == 422
        forbidden = await client.post("/api/home-flow/turn", json=request(session, text=PHRASE),
                                      headers={"Origin":"https://outside.invalid"})
        assert forbidden.status_code == 403
        flow.monotonic = lambda: flow.sessions[session].deadline + 1
        expired = await turn(client, request(session, text=PHRASE))
        assert expired["code"] == "flow_session_unavailable"
        flow.monotonic = __import__("time").monotonic
        newer = await bootstrap(client)
        assert newer["session_id"] != session
        disposed = await client.post("/api/home-flow/dispose", json={"session_id":newer["session_id"]})
        assert disposed.json()["code"] == "flow_session_disposed"
        assert (await turn(client, request(newer["session_id"], text=PHRASE)))["code"] == "flow_session_unavailable"


@pytest.mark.asyncio
async def test_real_query_scope_and_simulator_mode_preserve_held_qualifications(tmp_path):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        now = datetime.now(UTC)
        why = await turn(client, request(ready["session_id"], operation="why:1", lane="why",
            text="light.hall为什么亮了", device_id="light.hall", start=(now-timedelta(minutes=5)).isoformat(),
            end=now.isoformat()))
        assert why["code"] == "why_bounded_evidence", why
        report = domain(why)
        assert report["query"]["permission_scope"]["requested_entities"] == ["light.hall"]
        assert report["query"]["coverage"] and report["trace_status"]["status"] == "held"
        assert why["nas_lease"] in {"closed", "metadata_only"}
        assert "url" not in json.dumps(report["archive"])
        mode = await turn(client, request(ready["session_id"], operation="mode:1", text="我要睡觉了"))
        assert mode["code"] == "explicit_mode_result" and mode["phase"] == "result", mode
        result = domain(mode)
        assert result["effective"]["mode"] == "sleep"
        assert app.state.rule_runtime.status() == result["runtime"]
        assert mode["output_audibility"] == "unavailable" and mode["native_graph"] == "unavailable"
        assert mode["asr_admission"] == "unavailable"


@pytest.mark.asyncio
async def test_missing_household_timezone_remains_truthful_clarification(tmp_path):
    async with application(tmp_path, timezone=None) as (_, client):
        ready = await bootstrap(client)
        result = await turn(client, request(ready["session_id"], lane="why", text="刚才灯为什么亮了"))
        assert result["phase"] == "clarify"
        assert result["code"] == "household_timezone_required"


@pytest.mark.asyncio
async def test_control_uses_original_action_controller_without_fabricated_simulator_grant(tmp_path):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        result = await turn(client, request(ready["session_id"], operation="control:1", lane="control",
                                           text="打开灯", device_id="light.hall"))
        assert result["phase"] == "refused" and result["code"] == "device_selection_unavailable"
        assert result["approval_digest"] is None
        assert app.state.home_flow.control.ports.controller is app.state.core.actions
        assert not (await app.state.core.snapshot()).action_targets
        assert app.state.core.store.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_actual_app_original_mock_action_preview_live_approval_is_held(tmp_path):
    from test_homeplane_actions import rig

    # All authority and transport data are the existing synthetic fixture in
    # this test's private /tmp directory. No network or household grant is used.
    core, row, _, posts, _, path, _ = rig(tmp_path.resolve())
    row["attributes"]["friendly_name"] = "客厅灯"
    adapter = core.adapter
    core.actions.close()
    core.store.close()
    async with application(tmp_path, adapter=adapter, authority_path=path) as (app, client):
        ready = await bootstrap(client)
        preview = await turn(client, request(ready["session_id"], operation="control:1",
                                            lane="control", text="打开客厅灯"))
        assert preview["phase"] == "preview", preview
        trace = domain(preview)
        assert trace == app.state.core.store.light_action(preview["plan_id"]).model_dump(mode="json")
        assert trace["status"] == "planned" and trace["real_write_attempted"] is False
        held = await turn(client, request(ready["session_id"], operation="control:1", kind="approve",
            expected_context=preview["context_token"], plan_id=preview["plan_id"],
            approval_digest=preview["approval_digest"]))
        assert held["code"] == "flow_live_write_held" and held["phase"] == "unavailable"
        assert not posts
        saved = app.state.core.store.light_action(preview["plan_id"])
        assert saved.status == "planned" and saved.real_write_attempted is False
        assert saved.physically_confirmed is False


@pytest.mark.asyncio
async def test_late_repeated_old_control_cancel_keeps_new_reused_focus_fence(tmp_path, monkeypatch):
    from test_homeplane_actions import rig

    core, row, _, posts, _, path, _ = rig(tmp_path.resolve())
    row["attributes"]["friendly_name"] = "客厅灯"
    adapter = core.adapter
    core.actions.close()
    core.store.close()
    async with application(tmp_path, adapter=adapter, authority_path=path) as (app, client):
        ready = await bootstrap(client)
        session = ready["session_id"]
        first = await turn(client, request(session, operation="control:1", lane="control", text="打开客厅灯"))
        assert first["phase"] == "preview"
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.state.core.snapshot
        snapshots = 0

        async def held():
            nonlocal snapshots
            snapshots += 1
            if snapshots == 2:  # First is old cleanup's original scoped trace read.
                entered.set()
                await release.wait()
            return await original()

        monkeypatch.setattr(app.state.core, "snapshot", held)
        newer = asyncio.create_task(turn(client, request(session, operation="control:2", lane="control", text="打开客厅灯")))
        await asyncio.wait_for(entered.wait(), 2)
        cleanup = app.state.home_flow.sessions[session].operations["control:1"].cancel_task
        assert cleanup.done()
        stopped = await asyncio.wait_for(client.post("/api/home-flow/cancel", json=request(
            session, operation="control:1", kind="cancel", expected_context=first["context_token"])), 2)
        assert stopped.status_code == 200 and stopped.json()["phase"] == "cancelled"
        assert app.state.home_flow.sessions[session].operations["control:1"].cancel_task is cleanup
        assert snapshots == 2, "repeated old cancel cannot reenter original lane cleanup"
        release.set()
        result = await asyncio.wait_for(newer, 2)
        assert result["phase"] == "preview" and result["operation_id"] == "control:2", result
        assert app.state.home_flow._current(app.state.home_flow.sessions[session])
        assert not posts


@pytest.mark.asyncio
async def test_lifespan_closes_flow_and_owned_work_before_shared_store(tmp_path, monkeypatch):
    async with application(tmp_path) as (app, client):
        ready = await bootstrap(client)
        entered = asyncio.Event()
        original = app.state.rule_text.plan

        async def held(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.state.rule_text, "plan", held)
        sending = asyncio.create_task(turn(client, request(ready["session_id"], text=PHRASE)))
        await asyncio.wait_for(entered.wait(), 2)
        disposed = await asyncio.wait_for(client.post("/api/home-flow/dispose", json={
            "session_id":ready["session_id"]}), 2)
        assert disposed.status_code == 200
        assert (await asyncio.wait_for(sending, 2))["phase"] == "cancelled"
        flow = app.state.home_flow
    assert flow.closed and not flow.tasks and not flow.sessions


@pytest.mark.asyncio
async def test_shutdown_owns_pending_bootstrap_snapshot_before_shared_store_close(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    app = create_app(db_path=root/"traces.sqlite", settings_path=root/"settings.json")
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                              headers={"Origin":"http://testserver"})
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = app.state.core.snapshot
    close_store = app.state.core.store.close
    events, closing, sending = [], None, None

    async def noncooperative_snapshot():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        result = await original()
        events.append("snapshot_finished")
        return result

    def recorded_close():
        events.append("store_closed")
        close_store()

    monkeypatch.setattr(app.state.core, "snapshot", noncooperative_snapshot)
    monkeypatch.setattr(app.state.core.store, "close", recorded_close)
    try:
        sending = asyncio.create_task(client.post("/api/home-flow/bootstrap"))
        await asyncio.wait_for(entered.wait(), 2)
        assert len(app.state.home_flow.bootstrap_tasks) == 1
        closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not closing.done() and "store_closed" not in events
        release.set()
        response = await asyncio.wait_for(sending, 2)
        await asyncio.wait_for(closing, 2)
        assert response.status_code == 200
        assert response.json()["phase"] == "unavailable"
        assert response.json()["code"] == "flow_context_changed"
        assert events == ["snapshot_finished", "store_closed"]
        assert app.state.home_flow.closed
        assert not app.state.home_flow.tasks and not app.state.home_flow.bootstrap_tasks
        assert not app.state.home_flow.sessions
    finally:
        release.set()
        if sending and not sending.done():
            await asyncio.wait_for(sending, 2)
        if closing:
            await asyncio.wait_for(closing, 2)
        else:
            await lifespan.__aexit__(None, None, None)
        await client.aclose()


@pytest.mark.asyncio
async def test_reviewed_widget_consumes_actual_application_http_flow(tmp_path):
    from test_homeplane_home_conversation_widget import HARNESS, NODE

    assert NODE is not None, "existing Node required for this finite component check"
    async with application(tmp_path) as (app, client):
        manager = app.state.rules
        old = manager.backend.records()[0]
        malicious = '<img src=x onerror="SIMULATED-untrusted-label">'
        extra = await manager.backend.create_disabled("SIMULATED-choice-two", old.rule.model_copy(update={
            "id":"SIMULATED-choice-two", "intent":malicious}))
        await manager.backend.toggle(extra.id, extra.fingerprint, True)
        config = dict(bootstrap=[await bootstrap(client), await bootstrap(client)], phrase=PHRASE,
                      malicious=malicious, selected_rule=extra.id)
        script = tmp_path / "actual-app-component.mjs"
        # The reviewed F2 harness predates F4's explicit report/feedback/output
        # envelope. Unwrap only its raw evidence assertion, leaving widget and
        # actual HTTP responses byte-for-byte unchanged.
        script.write_text(HARNESS.replace(
            "const currentPre = () => root().querySelectorAll('pre').at(-1)?.textContent ?? '';",
            "const currentPre = () => { const raw=root().querySelectorAll('pre').at(-1)?.textContent; "
            "if(!raw)return ''; const body=JSON.parse(raw); return JSON.stringify(body.report ?? body); };"))
        process = await asyncio.create_subprocess_exec(NODE, str(script),
            str(Path(__file__).parents[1] / "homeplane/static/home-conversation.js"), json.dumps(config),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH":"/opt/homebrew/bin:/usr/bin:/bin", "TMPDIR":"/tmp"})
        reports, result = [], None
        try:
            async with asyncio.timeout(15):
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if message["type"] == "failure":
                        pytest.fail(message["error"])
                    if message["type"] == "done":
                        result = message
                        break
                    if message["type"] == "invalidate":
                        response = (await client.post("/api/home-flow/dispose", json={
                            "session_id":message["session_id"]})).json()
                    else:
                        body = message["request"]
                        path = "/api/home-flow/cancel" if body["kind"] == "cancel" else "/api/home-flow/turn"
                        actual = await client.post(path, json=body)
                        assert actual.status_code == 200, actual.text
                        response = actual.json()
                        reports.append((body, response))
                    process.stdin.write((json.dumps({"bridge_id":message["bridge_id"],
                                                    "response":response})+"\n").encode())
                    await process.stdin.drain()
                assert result is not None
                assert await process.wait() == 0, (await process.stderr.read()).decode()
            assert result["html_writes"] == 0 and "source_late" in result["checked"]
            approved = [(body, report) for body, report in reports if body["kind"] == "approve"]
            assert len(approved) == 1 and domain(approved[0][1])["status"] == "verified"
            assert manager.get(approved[0][0]["plan_id"]).status == "rolled_back"
            assert all(report["output_audibility"] == "unavailable" for _, report in reports)
            evidence = Path(__file__).parents[2] / "evidence" / "actual-http-widget.json"
            evidence.parent.mkdir(exist_ok=True)
            evidence.write_text(json.dumps({"qualification":"local Simulator; HTTP+DOM, no browser/device qualification",
                "checked":result["checked"], "html_writes":0,
                "requests_responses":[{"request":body,"response":report} for body,report in reports]},
                ensure_ascii=False, indent=2)+"\n")
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
