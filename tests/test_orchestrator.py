import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homeplane_interaction_sessions import admitted, bound

from friday.config import Settings
from friday.core import Evidence
from friday.home import Action, Entity, capabilities, validate_action
from friday.journal import Journal
from friday.orchestrator import Orchestrator, state_matches


class Home:
    def __init__(self):
        services = {"light": {"turn_on": {}, "turn_off": {}}, "lock": {"lock": {}, "unlock": {}}}
        self.entities = {
            "light.living": Entity(
                id="light.living",
                name="客厅灯",
                room="客厅",
                domain="light",
                state="off",
                attributes={"brightness": 100},
                capabilities=capabilities("light", {"supported_color_modes": ["brightness"]}, services),
            ),
            "light.study": Entity(
                id="light.study",
                name="书房灯",
                room="书房",
                domain="light",
                state="on",
                attributes={"brightness": 255},
                capabilities=capabilities("light", {"supported_color_modes": ["brightness"]}, services),
            ),
            "lock.front": Entity(
                id="lock.front",
                name="大门",
                domain="lock",
                state="locked",
                capabilities=capabilities("lock", {}, services),
            ),
            "sensor.temperature": Entity(id="sensor.temperature", name="温度", domain="sensor", state="26"),
        }
        self.calls = []
        self.fail_after_write = False

    async def discover(self):
        return list(self.entities.values())

    async def read(self, eid):
        entity = self.entities[eid]
        return {"state": entity.state, "attributes": copy.deepcopy(entity.attributes)}

    async def execute(self, action):
        validate_action(action, self.entities)
        self.calls.append(action)
        if self.fail_after_write:
            raise TimeoutError()
        entity = self.entities[action.entity_id]
        entity.state = (
            "unlocked" if action.service == "unlock" else "on" if action.service == "turn_on" else "off"
        )
        if "brightness_pct" in action.parameters:
            entity.attributes["brightness"] = round(action.parameters["brightness_pct"] / 100 * 255)


class Planner:
    def __init__(self, plan):
        self.value = plan
        self.calls = 0
        self.on_plan = None

    async def plan(self, context, schema):
        self.calls += 1
        if self.on_plan:
            self.on_plan()
        return self.value


class Decision:
    async def classify(self, text):
        return {"label": "complex", "confidence": 0.99}


@pytest.fixture
def setup(tmp_path):
    journal = Journal(tmp_path / "log.sqlite")
    home = Home()
    planner = Planner(
        {
            "summary": "调整两个房间",
            "steps": [
                {"entity_id": "light.living", "service": "turn_on"},
                {"entity_id": "light.study", "service": "turn_on", "parameters": {"brightness_pct": 30}},
            ],
        }
    )
    life = SimpleNamespace(running=True, generation=1)
    orch = Orchestrator(Settings(mode="live"), home, planner, Decision(), life, journal,
                        allow_simulated_admission=True)
    yield orch, home, planner, life
    journal.close()


async def handle(orch, text="调整客厅和书房"):
    return await orch.handle(text, str(uuid4()), 1)


async def test_complex_plan_two_devices_verified(setup):
    orch, home, _, _ = setup
    result = await handle(orch)
    assert result["status"] == "verified"
    assert len(home.calls) == 2
    assert home.entities["light.study"].attributes["brightness"] == 76
    assert all("调整两个房间" not in str(e) for e in orch.journal.events())


async def test_invalid_later_step_rejects_whole_plan(setup):
    orch, home, planner, _ = setup
    planner.value["steps"][1]["parameters"]["brightness_pct"] = 999
    assert (await handle(orch))["status"] == "error"
    assert not home.calls


async def test_unknown_entity_or_service_rejected(setup):
    orch, home, planner, _ = setup
    planner.value["steps"][1]["entity_id"] = "light.unknown"
    assert (await handle(orch))["status"] == "error"
    assert not home.calls


async def test_unavailable_target_rejects_before_any_write(setup):
    orch, home, _, _ = setup
    home.entities["light.study"].state = "unavailable"
    assert (await handle(orch))["status"] == "error"
    assert not home.calls


async def test_condition_uses_fresh_state(setup):
    orch, home, planner, _ = setup
    planner.value["steps"][0]["conditions"] = [
        {"entity_id": "sensor.temperature", "operation": "gt", "value": 28}
    ]
    result = await handle(orch)
    assert result["steps"][0]["status"] == "skipped"
    assert [a.entity_id for a in home.calls] == ["light.study"]


async def test_pause_while_planning_prevents_execution(setup):
    orch, home, planner, life = setup
    planner.on_plan = lambda: setattr(life, "generation", 2)
    assert (await handle(orch))["status"] == "ignored"
    assert not home.calls


async def test_risky_plan_needs_confirmation_and_state_change_invalidates(setup):
    orch, home, planner, _ = setup
    planner.value = {"summary": "打开大门", "steps": [{"entity_id": "lock.front", "service": "unlock"}]}
    result = await handle(orch)
    assert result["status"] == "confirmation_required"
    assert not home.calls
    home.entities["lock.front"].state = "unlocked"
    assert (await orch.confirm(result["plan_id"]))["status"] == "cancelled"
    assert not home.calls


async def test_confirmation_only_once(setup):
    orch, home, planner, _ = setup
    planner.value = {"summary": "开门", "steps": [{"entity_id": "lock.front", "service": "unlock"}]}
    result = await handle(orch)
    assert (await orch.confirm(result["plan_id"]))["status"] == "verified"
    with pytest.raises(ValueError):
        await orch.confirm(result["plan_id"])
    assert len(home.calls) == 1


async def test_unknown_write_result_stops_without_retry(setup):
    orch, home, _, _ = setup
    home.fail_after_write = True
    assert (await handle(orch))["status"] == "unverified"
    assert len(home.calls) == 1


async def test_ungrounded_pointing_does_not_reach_llm(setup):
    orch, home, planner, _ = setup
    evidence = Evidence(
        live_speaker=True,
        people_count=1,
        pointing=True,
        attention=0.99,
        target_id="unknown",
        target_confidence=0.99,
    )
    binding = bound(camera_id="text", session_id="text")
    result = await orch.handle("把这个打开", str(uuid4()), 1, evidence=evidence,
                               interaction=binding, admission=admitted(binding))
    assert result["status"] == "clarify"
    assert not planner.calls and not home.calls


async def test_tv_speech_never_reaches_planner(setup):
    orch, home, planner, _ = setup
    result = await orch.handle(
        "打开客厅灯", str(uuid4()), 1, evidence=Evidence(live_speaker=False, people_count=0)
    )
    assert result["status"] == "ignored"
    assert not planner.calls and not home.calls


async def test_duplicate_event_never_replays(setup):
    orch, home, _, _ = setup
    eid = str(uuid4())
    await orch.handle("调整客厅和书房", eid, 1)
    assert (await orch.handle("调整客厅和书房", eid, 1))["status"] == "ignored"
    assert len(home.calls) == 2


async def test_expired_input_does_not_execute(setup):
    orch, home, _, _ = setup
    assert (await orch.handle("调整客厅和书房", str(uuid4()), 1, captured_at=1))["status"] == "cancelled"
    assert not home.calls


def test_numeric_nan_cannot_verify():
    assert not state_matches({"brightness": 30}, {"state": "on", "attributes": {"brightness": float("nan")}})


def test_capability_limits_and_diagnostic_confirmation():
    home = Home()
    home.entities["light.living"].category = "config"
    assert validate_action(Action(entity_id="light.living", service="turn_on"), home.entities)
    with pytest.raises(ValueError):
        validate_action(
            Action(entity_id="light.living", service="turn_on", parameters={"entity_id": "lock.front"}),
            home.entities,
        )


async def test_low_confidence_simple_label_uses_planner(setup):
    orch, home, planner, _ = setup

    class Uncertain:
        async def classify(self, text):
            return {"label": "simple", "confidence": 0.47}

    orch.decision = Uncertain()
    await handle(orch, "打开客厅灯")
    assert planner.calls == 1
    assert len(home.calls) == 2


async def test_high_confidence_named_simple_command_bypasses_planner(setup):
    orch, home, planner, _ = setup

    class Certain:
        async def classify(self, text):
            return {"label": "simple", "confidence": 0.917}

    orch.decision = Certain()
    result = await handle(orch, "打开客厅灯")
    assert result["status"] == "verified"
    assert planner.calls == 0
    assert [a.entity_id for a in home.calls] == ["light.living"]


async def test_qualified_ambient_command_with_unavailable_gaze_uses_existing_plan(setup):
    orch, home, planner, _ = setup
    binding = bound(camera_id="text", session_id="text")
    result = await orch.handle(
        "打开客厅灯", str(uuid4()), 1,
        evidence=Evidence(live_speaker=True, people_count=1, attention=.98),
        interaction=binding, admission=admitted(binding, pointing_admitted=False),
    )
    assert result["status"] == "verified" and home.calls
    assert orch.focus is None  # This fixture's existing planner selects two devices.


async def test_unqualified_ambient_booleans_never_reach_planner(setup):
    orch, home, planner, _ = setup
    result = await orch.handle(
        "打开客厅灯", str(uuid4()), 1,
        evidence=Evidence(live_speaker=True, people_count=1, facing_camera=True, attention=.99),
    )
    assert result["status"] == "ignored" and not planner.calls and not home.calls


async def test_console_focus_does_not_escape_server_session_or_map(setup):
    orch, home, planner, _ = setup
    class Certain:
        async def classify(self, text):
            return {"label": "simple", "confidence": .99}
    orch.decision = Certain()
    binding = bound(input_kind="console_text", camera_id="text", session_id="server-A",
                    subject_ref=None, track_ref=None, track_epoch=None, associated=False, speaker_count=0)
    result = await orch.handle("打开客厅灯", str(uuid4()), 1, session="server-A", interaction=binding)
    assert result["status"] == "verified" and not planner.calls
    result = await orch.handle("亮一点", str(uuid4()), 1, session="server-A", interaction=binding)
    assert result["status"] == "verified" and home.calls[-1].entity_id == "light.living"
    result = await orch.handle("这个亮一点", str(uuid4()), 1, session="server-B",
                               interaction=__import__("dataclasses").replace(binding, session_id="server-B"))
    assert result["status"] == "clarify"
    assert len(home.calls) == 2


async def test_map_change_during_plan_prevents_execution_and_late_focus(setup):
    orch, home, planner, _ = setup
    binding = bound(input_kind="console_text", camera_id="text", session_id="text", subject_ref=None,
                    track_ref=None, track_epoch=None, associated=False, speaker_count=0)
    planner.on_plan = lambda: setattr(orch.settings, "camera_fixed_view", True)
    result = await orch.handle("打开客厅灯", str(uuid4()), 1, interaction=binding)
    assert result["status"] == "cancelled" and not home.calls and orch.focus is None


async def test_zero_timestamp_is_expired_and_cannot_become_current_input(setup):
    orch, home, _, _ = setup
    result = await orch.handle("调整客厅和书房", str(uuid4()), 1, captured_at=0)
    assert result["status"] == "cancelled" and not home.calls


async def test_explicit_other_room_action_does_not_create_this_rooms_referential_focus(setup):
    orch, home, planner, _ = setup
    class Certain:
        async def classify(self, text):
            return {"label": "simple", "confidence": .99}
    orch.decision = Certain()
    binding = bound(input_kind="console_text", camera_id="text", session_id="text", subject_ref=None,
                    track_ref=None, track_epoch=None, associated=False, speaker_count=0)
    result = await orch.handle("打开书房灯", str(uuid4()), 1, interaction=binding)
    assert result["status"] == "verified" and not planner.calls
    assert orch.focus is None


@pytest.mark.parametrize("condition_matches", [False, True])
async def test_invalidated_condition_read_is_not_consumed_or_followed_by_io(setup, condition_matches):
    orch, home, planner, _ = setup
    planner.value = {
        "summary": "按当前温度开灯",
        "steps": [{"entity_id": "light.living", "service": "turn_on", "conditions": [
            {"entity_id": "sensor.temperature", "operation": "gt", "value": 20 if condition_matches else 28},
            {"entity_id": "sensor.temperature", "operation": "gt", "value": 10},
        ]}],
    }
    original = home.read
    live, condition_reads, reads_after_invalidation = [True], [0], []

    async def read(target):
        if not live[0]:
            reads_after_invalidation.append(target)
        observed = await original(target)
        if target == "sensor.temperature":
            condition_reads[0] += 1
            if condition_reads[0] == 2:  # Planning snapshot, then the first live condition read.
                live[0] = False
        return observed

    home.read = read
    result = await orch.handle("打开客厅灯", str(uuid4()), 1, is_current=lambda: live[0])
    assert result["status"] == "cancelled" and result["steps"] == []
    assert not home.calls and not reads_after_invalidation and condition_reads == [2]


async def test_current_final_target_read_preserves_unchanged_result(setup):
    orch, home, planner, _ = setup
    home.entities["light.living"].state = "on"
    planner.value = {"summary": "保持客厅灯开启",
                     "steps": [{"entity_id": "light.living", "service": "turn_on"}]}
    result = await orch.handle("打开客厅灯", str(uuid4()), 1, is_current=lambda: True)
    assert result["status"] == "verified"
    assert result["steps"] == [{"entity_id": "light.living", "status": "unchanged"}]
    assert not home.calls


@pytest.mark.parametrize("stage", ["execute", "readback", "retry_delay"])
async def test_invalidation_after_dispatch_retains_unverified_and_stops_io(setup, monkeypatch, stage):
    orch, home, planner, _ = setup
    planner.value = {"summary": "打开客厅灯",
                     "steps": [{"entity_id": "light.living", "service": "turn_on"}]}
    original_read, original_execute = home.read, home.execute
    live, post_dispatch_reads, delays = [True], [], []

    async def execute(action):
        await original_execute(action)
        if stage == "execute":
            live[0] = False

    async def read(target):
        observed = await original_read(target)
        if home.calls:
            post_dispatch_reads.append(target)
            if stage == "readback":
                live[0] = False
            elif stage == "retry_delay":
                observed["state"] = "off"  # Original write happened; report has not caught up.
        return observed

    async def delay(seconds):
        delays.append(seconds)
        live[0] = False

    home.execute, home.read = execute, read
    monkeypatch.setattr("friday.orchestrator.asyncio.sleep", delay)
    result = await orch.handle("打开客厅灯", str(uuid4()), 1, is_current=lambda: live[0])
    assert len(home.calls) == 1 and home.entities["light.living"].state == "on"
    assert result["status"] == "unverified" and result["steps"][0]["status"] == "unverified"
    assert len(post_dispatch_reads) == (0 if stage == "execute" else 1)
    assert delays == ([0.5] if stage == "retry_delay" else [])
