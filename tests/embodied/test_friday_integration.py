"""Migration boundaries: fixed targets, origin, player identity and optional models."""

from unittest.mock import AsyncMock

import httpx
import pytest

from homeplane.api import create_app
from homeplane.embodied.catalog import catalog
from homeplane.embodied.extensions import RecognitionEvent, RecognizerRegistry
from homeplane.embodied.household import DeviceCommands, basic_command, resolve_targets


def test_whole_house_grammar_preserves_prohibitions_and_complex_requests():
    for text in ["别关主卧灯", "不要关闭客厅窗帘", "Friday，别打开空调"]:
        assert basic_command(text, True)["route"] == "no_op"
    assert basic_command("Friday，打开书房窗帘")["device"] == "书房窗帘"
    assert basic_command("把主卧空调温度调到26度", True)["action"] == "temperature:26"
    assert basic_command("空调调到26度", True)["action"] == "temperature:26"
    assert basic_command("客厅空调制冷", True)["action"] == "mode:cool"
    for text, device, action in [
        ("全屋灯打开", "全屋灯", "on"),
        ("主卧空调关闭", "主卧空调", "off"),
        ("书房窗帘拉开", "书房窗帘", "on"),
    ]:
        command = basic_command(text, True)
        assert command["device"] == device and command["action"] == action
    assert basic_command("帮我查一下今天股市怎么样", True) is None
    assert basic_command("如果明天下雨就打开窗帘", True) is None
    assert basic_command("打开灯", False) is None


def test_device_resolution_requires_unique_target_or_explicit_group():
    items = [
        dict(id=str(i), name=name, room=room, kind=kind, controllable=True)
        for i, (name, room, kind) in enumerate(
            [
                ("主卧主灯", "主卧", "light"),
                ("主卧床头灯", "主卧", "light"),
                ("书房主灯", "书房", "light"),
                ("主卧窗帘", "主卧", "cover"),
            ]
        )
    ]
    assert not resolve_targets("灯", items)
    assert not resolve_targets("主卧灯", items)
    assert resolve_targets("主卧主灯", items)[0]["id"] == "0"
    assert len(resolve_targets("全屋灯", items)) == 3
    assert resolve_targets("窗帘", items, room="主卧")[0]["id"] == "3"
    assert not resolve_targets("门锁", items)
    assert DeviceCommands.parameters(dict(kind="light", source_kind="switch"), "off") == (
        "switch.set",
        dict(power="off"),
    )


def test_camera_session_track_and_sequence_fence_extension_events():
    registry = RecognizerRegistry()
    event = RecognitionEvent(
        recognizer="hand.ok",
        camera="cam_1234567890",
        session="s1",
        track=7,
        sequence=1,
        observed_mono=10,
        confidence=0.9,
    )
    context = dict(camera=event.camera, session="s1", bound_track=7, now=11)
    assert registry.admit(event, **context)
    assert not registry.admit(event, **context)
    assert not registry.admit(event.model_copy(update={"sequence": 2}), **dict(context, session="s2"))
    assert not registry.admit(event.model_copy(update={"sequence": 2}), **dict(context, bound_track=8))
    assert not registry.admit(event.model_copy(update={"sequence": 2}), **dict(context, now=12))


class OptionalService:
    def configured(self):
        return False

    def cameras(self):
        return []

    status = AsyncMock(return_value=dict(configured=False, owned=False, state="not_configured", quiet=True))
    close = AsyncMock()
    start = AsyncMock(return_value=dict(state="connecting"))


@pytest.mark.asyncio
async def test_console_catalog_without_optional_sdk_and_same_origin_lifecycle(tmp_path):
    service = OptionalService()
    app = create_app(
        db_path=tmp_path / "traces.sqlite", settings_path=tmp_path / "settings.json", embodied_service=service
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            result = (await client.get("/api/games")).json()
            assert len(result["apps"]) == 6
            assert result["service"]["state"] == "not_configured"
            assert (await client.post("/api/embodied/start", json=dict(app="ski"))).status_code == 403
            assert (
                await client.post(
                    "/api/embodied/start", json=dict(app="ski"), headers={"Origin": "http://foreign.invalid"}
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/embodied/start", json=dict(app="ski"), headers={"Origin": "http://testserver"}
                )
            ).status_code == 200


def test_catalog_does_not_claim_full_werewolf_rules():
    item = next(i for i in catalog() if i["id"] == "werewolf")
    assert item["kind"] == "seating"
    assert item["status"] == "prototype"


def test_bystander_cannot_switch_or_steal_bound_player_and_owner_survives_yeah():
    from test_motion_games import human

    from homeplane.embodied.motion_games import MotionGames

    game = MotionGames()
    game.select("ski")
    people = [human(1, 0.3), human(2, 0.7)]
    registration = {"status": "locked"}
    for i in range(40):
        game.update(people, registration, i * 0.05, [dict(person_id=2, gesture="ok")])
    assert game.preferred_track == 2
    for i in range(40, 70):
        game.update(people, registration, i * 0.05, [])
    for i in range(70, 110):
        game.update(people, registration, i * 0.05, [dict(person_id=1, gesture="yeah")])
    assert game.selected_mode == "ski" and game.preferred_track == 2
    for i in range(110, 150):
        game.update(people, registration, i * 0.05, [dict(person_id=2, gesture="yeah")])
    assert game.selected_mode == "tennis" and game.preferred_track == 2


def test_permanent_authority_requires_explicit_null_and_exact_scope():
    from homeplane.action_contracts import HomeDeviceAuthority

    value = dict(
        enabled=True,
        authority_ref="synthetic-owner",
        ha_url="http://synthetic.invalid",
        ha_instance="synthetic",
        scope="all_registered_devices",
        operations=["light.set"],
        external_entity_ids=["light.synthetic"],
        expires_at=None,
    )
    assert HomeDeviceAuthority(**value).expires_at is None
    value.pop("expires_at")
    with pytest.raises(ValueError):
        HomeDeviceAuthority(**value)


@pytest.mark.asyncio
async def test_permanent_exact_entity_scope_rechecked_before_send(tmp_path):
    from tests.test_homeplane_actions import close
    from tests.test_homeplane_device_controls import plan, rig

    core, rows, calls, fault, path, authority = rig(tmp_path)
    try:
        permanent = authority.model_copy(
            update={"expires_at": None, "external_entity_ids": ["light.simulated"]}
        )
        path.write_text(permanent.model_dump_json())
        snapshot = await core.snapshot()
        targets = core.actions.targets(snapshot)
        assert len(targets) == 1
        assert next(e for e in snapshot.entities if e.id == targets[0]).kind == "light"
        allowed = await plan(core, "light.set", {"power": "on"})
        denied = await plan(core, "climate.mode", {"hvac_mode": "cool"})
        assert allowed.status == "planned" and denied.status == "rejected" and not calls
        # A plan cannot survive a subsequent narrowing of the permanent grant.
        path.write_text(
            permanent.model_copy(update={"external_entity_ids": ["cover.simulated"]}).model_dump_json()
        )
        result = await core.actions.execute(allowed.action_id)
        assert result.status == "rejected" and not calls
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_home_command_logs_success_failure_without_household_payload(tmp_path, monkeypatch):
    from homeplane.embodied import bridge
    from homeplane.embodied.home_service import HomeService
    from homeplane.runtime_log import RuntimeLog

    service = object.__new__(HomeService)
    service.state = {}
    service.runtime_log = RuntimeLog(tmp_path / "logs")
    monkeypatch.setattr(bridge, "command", AsyncMock(return_value={"status": "accepted"}))
    await service.command("open", "private-event-id", "private-device-description")
    monkeypatch.setattr(bridge, "command", AsyncMock(side_effect=ValueError("private-token-transcript")))
    with pytest.raises(ValueError):
        await service.command("close", "private-event-id-2", "private-device-description")
    rows = service.runtime_log.export()["events"]
    assert [r["event"] for r in rows] == ["start", "result", "start", "result"]
    assert rows[1]["result"] == "ok" and rows[-1]["result"] == "error"
    for path in (tmp_path / "logs").glob("*.jsonl"):
        assert "private-" not in path.read_text()
