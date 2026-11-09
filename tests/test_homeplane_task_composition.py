"""Actual Console -> Core task approval -> registered synthetic event -> mock IO."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeplane.api import create_app
from homeplane.arl import current_model_task
from tests.test_homeplane_core_rules import event, fixture
from tests.test_homeplane_home_flow import bootstrap, domain, request, turn
from tests.test_household_timezone_wiring import OfflineARL

TEXT = "每当门磁从关闭变为打开，先打开灯，等待50毫秒，再打开插座。"


class TaskRuntime(OfflineARL):
    def __init__(self):
        self.calls = []
        self.mutate = lambda draft, data: draft

    async def complete_scenario(self, scenario, prompt, **kwargs):
        intent = current_model_task()
        self.calls.append((scenario, prompt, kwargs, intent))
        assert intent.task_kind == "household.plan" and intent.current() is True
        assert intent.requested_class == "interactive" and len(kwargs["request_id"]) == 64
        data = json.loads(prompt.split("\n", 1)[1])
        by_kind = {e["kind"]: e for e in data["catalogue"]}
        draft = dict(
            kind="task",
            title="开门后的灯光安排",
            trigger=dict(
                type="state_transition",
                entity=by_kind["binary_sensor"]["ref"],
                capability="binary_sensor.state",
                from_value="off",
                to_value="on",
            ),
            conditions=[],
            steps=[
                dict(
                    type="device",
                    entity=by_kind["light"]["ref"],
                    operation="light.set",
                    desired={"power": "on"},
                ),
                dict(type="delay", milliseconds=50),
                dict(
                    type="device",
                    entity=by_kind["switch"]["ref"],
                    operation="switch.set",
                    desired={"power": "on"},
                ),
            ],
        )
        return dict(
            text=json.dumps(self.mutate(draft, data), ensure_ascii=False),
            simulated=False,
            native_acknowledgements_verified=True,
            device_authority=False,
            application_admission=dict(scenario_id="home_planning", package_id="high"),
        )


@asynccontextmanager
async def task_app(tmp_path, runtime=None, *, household_timezone="Asia/Shanghai"):
    core, _, calls, _, path, _, owner, _, old_runtime = await fixture(tmp_path)
    adapter = core.adapter
    await old_runtime.close()
    owner.close()
    core.actions.close()
    core.store.close()
    app = create_app(
        adapter=adapter,
        authority_path=path,
        db_path=tmp_path.resolve() / "task.sqlite",
        settings_path=tmp_path.resolve() / "settings.json",
        arl_runtime=runtime or TaskRuntime(),
        flow_household_timezone=household_timezone,
        background_enabled=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client:
            ready = await bootstrap(client)
            yield app, client, calls, ready["session_id"]


def approve(session, shown, **changes):
    return request(
        session,
        operation="task:one",
        kind="approve",
        plan_id=shown["plan_id"],
        approval_digest=shown["approval_digest"],
        expected_context=shown["context_token"],
        **changes,
    )


@pytest.mark.asyncio
async def test_natural_task_preview_approval_survives_console_disposal_and_runs_original_background(tmp_path):
    model = TaskRuntime()
    async with task_app(tmp_path, model) as (app, client, calls, session):
        shown = await turn(client, request(session, operation="task:one", text=TEXT))
        assert shown["phase"] == "preview", shown
        payload = domain(shown)
        plan = payload["plan"]
        assert plan["old"] is None and plan["status"] == "awaiting_approval"
        assert [a["type"] for a in plan["new"]["rule"]["actions"]] == ["device.set", "delay", "device.set"]
        assert len(payload["task_review"]["steps"]) == 3 and "SIMULATED" in payload["task_review"]["when"]
        assert not app.state.core_rules.records() and not calls
        done = await turn(client, approve(session, shown))
        applied = domain(done)["plan"]
        assert applied["status"] == "verified" and applied["real_write_attempted"] is False
        assert not calls and len(model.calls) == 1
        data = json.loads(model.calls[0][1].split("\n", 1)[1])
        assert set(data) == {"task", "catalogue", "timezone", "reference_time"}
        assert data["timezone"] == "Asia/Shanghai"
        assert datetime.fromisoformat(data["reference_time"]).utcoffset() == timedelta(hours=8)
        assert "binary_sensor.simulated_contact" not in model.calls[0][1]
        assert not any("bindings" in e or "state" in e or "authority" in e for e in data["catalogue"])
        assert all(e["ref"].startswith("e") for e in data["catalogue"])
        await client.post("/api/home-flow/dispose", json={"session_id": session})
        assert app.state.core_rules.read(applied["new"]["id"]).enabled
        await app.state.home_experience.household_mode.select("awake")
        runtime = app.state.core_rule_runtime
        await runtime.ingest(app.state.core.adapter, event(app.state.core))
        assert len(calls) == 1 and runtime.pending
        for _ in range(30):
            await asyncio.sleep(0.01)
            await runtime.tick()
            if not runtime.pending:
                break
        assert [c.url.path for c in calls] == ["/api/services/light/turn_on", "/api/services/switch/turn_on"]
        run = runtime.get(runtime.status()["runs"][0]["id"])
        assert run.status == "completed" and run.real_write_attempted and not run.physically_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["entity", "operation", "unit", "safety", "partial", "duplicate", "protected_mode"]
)
async def test_unbound_or_incomplete_model_proposal_never_creates_a_plan(tmp_path, fault):
    model = TaskRuntime()

    def mutate(draft, data):
        if fault == "entity":
            draft["steps"][0]["entity"] = "e999"
        if fault == "operation":
            draft["steps"][0]["operation"] = "shell.exec"
        if fault == "safety":
            draft["safety"] = {"respect_sleep": False}
        if fault == "protected_mode":
            draft["conditions"] = [dict(type="mode", mode="home.sleep", equals=True)]
        if fault == "unit":
            draft["conditions"] = [
                dict(
                    type="numeric",
                    entity=draft["trigger"]["entity"],
                    capability="temperature",
                    operator="gt",
                    value=30,
                    unit="°C",
                )
            ]
        if fault == "partial":
            return dict(kind="unsupported", title="只打开灯", steps=draft["steps"])
        return draft

    model.mutate = mutate
    original = model.complete_scenario
    if fault == "duplicate":

        async def duplicate(*args, **kwargs):
            value = await original(*args, **kwargs)
            value["text"] = '{"kind":"task","kind":"unsupported"}'
            return value

        model.complete_scenario = duplicate
    async with task_app(tmp_path, model) as (app, client, calls, session):
        report = await turn(client, request(session, operation="task:one", text=TEXT))
        assert report["phase"] in {"unavailable", "refused", "clarify"}, report
        assert len(model.calls) == 1
        assert not app.state.core_rules.records() and not calls
        assert (
            app.state.core.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0] == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"simulated": True},
        {"native_acknowledgements_verified": False},
        {"device_authority": True},
        {"application_admission": dict(scenario_id="voice_intent", package_id="low")},
    ],
)
async def test_unverified_model_receipt_cannot_create_or_enable_task(tmp_path, change):
    model = TaskRuntime()
    original = model.complete_scenario

    async def bad(*args, **kwargs):
        return {**await original(*args, **kwargs), **change}

    model.complete_scenario = bad
    async with task_app(tmp_path, model) as (app, client, calls, session):
        report = await turn(client, request(session, operation="task:one", text=TEXT))
        assert report["phase"] in {"unavailable", "refused"}, report
        assert len(model.calls) == 1
        assert not app.state.core_rules.records() and not calls


@pytest.mark.asyncio
async def test_cancelled_late_model_never_creates_or_enables_task(tmp_path):
    model = TaskRuntime()
    original = model.complete_scenario
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def late(*args, **kwargs):
        value = await original(*args, **kwargs)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return value

    model.complete_scenario = late
    async with task_app(tmp_path, model) as (app, client, calls, session):
        pending = asyncio.create_task(turn(client, request(session, operation="task:one", text=TEXT)))
        stop = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stop = asyncio.create_task(
                client.post(
                    "/api/home-flow/cancel", json=request(session, operation="task:one", kind="cancel")
                )
            )
            await asyncio.wait_for(cancelled.wait(), 2)
            release.set()
            await asyncio.wait_for(asyncio.gather(pending, stop), 3)
            assert not app.state.core_rules.records() and not calls
            assert (
                app.state.core.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0]
                == 0
            )
        finally:
            release.set()
            await asyncio.gather(pending, *([stop] if stop else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_changed_source_before_model_return_or_approval_cannot_enable_task(tmp_path):
    model = TaskRuntime()
    async with task_app(tmp_path, model) as (app, client, calls, session):
        shown = await turn(client, request(session, operation="task:one", text=TEXT))
        assert shown["phase"] == "preview", shown
        grant, _ = app.state.core.actions.authority()
        app.state.core.actions.authority_path.write_text(
            grant.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(days=2)}).model_dump_json()
        )
        done = await turn(client, approve(session, shown))
        assert done["phase"] in {"cancelled", "unavailable", "refused"}, done
        assert not app.state.core_rules.records() and not calls


@pytest.mark.asyncio
async def test_new_task_can_be_disabled_using_same_original_approval_record(tmp_path):
    async with task_app(tmp_path) as (app, client, calls, session):
        shown = await turn(client, request(session, operation="task:one", text=TEXT))
        assert shown["phase"] == "preview", shown
        done = await turn(client, approve(session, shown))
        plan = domain(done)["plan"]
        rolled = await turn(
            client,
            request(
                session,
                operation="task:one",
                kind="rollback",
                plan_id=plan["id"],
                expected_context=done["context_token"],
            ),
        )
        assert domain(rolled)["plan"]["status"] == "rolled_back"
        assert not app.state.core_rules.read(plan["new"]["id"]).enabled and not calls


@pytest.mark.asyncio
async def test_planning_does_not_hold_application_lock_and_registry_rebinding_rejects_late_draft(tmp_path):
    model = TaskRuntime()
    original = model.complete_scenario
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        value = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return value

    model.complete_scenario = held
    async with task_app(tmp_path, model) as (app, client, calls, session):
        pending = asyncio.create_task(turn(client, request(session, operation="task:one", text=TEXT)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            ready = await asyncio.wait_for(bootstrap(client), 1)
            assert ready["session_id"] != session and not pending.done()
            app.state.core.adapter._registry["light.simulated"]["device_id"] = "SIMULATED-rebound-task"
            release.set()
            report = await asyncio.wait_for(pending, 2)
            assert report["phase"] in {"cancelled", "refused", "unavailable"}, report
            assert not app.state.core_rules.records() and not calls
            assert (
                app.state.core.store.connection.execute("SELECT count(*) FROM core_rule_plans").fetchone()[0]
                == 0
            )
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarify", "unsupported"])
async def test_model_abstention_is_clear_and_never_becomes_partial_task(tmp_path, kind):
    model = TaskRuntime()
    model.mutate = lambda draft, data: dict(kind=kind)
    async with task_app(tmp_path, model) as (app, client, calls, session):
        report = await turn(client, request(session, operation="task:one", text=TEXT))
        assert report["phase"] == ("clarify" if kind == "clarify" else "unavailable"), report
        assert not report["approval_digest"] and len(model.calls) == 1
        assert not app.state.core_rules.records() and not calls


@pytest.mark.asyncio
async def test_numeric_one_stays_numeric_and_household_time_window_is_server_owned(tmp_path):
    model = TaskRuntime()

    def mutate(draft, data):
        draft["steps"][0]["desired"]["brightness"] = 1
        draft["conditions"] = [dict(type="time_window", start="00:00", end="23:59")]
        return draft

    model.mutate = mutate
    async with task_app(tmp_path, model) as (app, client, calls, session):
        shown = await turn(client, request(session, operation="task:one", text=TEXT))
        assert shown["phase"] == "preview", shown
        projected = domain(shown)
        assert "亮度设为1" in projected["task_review"]["steps"][0]
        assert projected["plan"]["new"]["rule"]["conditions"][0]["timezone"] == "Asia/Shanghai"
        assert not app.state.core_rules.records() and not calls


@pytest.mark.asyncio
async def test_original_http_task_receipts_render_review_enable_and_disable_in_current_widget(tmp_path):
    import subprocess
    from pathlib import Path

    from tests.test_homeplane_home_conversation_widget import HARNESS, NODE

    if NODE is None:
        pytest.skip("Existing Node unavailable for offline DOM check")
    script = (
        HARNESS.split("const config = JSON.parse(process.argv[3]);")[0]
        + r"""
const config=JSON.parse(process.argv[3]);
const root=document.createElement('main');document.body.append(root);
let projected=config.preview;const calls=[];
const client={async send(r){calls.push(r);return {...projected,session_id:r.session_id,turn_id:r.turn_id,operation_id:r.operation_id};},async cancel(r){return {...r,phase:'cancelled',code:'synthetic_cancel'};}};
const widget=HomeConversation.mount(root,{sourceToken:'SIMULATED-task-review',client});
try{
 widget.update({sourceToken:'SIMULATED-task-review',response:config.bootstrap});
 root.querySelector('[data-hc="input"]').value='每当门磁打开，安排灯光';
 root.querySelector('form').fire('submit');await new Promise(r=>setImmediate(r));
 const plan=root.querySelector('.hc-plan');
 assert(plan.textContent.includes(config.preview.domain.report.task_review.when));
 assert.equal(plan.querySelectorAll('.hc-retain').length,3);
 assert(!plan.textContent.includes('undefined')&&!plan.textContent.includes('ha:'));
 const enable=root.querySelector('[data-hc="approve"]');assert.equal(enable.textContent,'启用任务');
 projected=config.done;enable.fire('click');await new Promise(r=>setImmediate(r));
 assert(root.textContent.includes('后台任务已启用'));
 const disable=root.querySelector('[data-hc="rollback"]');assert.equal(disable.textContent,'停用任务');
 projected=config.rolled;disable.fire('click');await new Promise(r=>setImmediate(r));
 assert(root.textContent.includes('后台任务已停用'));
 assert(calls.find(r=>r.kind==='rollback').expected_context===config.done.context_token);
 assert.equal(root.querySelector('[data-hc="rollback"]'),null);
}finally{widget.dispose();}
"""
    )
    async with task_app(tmp_path) as (app, client, calls, session):
        shown = await turn(client, request(session, operation="task:one", text=TEXT))
        assert shown["phase"] == "preview", shown
        done = await turn(client, approve(session, shown))
        rolled = await turn(
            client,
            request(
                session,
                operation="task:one",
                kind="rollback",
                plan_id=shown["plan_id"],
                expected_context=done["context_token"],
            ),
        )
        config = dict(
            bootstrap=(await client.post("/api/home-flow/context", json={"session_id": session})).json(),
            preview=shown,
            done=done,
            rolled=rolled,
        )
        path = tmp_path / "task-widget.mjs"
        path.write_text(script)
        result = subprocess.run(
            [
                NODE,
                str(path),
                str(Path(__file__).parents[1] / "homeplane/static/home-conversation.js"),
                json.dumps(config),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert not calls
