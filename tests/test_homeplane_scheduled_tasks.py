"""Scheduled task proposals and clock math; no hardware or real model."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from homeplane.rule_contracts import RuleIR
from homeplane.task_composition import is_task_request
from tests.test_homeplane_home_flow import domain, request, turn
from tests.test_homeplane_task_composition import TaskRuntime, task_app


@pytest.mark.parametrize(
    "text", ["每天22点关闭灯", "每晚十点关闭灯", "每周一三五20:00打开灯", "工作日早上七点打开灯"]
)
def test_time_requests_enter_the_task_lane_before_single_device_control(text):
    assert is_task_request(text)


def scheduled_rule(**changes):
    trigger = dict(type="schedule", at="22:00", weekdays=[1, 2, 3, 4, 5, 6, 7], timezone="Asia/Shanghai")
    trigger.update(changes)
    return RuleIR.model_validate(
        dict(
            id="SYNTHETIC-timer",
            revision=1,
            intent="SYNTHETIC scheduled lighting",
            trigger=trigger,
            actions=[
                dict(
                    type="device.set",
                    id="one",
                    target="SYNTHETIC-target",
                    operation="light.set",
                    desired={"power": "on"},
                )
            ],
            execution={"preferred_backend": "home_core"},
        )
    )


@pytest.mark.parametrize(
    "fault",
    [
        dict(at="25:00"),
        dict(weekdays=[]),
        dict(weekdays=[1, 1]),
        dict(weekdays=[True]),
        dict(timezone="missing/invalid"),
        dict(timezone=""),
    ],
)
def test_unknown_timezone_or_ambiguous_weekdays_are_refused(fault):
    with pytest.raises(ValidationError):
        scheduled_rule(**fault)


@pytest.mark.asyncio
async def test_daily_plan_uses_family_timezone_and_original_non_actuating_approval(tmp_path):
    model = TaskRuntime()

    def schedule(draft, data):
        draft["title"] = "工作日灯光安排"
        draft["trigger"] = dict(type="schedule", at="22:00", weekdays=[1, 2, 3, 4, 5])
        return draft

    model.mutate = schedule
    async with task_app(tmp_path, model) as (app, client, calls, session):
        shown = await turn(
            client,
            request(
                session, operation="task:one", text="创建任务：每个工作日22点先开灯，等50毫秒，再开插座。"
            ),
        )
        assert shown["phase"] == "preview", shown
        result = domain(shown)
        rule = result["plan"]["new"]["rule"]
        assert rule["trigger"] == dict(
            schema_version=1, type="schedule", at="22:00", weekdays=[1, 2, 3, 4, 5], timezone="Asia/Shanghai"
        )
        assert result["task_review"]["trigger_kind"] == "schedule"
        assert "22:00" in result["task_review"]["when"] and "周一至周五" in result["task_review"]["when"]
        assert result["plan"]["status"] == "awaiting_approval"
        assert result["plan"]["trial"]["simulated"] and not result["plan"]["trial"]["real_write_attempted"]
        assert not calls and not app.state.core_rules.records()


@pytest.mark.parametrize(
    "zone,clock,now,expected",
    [
        ("Asia/Shanghai", "22:00", "2026-11-09T14:00:01+00:00", "2026-11-09"),
        ("Asia/Shanghai", "22:00", "2026-11-09T14:00:04+00:00", None),
        ("America/New_York", "01:30", "2026-11-01T05:30:01+00:00", "2026-11-01"),
        ("America/New_York", "01:30", "2026-11-01T06:30:01+00:00", None),
        ("America/New_York", "02:30", "2026-03-08T07:30:01+00:00", None),
    ],
)
def test_only_the_original_due_instant_is_eligible_without_dst_replay(zone, clock, now, expected):
    from homeplane.rule_schedule import due_occurrence

    trigger = scheduled_rule(timezone=zone, at=clock).trigger
    result = due_occurrence(trigger, datetime.fromisoformat(now), grace_seconds=3)
    assert (result.day if result else None) == expected


def test_next_occurrence_skips_nonexistent_local_time_and_uses_only_first_fold():
    from homeplane.rule_schedule import next_occurrence

    trigger = scheduled_rule(timezone="America/New_York", at="02:30").trigger
    assert next_occurrence(trigger, datetime(2026, 3, 8, 5, tzinfo=UTC)).at == datetime(
        2026, 3, 9, 6, 30, tzinfo=UTC
    )
    trigger = scheduled_rule(timezone="America/New_York", at="01:30").trigger
    assert next_occurrence(trigger, datetime(2026, 11, 1, 6, tzinfo=UTC)).at == datetime(
        2026, 11, 2, 6, 30, tzinfo=UTC
    )


@pytest.mark.parametrize("text", ["每天开灯多久", "工作日能耗多少", "今天灯关闭了吗"])
def test_recurring_status_questions_do_not_become_device_tasks(text):
    assert not is_task_request(text)


@pytest.mark.parametrize("text", ["每天晚上十点把灯关了", "今晚22点关闭灯好吗", "明天7点把灯打开"])
def test_time_qualified_instructions_never_fall_back_to_immediate_control(text):
    assert is_task_request(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timezone", "weekdays", "time", "missing_zone", "delay"])
async def test_incomplete_or_unapproved_clock_scope_never_partially_creates_a_rule(tmp_path, fault):
    model = TaskRuntime()

    def schedule(draft, data):
        draft["trigger"] = dict(type="schedule", at="22:00", weekdays=[1, 2, 3, 4, 5])
        if fault == "timezone":
            draft["trigger"]["timezone"] = "UTC"
        if fault == "weekdays":
            draft["trigger"]["weekdays"] = [1, 1]
        if fault == "time":
            draft["trigger"]["at"] = "25:00"
        if fault == "delay":
            draft["steps"][1]["milliseconds"] = 30000
        return draft

    model.mutate = schedule
    async with task_app(
        tmp_path, model, household_timezone=None if fault == "missing_zone" else "Asia/Shanghai"
    ) as (app, client, calls, session):
        shown = await turn(
            client, request(session, operation="task:one", text="创建每日定时任务：按完整方案开灯并开插座。")
        )
        assert shown["phase"] != "preview"
        assert not calls and not app.state.core_rules.records()


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["America/New_York", None, "missing/invalid"])
async def test_manual_context_reads_only_a_verified_family_timezone(tmp_path, zone):
    async with task_app(tmp_path, household_timezone=None) as (app, client, calls, _):

        async def timezone():
            return zone

        app.state.core.adapter.household_timezone = timezone
        result = await client.get("/api/rules/core/context")
        assert result.status_code == 200
        assert result.json()["household_timezone"] == (
            "America/New_York" if zone == "America/New_York" else None
        )
        assert not calls


@pytest.mark.asyncio
async def test_foreign_clock_payload_and_registered_ha_edge_cannot_trigger_a_timer(tmp_path):
    from homeplane.contracts import Event
    from homeplane.rule_contracts import ScheduleTrigger
    from homeplane.rule_schedule import ClockOccurrence, clock_payload
    from tests.test_homeplane_core_rules import cleanup, enable, event, fixture, proposal

    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    try:
        request = await proposal(core, manager)
        now = datetime.now(UTC)
        at = now.replace(second=0, microsecond=0)
        trigger = ScheduleTrigger(at=at.strftime("%H:%M"), timezone="UTC")
        plan = await enable(
            manager, request.model_copy(update={"rule": request.rule.model_copy(update={"trigger": trigger})})
        )
        fake = Event(
            source="friday.core.clock",
            kind="source-report",
            correlation_id=plan.new.rule.id,
            wall_time=at,
            monotonic_time=__import__("time").monotonic(),
            boot_id=core.adapter._boot,
            source_seq="SIMULATED-connection:foreign",
            payload=clock_payload(trigger, ClockOccurrence(at.date().isoformat(), at)),
        )
        core.adapter._events.append(fake)
        result = await runtime.ingest(core.adapter, fake)
        assert result["accepted"] is False
        await runtime.ingest(core.adapter, event(core))
        assert not calls and not runtime.pending
    finally:
        await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_clock_discontinuity_retires_the_pending_epoch_until_recovery(tmp_path):
    from tests.test_homeplane_core_rules import cleanup, fixture

    core, _, calls, _, _, _, owner, _, runtime = await fixture(tmp_path)
    try:
        clock = runtime.clock
        now = datetime(2026, 11, 9, tzinfo=UTC)
        assert clock.sample(now, 100)
        original = clock.epoch
        assert not clock.sample(now + timedelta(minutes=10), 101)
        assert clock.epoch is not original
        assert not clock.sample(now + timedelta(minutes=10, seconds=10), 111)
        assert clock.sample(now + timedelta(minutes=10, seconds=31), 132)
        assert not calls
    finally:
        await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_confirmed_task_execution_window_can_contain_its_requested_delay(tmp_path):
    model = TaskRuntime()

    def delayed(draft, data):
        draft["steps"][1]["milliseconds"] = 5000
        return draft

    model.mutate = delayed
    async with task_app(tmp_path, model) as (app, client, calls, session):
        shown = await turn(
            client, request(session, operation="task:one", text="每当门磁打开，开灯，等待5秒，再打开插座。")
        )
        assert shown["phase"] == "preview", shown
        rule = domain(shown)["plan"]["new"]["rule"]
        assert rule["execution"]["max_event_age_ms"] > sum(a.get("milliseconds", 0) for a in rule["actions"])
        assert not calls


@pytest.mark.asyncio
async def test_normal_health_wake_does_not_look_like_a_clock_jump(tmp_path):
    from tests.test_homeplane_core_rules import cleanup, fixture

    core, _, calls, _, _, _, owner, _, runtime = await fixture(tmp_path)
    try:
        start = datetime(2026, 11, 9, tzinfo=UTC)
        assert runtime.clock.sample(start, 100)
        assert runtime.clock.sample(start + timedelta(seconds=30.1), 130.1)
        assert not calls
    finally:
        await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_clock_occurrence_stays_consumed_after_run_pruning_and_restart(tmp_path, monkeypatch):
    from homeplane import core_rules, core_schedule
    from homeplane.core_rules import CoreRuleRuntime
    from homeplane.rule_contracts import ScheduleTrigger
    from tests.test_homeplane_core_rules import cleanup, enable, fixture, proposal

    core, _, calls, _, _, _, owner, manager, runtime = await fixture(tmp_path)
    try:
        proposed = await proposal(core, manager)
        due = datetime.now(UTC).replace(second=0, microsecond=0)
        trigger = ScheduleTrigger(at=due.strftime("%H:%M"), timezone="UTC")
        proposed = proposed.model_copy(update={"rule": proposed.rule.model_copy(update={"trigger": trigger})})
        await enable(manager, proposed)

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return (due + timedelta(seconds=1)).astimezone(tz or UTC)

        monkeypatch.setattr(core_schedule, "datetime", Frozen)
        monkeypatch.setattr(core_rules, "datetime", Frozen)
        await runtime.clock.poll()
        assert core.store.connection.execute("SELECT count(*) FROM core_rule_runs").fetchone()[0] == 1
        assert core.store.connection.execute("SELECT count(*) FROM core_clock_slots").fetchone()[0] == 1
        assert not calls and not runtime.pending
        await runtime.clock.poll()
        assert core.store.connection.execute("SELECT count(*) FROM core_rule_runs").fetchone()[0] == 1
        with core.store.connection:
            core.store.connection.execute("DELETE FROM core_rule_runs")
        await runtime.close()
        runtime = CoreRuleRuntime(manager)
        await runtime.clock.poll()
        assert core.store.connection.execute("SELECT count(*) FROM core_rule_runs").fetchone()[0] == 0
        assert not calls and not runtime.pending
    finally:
        await cleanup(core, owner, runtime)


@pytest.mark.asyncio
async def test_approved_daily_task_runs_on_real_clock_after_console_disposal(tmp_path):
    from homeplane.core_rules import CoreRuleRuntime
    from tests.test_homeplane_task_composition import approve

    now = datetime.now(UTC)
    due = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if (due - now).total_seconds() < 5:
        due += timedelta(minutes=1)
    model = TaskRuntime()

    def schedule(draft, data):
        draft["trigger"] = dict(
            type="schedule",
            at=due.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M"),
            weekdays=list(range(1, 8)),
        )
        draft["title"] = "每天按时安排灯光"
        draft["steps"][1]["milliseconds"] = 5000
        return draft

    model.mutate = schedule
    async with task_app(tmp_path, model) as (app, client, calls, session):
        shown = await turn(
            client,
            request(session, operation="task:one", text="创建每天定时任务：先开灯，等5秒，再开插座。"),
        )
        assert shown["phase"] == "preview", shown
        done = await turn(client, approve(session, shown))
        assert domain(done)["plan"]["status"] == "verified"
        await client.post("/api/home-flow/dispose", json={"session_id": session})
        runtime = app.state.core_rule_runtime
        CoreRuleRuntime.start(runtime)
        selected = False
        while datetime.now(UTC) < due + timedelta(seconds=10) and (len(calls) < 2 or runtime.pending):
            now = datetime.now(UTC)
            stream = app.state.core.adapter._stream
            stream.receipts = {key: now for key in stream.rows}
            stream.published_receipts = dict(stream.receipts)
            if not selected and (due - now).total_seconds() < 1:
                await app.state.home_experience.household_mode.select("awake")
                selected = True
            await asyncio.sleep(0.02 if selected else min(0.5, max(0.02, (due - now).total_seconds() - 1)))
        assert [c.url.path for c in calls] == [
            "/api/services/light/turn_on",
            "/api/services/switch/turn_on",
        ], runtime.status()
        assert runtime.status()["runs"][0]["status"] == "completed"
        await runtime.tick()
        assert len(calls) == 2 and not runtime.pending
        assert len(model.calls) == 1
