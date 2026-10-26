import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_homeplane_interaction_sessions import admitted, bound

from friday.adapters import DemoAdapter
from friday.config import Anchor, Settings
from friday.core import Controller, Evidence, Observation, parse_command
from friday.journal import Journal


@pytest.fixture
def setup(tmp_path):
    settings = Settings.model_validate_json(Path("friday.example.json").read_text())
    journal = Journal(tmp_path / "events.sqlite")
    adapter = DemoAdapter(settings.devices)
    controller = Controller(settings, adapter, journal, allow_simulated_admission=True)
    controller.start()
    yield controller, adapter, journal
    journal.close()


def observation(text="打开客厅灯", **kwargs):
    values = dict(
        event_id=str(uuid4()),
        text=text,
        captured_at=time.time(),
        session="one",
        camera_id="living-room",
        evidence=Evidence(live_speaker=True, people_count=1, facing_camera=True, attention=0.98),
    )
    values.update(kwargs)
    binding = values.get("interaction", bound(session_id=values["session"], camera_id=values["camera_id"]))
    values.setdefault("interaction", binding)
    values.setdefault("admission", admitted(binding) if binding else None)
    return Observation(**values)


@pytest.mark.parametrize(
    "text",
    [
        "不要打开客厅灯",
        "他怎么还不开灯啊？",
        "如果打开客厅灯",
        "他说打开客厅灯",
        "“打开客厅灯”",
        "有点暗",
        "开门",
        "打开客厅灯然后开门",
        "关闭所有设备",
    ],
)
async def test_background_and_unsupported_never_write(setup, text):
    c, a, _ = setup
    result = await c.process(observation(text), c.generation)
    assert result.status in ("ignored", "clarify")
    assert all(not s["on"] for s in a.states.values())


async def test_wake_free_name(setup):
    c, a, _ = setup
    r = await c.process(observation(), c.generation)
    assert r.status == "simulated"
    assert a.states["ceiling-light"]["on"]
    assert not a.states["floor-lamp"]["on"]


async def test_point_then_dim_context(setup):
    c, a, _ = setup
    point = Evidence(
        live_speaker=True,
        people_count=1,
        pointing=True,
        attention=0.99,
        target_id="floor-lamp",
        target_confidence=0.98,
    )
    r = await c.process(observation("把这个打开", evidence=point), c.generation)
    assert r.device_id == "floor-lamp" and r.status == "simulated"
    r = await c.process(observation("这个暗一点"), c.generation)
    assert r.status == "simulated"
    assert a.states["floor-lamp"]["brightness"] == 50


@pytest.mark.parametrize(
    "patch", [dict(live_speaker=False), dict(people_count=2), dict(attention=0.5)]
)
async def test_attention_gate(setup, patch):
    c, a, _ = setup
    obs = observation()
    obs.evidence = obs.evidence.model_copy(update=patch)
    assert (await c.process(obs, c.generation)).reason == "attention_uncertain"
    assert not a.states["ceiling-light"]["on"]


async def test_friday_optional_or_required(setup):
    c, a, _ = setup
    c.settings.wake_mode = "required"
    assert (await c.process(observation(), c.generation)).reason == "say_friday"
    assert (await c.process(observation("Miloco，打开客厅灯"), c.generation)).status == "ignored"
    assert (await c.process(observation("Friday，打开客厅灯"), c.generation)).status == "simulated"


async def test_replay_and_stale(setup):
    c, a, j = setup
    obs = observation()
    await c.process(obs, c.generation)
    assert (await c.process(obs, c.generation)).reason == "duplicate_event"
    stale = observation("关闭客厅灯", captured_at=time.time() - 60)
    assert (await c.process(stale, c.generation)).reason == "stale_or_wrong_camera"
    assert a.states["ceiling-light"]["on"]


async def test_context_expires_and_session_boundary(setup):
    c, a, _ = setup
    await c.process(observation(), c.generation)
    assert (await c.process(observation("这个暗一点", session="other"), c.generation)).status == "clarify"
    assert c.focus is None
    await c.process(observation("关闭客厅灯"), c.generation)
    expires = c.focus.expires_mono
    c.interactions.clock = lambda: expires + 1
    assert (await c.process(observation("这个暗一点"), c.generation)).status == "clarify"


async def test_ambiguous_and_unregistered_point(setup):
    c, a, _ = setup
    assert (await c.process(observation("灯打开"), c.generation)).status == "clarify"
    obs = observation("把这个打开")
    obs.evidence = obs.evidence.model_copy(
        update={"pointing": True, "target_id": "door", "target_confidence": 1.0}
    )
    assert (await c.process(obs, c.generation)).reason == "pointing_ambiguous"


async def test_pause_while_reading_cancels_write(setup):
    c, a, _ = setup
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = a.read

    async def blocked(device):
        entered.set()
        await proceed.wait()
        return await original(device)

    a.read = blocked
    task = asyncio.create_task(c.process(observation(), c.generation))
    await entered.wait()
    c.stop()
    proceed.set()
    assert (await task).reason == "cancelled_before_dispatch"
    assert not a.states["ceiling-light"]["on"]


async def test_live_ptz_blocks_pointing(setup):
    c, a, _ = setup
    c.settings.mode = "live"
    obs = observation(
        "把这个打开",
        evidence=Evidence(
            live_speaker=True,
            people_count=1,
            pointing=True,
            attention=0.99,
            target_id="floor-lamp",
            target_confidence=0.99,
        ),
    )
    assert (await c.process(obs, c.generation)).reason == "camera_view_not_fixed"


async def test_ack_without_state_change_is_not_success(setup):
    c, a, _ = setup

    async def no_op(*args):
        pass

    a.write = no_op
    r = await c.process(observation(), c.generation)
    assert r.status == "unverified"
    assert c.focus is None


async def test_diagnostics_exclude_transcript_and_bounded(setup):
    c, _, j = setup
    await c.process(observation(), c.generation)
    for i in range(1005):
        j.record("lifecycle", "test", secret="not stored", transcript="private speech")
    events = j.events(2000)
    assert len(events) == 1000
    assert "private speech" not in json.dumps(events)
    assert "not stored" not in json.dumps(events)


def test_journal_dedup_survives_restart(tmp_path):
    path = tmp_path / "db.sqlite"
    j = Journal(path)
    assert j.reserve("a")
    j.close()
    j = Journal(path)
    assert not j.reserve("a")
    assert j.db.execute("SELECT status FROM seen").fetchone()[0] == "interrupted_unknown"
    j.close()


def test_anchor_and_remote_validation():
    with pytest.raises(ValueError):
        Anchor(camera_id="x", box=(0.8, 0.2, 0.1, 0.9))
    with pytest.raises(ValueError):
        Settings(vlm_url="https://remote.example/v1")
    assert parse_command("Friday, 把这个打开")[2]


async def test_timeout_after_write_is_unknown_not_failed(setup):
    c, a, _ = setup

    async def uncertain(*args):
        raise TimeoutError()

    a.write = uncertain
    result = await c.process(observation(), c.generation)
    assert result.status == "unverified"
    assert result.reason == "dispatch_result_unknown"
    assert c.focus is None


async def test_qualified_no_gaze_pointing_or_wake_enters_existing_demo_control(setup):
    c, a, _ = setup
    obs = observation(evidence=Evidence(live_speaker=True, people_count=1, attention=.98))
    result = await c.process(obs, c.generation)
    assert result.status == "simulated" and result.producer_state == "simulated"
    assert a.states["ceiling-light"]["on"]


async def test_legacy_evidence_is_not_new_producer_admission(setup):
    c, a, _ = setup
    result = await c.process(observation(interaction=None, admission=None), c.generation)
    assert result.reason == "ambient_admission_unavailable"
    assert not a.states["ceiling-light"]["on"] and c.focus is None


@pytest.mark.parametrize("change", ["subject", "room", "map", "source", "authority", "expiry"])
async def test_binding_change_during_read_never_dispatches_or_commits_focus(setup, change):
    from dataclasses import replace
    c, a, _ = setup
    obs = observation()
    original = a.read

    async def changed(device):
        if change == "map":
            c.settings.camera_fixed_view = not c.settings.camera_fixed_view
        elif change == "expiry":
            c.interactions.invalidate()
        else:
            field = {"subject": "subject_ref", "room": "room_id", "source": "source_revision",
                     "authority": "authority_revision"}[change]
            obs.interaction = replace(obs.interaction, **{field: "changed"})
        return await original(device)

    a.read = changed
    result = await c.process(obs, c.generation)
    assert result.reason == "cancelled_before_dispatch" and not a.states["ceiling-light"]["on"]
    assert c.focus is None


async def test_stop_after_dispatch_does_not_claim_physical_undo_or_reuse_focus(setup):
    c, a, _ = setup
    original = a.write

    async def sent_then_stopped(*args):
        await original(*args)
        c.stop()

    a.write = sent_then_stopped
    result = await c.process(observation(), c.generation)
    assert result.status == "unverified" and a.states["ceiling-light"]["on"]
    assert c.focus is None


@pytest.mark.parametrize("change", [{"subject_ref": "fixture-B"}, {"track_epoch": "epoch-2"}])
async def test_real_consumer_does_not_lend_A_focus_to_B_or_recycled_track(setup, change):
    from dataclasses import replace
    c, a, _ = setup
    assert (await c.process(observation(), c.generation)).status == "simulated"
    binding = replace(bound(session_id="one"), **change)
    other = observation("亮一点", interaction=binding, admission=admitted(binding))
    result = await c.process(other, c.generation)
    assert result.status == "clarify" and a.states["ceiling-light"]["brightness"] == 70
    assert c.focus is None
