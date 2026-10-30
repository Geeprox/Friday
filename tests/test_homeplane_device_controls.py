"""Simulated multi-device services, durable action guards, no household access."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from homeplane.action_contracts import DevicePlanRequest, HomeDeviceAuthority
from homeplane.actions import ActionController
from homeplane.core import HomeCore
from homeplane.ha import HAReadOnlyAdapter
from homeplane.ha_control import OPERATIONS
from homeplane.store import TraceStore
from tests.test_homeplane_actions import close
from tests.test_homeplane_ha import Socket

CASES = [
    ("light.set", {"power": "on", "brightness": 8}, "light", "turn_on"),
    ("humidifier.power",{"power":"on"},"humidifier","turn_on"),
    ("humidifier.humidity",{"humidity":55},"humidifier","set_humidity"),
    ("humidifier.mode",{"mode":"quiet"},"humidifier","set_mode"),
    ("water_heater.power",{"power":"on"},"water_heater","turn_on"),
    ("water_heater.mode",{"operation_mode":"low"},"water_heater","set_operation_mode"),
    ("switch.set", {"power": "on"}, "switch", "turn_on"),
    ("fan.set", {"power": "off"}, "fan", "turn_off"),
    ("fan.percentage", {"percentage": 50}, "fan", "set_percentage"),
    ("climate.power", {"power": "on"}, "climate", "turn_on"),
    ("cover.tilt", {"tilt_position": 65}, "cover", "set_cover_tilt_position"),
    ("climate.mode", {"hvac_mode": "cool"}, "climate", "set_hvac_mode"),
    ("climate.temperature", {"temperature": 24.5}, "climate", "set_temperature"),
    ("cover.position", {"position": 20}, "cover", "set_cover_position"),
    ("lock.set", {"lock": "locked"}, "lock", "lock"),
    ("number.value", {"value": 3}, "number", "set_value"),
    ("select.option", {"option": "low"}, "select", "select_option"),
    ('climate.fan_mode',{'fan_mode':'low'},'climate','set_fan_mode'),
    ('light.color_temperature',{'color_temp_kelvin':3500},'light','turn_on'),
    ('media_player.power',{'power':'on'},'media_player','turn_on'),
    ('media_player.volume',{'volume_level':.4},'media_player','volume_set'),
    ('media_player.mute',{'is_volume_muted':True},'media_player','volume_mute'),
    ('media_player.playback',{'state':'playing'},'media_player','media_play'),
]


class Registry(Socket):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def send(self, message):
        data = json.loads(message)
        self.sent.append(data)
        if "id" not in data:
            return
        inventories = {
            "config/entity_registry/list": [
                {
                    "entity_id": r["entity_id"],
                    "id": "stable-" + r["entity_id"],
                    "device_id": "device-" + r["entity_id"],
                }
                for r in self.rows
            ],
            "config/device_registry/list": [
                {"id": "device-" + r["entity_id"], "area_id": "room"} for r in self.rows
            ],
            "config/area_registry/list": [{"area_id": "room", "name": "SIMULATED room"}],
        }
        self.messages.append({"id": data["id"], "success": True, "result": inventories[data["type"]]})


def rig(tmp_path):
    attributes = {
        "humidifier":{"supported_features":1,"available_modes":["auto","quiet"],"mode":"auto","min_humidity":30,"max_humidity":70,"humidity":60,"current_humidity":65},
        "water_heater":{"supported_features":10,"operation_list":["low","high","off"],"operation_mode":"off","current_temperature":35},
        "light": {"brightness": 100, "supported_color_modes": ["brightness",'color_temp'],
                  'min_color_temp_kelvin':2000,'max_color_temp_kelvin':6500,'color_temp_kelvin':3000},
        "switch": {},
        "fan": {"supported_features": 1, "percentage": 25, "percentage_step": 25},
        "climate": {
            "supported_features": 393,
            'fan_modes':['auto','low','high'],'fan_mode':'auto',
            "hvac_modes": ["off", "cool", "heat"],
            "min_temp": 16,
            "max_temp": 30,
            "target_temp_step": 0.5,
            "temperature": 23,
        },
        "cover": {"supported_features": 132, "current_position": 0, "current_tilt_position": 20},
        "lock": {},
        "number": {"min": 0, "max": 10, "step": 1},
        "select": {"options": ["low", "high"]},
        "camera": {},
        "sensor": {"device_class": "temperature", "unit_of_measurement": "°C"},
        'media_player':{'supported_features':16781,'volume_level':.2,'is_volume_muted':False},
    }
    initial = {
        "humidifier":"off",
        "water_heater":"off",
        "light": "off",
        "switch": "off",
        "fan": "on",
        "climate": "off",
        "cover": "closed",
        "lock": "unlocked",
        "number": "2",
        "select": "high",
        "camera": "idle",
        "sensor": "24",
        'media_player':'paused',
    }
    rows = [
        {
            "entity_id": d + ".simulated",
            "state": initial[d],
            "attributes": {"friendly_name": "SIMULATED " + d, **attrs},
        }
        for d, attrs in attributes.items()
    ]
    calls = []
    fault = {"send": None, "mismatch": False, "rebound": False}
    domains = {d: {} for d in attributes}
    for _, _, d, s in CASES:
        domains[d][s] = {}
    for d in ("light", "switch", "fan"):
        domains[d].update({"turn_on": {}, "turn_off": {}})
    domains['climate']['turn_off']={}
    domains['humidifier']['turn_off']={}
    domains['water_heater']['turn_off']={}
    domains["lock"]["unlock"] = {}
    domains['media_player'].update({'turn_off':{},'media_pause':{}})

    def handle(request):
        if request.url.path == "/api/states":
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/services":
            return httpx.Response(200, json=[{"domain": d, "services": s} for d, s in domains.items()])
        external = request.url.path.rsplit("/", 1)[-1]
        if request.method == "POST":
            calls.append(request)
            body = json.loads(request.content)
            if fault["send"] == "timeout":
                raise httpx.ReadTimeout("SIMULATED private error")
            if fault["send"] == "cancel":
                raise asyncio.CancelledError()
            if fault["send"] == "deny":
                return httpx.Response(403, json={"secret": "never return"})
            row = next(r for r in rows if r["entity_id"] == body["entity_id"])
            if not fault["mismatch"]:
                if external in {"turn_on", "turn_off"}:
                    row["state"] = ("cool" if row["entity_id"].startswith("climate.") else "on") if external == "turn_on" else "off"
                for k, v in body.items():
                    if k == "entity_id":
                        continue
                    if k in {"hvac_mode", "option", "value"}:
                        row["state"] = str(v)
                    else:
                        row["attributes"][{"position":"current_position","tilt_position":"current_tilt_position"}.get(k,k)] = v
                if external in {"lock", "unlock"}:
                    row["state"] = "locked" if external == "lock" else "unlocked"
                if external in {'media_play','media_pause'}:
                    row['state']='playing' if external=='media_play' else 'paused'
            return httpx.Response(200, json=[{"entity_id": "unrelated_changed_state"}])
        return httpx.Response(200, json=next(r for r in rows if r["entity_id"] == external))

    ha = HAReadOnlyAdapter(
        "http://simulated.invalid",
        "SIMULATED-private",
        "multi-fixture",
        transport=httpx.MockTransport(handle),
        ws_connect=lambda *_a, **_k: Registry(rows),
    )
    store = TraceStore(tmp_path / "SIMULATED-controls.sqlite")
    core = HomeCore(ha, store)
    path = tmp_path / "SIMULATED-authority.json"
    authority = HomeDeviceAuthority(
        enabled=True,
        authority_ref="SIMULATED all-device grant",
        ha_url=ha.url,
        ha_instance=ha.instance_id,
        scope="all_registered_devices",
        operations=list(OPERATIONS),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    path.write_text(authority.model_dump_json())
    path.chmod(0o600)
    core.actions = ActionController(core, path)
    return core, rows, calls, fault, path, authority


async def plan(core, operation, desired):
    snap = await core.snapshot()
    entity = next(e for e in snap.entities if e.kind == operation.split(".")[0])
    return await core.actions.plan(
        DevicePlanRequest(
            target=entity.id,
            operation=operation,
            desired=desired,
            expected_state_version=entity.state_version,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            idempotency_key=str(uuid4()),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,desired,domain,service", CASES)
async def test_all_registered_operations_plan_once_send_exact_target_and_report(
    tmp_path, operation, desired, domain, service
):
    core, rows, calls, *_ = rig(tmp_path)
    try:
        snap = await core.snapshot()
        assert len(snap.action_targets) == 11 and not calls  # Camera and sampled sensor excluded.
        trace = await plan(core, operation, desired)
        assert trace.status == "planned" and not calls and not trace.real_write_attempted
        before = json.loads(json.dumps(rows))
        a, b = await asyncio.gather(
            core.actions.execute(trace.action_id), core.actions.execute(trace.action_id)
        )
        assert a.status == b.status == "reported_match" and not a.physically_confirmed
        assert len(calls) == 1 and calls[0].url.path == f"/api/services/{domain}/{service}"
        assert json.loads(calls[0].content)["entity_id"] == domain + ".simulated"
        assert [r for r in before if not r["entity_id"].startswith(domain + ".")] == [
            r for r in rows if not r["entity_id"].startswith(domain + ".")
        ]
        assert "SIMULATED-private" not in a.model_dump_json()
    finally:
        await close(core)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,desired",
    [
        ("climate.mode", {"hvac_mode": "dry"}),
        ("climate.temperature", {"temperature": 12}),
        ("climate.temperature", {"temperature": 24.25}),
        ("number.value", {"value": 2.5}),
        ("select.option", {"option": "absent"}),
        ("fan.percentage", {"percentage": 37}),
    ],
)
async def test_live_choices_ranges_and_steps_reject_without_send(tmp_path, operation, desired):
    core, _, calls, *_ = rig(tmp_path)
    try:
        trace = await plan(core, operation, desired)
        assert trace.status == "rejected" and trace.code == "unsupported_parameters"
        assert not calls and not trace.real_write_attempted
    finally:
        await close(core)


@pytest.mark.parametrize(
    "operation,desired",
    [
        ("switch.set", {"power": "toggle"}),
        ("switch.set", {"power": "on", "service": "restart"}),
        ("climate.mode", {"hvac_mode": ["cool"]}),
        ("climate.temperature", {"temperature": True}),
        ("cover.position", {"position": 101}),
        ("cover.position", {"position": True}),
        ("lock.set", {"lock": "toggle"}),
        ("number.value", {"value": float("inf")}),
    ],
)
def test_client_cannot_supply_raw_services_or_untyped_parameters(operation, desired):
    with pytest.raises(ValidationError):
        DevicePlanRequest(
            target="SIMULATED",
            operation=operation,
            desired=desired,
            expected_state_version="SIMULATED",
            expires_at=datetime.now(UTC),
            idempotency_key="SIMULATED",
        )


@pytest.mark.asyncio
async def test_global_grant_changes_source_binding_pause_and_restart_fail_closed(tmp_path):
    core, rows, calls, fault, path, authority = rig(tmp_path)
    try:
        trace = await plan(core, "switch.set", {"power": "on"})
        path.write_text(authority.model_copy(update={"operations": ["light.set"]}).model_dump_json())
        assert (await core.actions.execute(trace.action_id)).code == "authority_changed" and not calls
        path.write_text(authority.model_dump_json())
        pending = await plan(core, "cover.position", {"position": 20})
        core.actions.pause()
        assert (await core.snapshot()).action_targets == []
        assert (await core.actions.execute(pending.action_id)).status == "rejected" and not calls
        path.write_text(authority.model_dump_json())
        pending = await plan(core, "switch.set", {"power": "on"})
        core.actions.close()
        core.actions = ActionController(core, path)
        assert (await core.actions.execute(pending.action_id)).code == "restart_requires_new_plan"
        core.adapter.instance_id = "other-home"
        assert (await core.snapshot()).action_targets == [] and not calls
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_timeout_and_delayed_cover_readback_require_readonly_reconcile_no_retry(tmp_path):
    core, rows, calls, fault, *_ = rig(tmp_path)
    try:
        fault["mismatch"] = True
        pending = await plan(core, "cover.position", {"position": 20})
        unknown = await core.actions.execute(pending.action_id)
        assert unknown.status == "unknown_outcome" and len(calls) == 1
        next(r for r in rows if r["entity_id"] == "cover.simulated")["attributes"]["current_position"] = 20
        checked = await core.actions.reconcile(pending.action_id)
        assert checked.status == "reported_match" and not checked.physically_confirmed and len(calls) == 1
        fault["send"] = "timeout"
        p = await plan(core, "switch.set", {"power": "on"})
        assert (await core.actions.execute(p.action_id)).status == "unknown_outcome"
        await core.actions.execute(p.action_id)
        assert len(calls) == 2
        original = core.actions.binding_fingerprint
        core.actions.binding_fingerprint = lambda _: "f" * 64
        # A renamed/rebound or different HA source cannot verify the old operation.
        next(r for r in rows if r["entity_id"] == "switch.simulated")["state"] = "on"
        assert (await core.actions.reconcile(p.action_id)).status == "unknown_outcome" and len(calls) == 2
        core.actions.binding_fingerprint = original
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_missing_entity_features_or_service_never_becomes_controllable(tmp_path):
    core, rows, calls, *_ = rig(tmp_path)
    try:
        cover = next(r for r in rows if r["entity_id"] == "cover.simulated")
        cover["attributes"]["supported_features"] = 0
        snap = await core.snapshot()
        assert not any(e.id in snap.action_targets for e in snap.entities if e.kind == "cover")
        cover["attributes"]["supported_features"] = 4
        next(r for r in rows if r["entity_id"] == "switch.simulated")["state"] = "unavailable"
        assert (await plan(core, "switch.set", {"power": "on"})).code == "stale_input"
        assert not calls
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_resume_only_restores_current_private_grant_and_never_revives_old_plan(tmp_path):
    core, _, calls, _, _, _ = rig(tmp_path)
    try:
        pending = await plan(core, "switch.set", {"power": "on"})
        core.actions.pause()
        report = core.actions.policy_report()
        assert report["scope"] == "all_registered_devices" and not report["enabled"]
        with pytest.raises(ValueError, match="source_conflict"):
            core.actions.resume("a" * 64)
        core.actions.resume(report["revision"])
        assert core.actions.policy_report()["enabled"]
        assert (await core.actions.execute(pending.action_id)).status == "expired" and not calls
        assert (await plan(core, "switch.set", {"power": "on"})).status == "planned" and not calls
    finally:
        await close(core)


@pytest.mark.asyncio
async def test_tilt_and_climate_power_require_device_feature_and_live_service(tmp_path):
    core,rows,calls,*_=rig(tmp_path)
    try:
        await core.snapshot()
        for kind,operation,desired,feature in [('cover','cover.tilt',{'tilt_position':50},4),('climate','climate.power',{'power':'on'},9)]:
            row=next(r for r in rows if r['entity_id']==kind+'.simulated')
            row['attributes']['supported_features']=feature
            assert (await plan(core,operation,desired)).status=='rejected'
        assert not calls
    finally:await close(core)
