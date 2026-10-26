"""Anchored household grammar and execution through Friday's ActionController.

No raw service names, entity IDs or model-supplied HTTP requests cross this port.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from .intent import address

PREFIX = r"(?:(?:请你|请|麻烦你|麻烦|能不能|可以|你帮我|帮我|给我)\s*)*"
SUFFIX = r"(?:(?:一下|吧|谢谢)\s*)*"
OBJECT = r"[\u3400-\u9fffA-Za-z0-9]{1,32}?"
VERB = r"打开|拉开|开启|推开|关闭|关上|合上|拉上|关掉|开|关"


def basic_command(text, attending=False):
    admission = address(text, attending)
    if not admission["admitted"]:
        return None
    body = admission["body"].strip().rstrip("。.!！").replace(" ", "")
    if not body or len(body) > 80:
        return None
    if re.fullmatch(
        PREFIX + r"(?:先|暂时)?(?:别|不要|不用|不必)(?:帮我)?(?:" + VERB + r")" + OBJECT + SUFFIX, body
    ):
        return {"route": "no_op", "reply": "好的，保持当前状态。"}
    patterns = [
        PREFIX + r"(?P<verb>" + VERB + r")(?P<object>" + OBJECT + r")" + SUFFIX,
        PREFIX + r"(?:把)?(?P<object>" + OBJECT + r")(?:给我)?(?P<verb>" + VERB + r")" + SUFFIX,
    ]
    match = next((m for p in patterns if (m := re.fullmatch(p, body))), None)
    if match:
        target = match["object"].removesuffix("一下").removesuffix("吧").removesuffix("谢谢")
        if not any(t in target for t in ("灯", "窗帘", "梦幻帘", "空调")):
            return None
        return {
            "route": "home",
            "device": target,
            "action": "on" if match["verb"] in ("打开", "拉开", "开启", "推开", "开") else "off",
        }
    match = re.fullmatch(
        PREFIX
        + r"(?:把)?(?P<object>[\u3400-\u9fffA-Za-z0-9]{0,30}(?:空调|灯光|灯))(?:的)?(?:温度|亮度)?(?:调到|设为|设置为|调成)(?P<value>\d{1,3})(?P<unit>度|%|百分之)?"
        + SUFFIX,
        body,
    )
    if match:
        target = match["object"]
        return {
            "route": "home",
            "device": target,
            "action": ("temperature:" if "空调" in target else "brightness:") + match["value"],
        }
    match = re.fullmatch(
        PREFIX
        + r"(?:把)?(?P<object>[\u3400-\u9fffA-Za-z0-9]{0,30}空调)(?:设为|调成|切换到)?(?P<mode>制冷|制热|送风|除湿|自动)"
        + SUFFIX,
        body,
    )
    if match:
        modes = {"制冷": "cool", "制热": "heat", "送风": "fan_only", "除湿": "dry", "自动": "auto"}
        return {"route": "home", "device": match["object"], "action": "mode:" + modes[match["mode"]]}
    return None


def normalize(value):
    return re.sub(r"[的\s·_\-（）()]", "", value).replace("监控", "").replace("灯光", "灯")


def resolve_targets(query, items, *, room=None):
    """A unique registered match, or an explicit all-room group; never fuzzy pick."""
    if not isinstance(query, str) or not 1 <= len(query) <= 80:
        return []
    value = normalize(query)
    group = value.startswith(("所有", "全部", "全屋"))
    value = re.sub(r"^(?:所有|全部|全屋)", "", value)
    kind = (
        "cover"
        if any(k in value for k in ("帘", "窗帘"))
        else "climate"
        if "空调" in value
        else "light"
        if "灯" in value
        else None
    )
    if kind is None:
        return []
    candidates = [item for item in items if item["kind"] == kind and item["controllable"]]
    exact = [
        item
        for item in candidates
        if value in {normalize(item["name"]), normalize(item["room"] + item["name"])}
    ]
    if len(exact) == 1:
        return exact
    room_names = {normalize(item["room"]) for item in candidates if item["room"]}
    explicit_rooms = [r for r in room_names if r in value]
    selected_room = (
        max(explicit_rooms, key=len) if explicit_rooms else normalize(room or "") if not group else None
    )
    if selected_room:
        candidates = [i for i in candidates if normalize(i["room"]) == selected_room]
        value = value.replace(selected_room, "", 1)
    generic = {"light": {"灯", "所有灯"}, "cover": {"窗帘", "帘"}, "climate": {"空调"}}[kind]
    if value not in generic:
        candidates = [i for i in candidates if value in normalize(i["name"])]
    return candidates if group or len(candidates) == 1 else []


class DeviceCommands:
    def __init__(self, core, lock, service=None):
        self.core, self.lock, self.service = core, lock, service

    async def inventory(self):
        snapshot = await self.core.snapshot()
        targets = set(self.core.actions.targets(snapshot))
        from .paths import PRIVATE

        try:
            aliases = json.loads((PRIVATE / "device-aliases.json").read_text())
        except (OSError, ValueError):
            aliases = {}
        result = []
        for entity in snapshot.entities:
            external = next((b.external_id for b in entity.bindings if b.source == "ha"), "")
            alias = aliases.get(external)
            if entity.kind not in {"light", "cover", "climate", "fan", "switch"} and not alias:
                continue
            from homeplane.home_experience import household_function
            siblings = next((d.function_ids for d in snapshot.devices if entity.id in d.function_ids), [])
            if not household_function(entity, alias, [e for e in snapshot.entities if e.id in siblings]):
                continue
            if entity.kind == "light" and (
                "指示灯" in entity.name or "照明" not in entity.name and "路由" in entity.name
            ):
                continue
            room = (entity.room or "").rsplit(" ", 1)[-1]
            display_name = entity.name.split("  ", 1)[0]
            result.append(
                dict(
                    id=entity.id,
                    name=alias["name"] if alias else display_name,
                    room=alias.get("room", room) if alias else room,
                    kind=alias["kind"] if alias else entity.kind,
                    source_kind=entity.kind,
                    controllable=entity.id in targets,
                    capabilities=[c.name for c in entity.capabilities if c.access == 'write' and c.support == 'supported'],
                    capability_specs={c.name:c.model_dump() for c in entity.capabilities if c.access == 'write' and c.support == 'supported'},
                    current=next((o.value for o in snapshot.observations if o.entity_id == entity.id), None),
                )
            )
        from .miot_spec import optional_cover
        cover = optional_cover() if self.service and self.service.configured() else None
        if cover:
            authority, _ = self.core.actions.authority()
            bound = bool(
                authority
                and self.core.actions.policy_report().get("enabled")
                and authority.ha_url.rstrip("/") == getattr(self.core.adapter, "url", None)
                and authority.ha_instance == getattr(self.core.adapter, "instance_id", None)
            )
            result.append(
                dict(
                    id="friday:configured-cover",
                    name=cover['name'],
                    room=cover['room'],
                    kind="cover",
                    source_kind="miot",
                    controllable=bound,
                    capabilities=["cover.open", "cover.close", "cover.position", "cover.stop"],
                )
            )
        for item in result:
            item["actions"] = supported_actions(item)
        return result

    @staticmethod
    def parameters(item, action):
        kind = item["kind"]
        if kind in {'switch', 'fan'} and action in {'on','off','open','close'}:
            return kind + '.set', {'power': 'on' if action in {'on','open'} else 'off'}
        if kind == "light":
            if action in {"on", "off", "open", "close"}:
                return ("switch.set" if item.get("source_kind") == "switch" else "light.set"), {
                    "power": "on" if action in {"on", "open"} else "off"
                }
            if action.startswith("brightness:") and item.get("source_kind") != "switch":
                percent = int(action.split(":")[1])
                if not 1 <= percent <= 100:
                    raise ValueError("invalid_percentage")
                return "light.set", {"power": "on", "brightness": max(1, round(percent * 255 / 100))}
        if kind == "cover":
            if action in {"on", "off", "open", "close"}:
                if 'cover.state' in item.get('capabilities', []):
                    return 'cover.state', {'state': 'open' if action in {'on','open'} else 'closed'}
                return "cover.position", {"position": 100 if action in {"on", "open"} else 0}
            if action.startswith("position:"):
                return "cover.position", {"position": int(action.split(":")[1])}
        if kind == "climate":
            if action in {"off", "close"}:
                return "climate.mode", {"hvac_mode": "off"}
            if action in {"on", "open"}:
                choices = item.get('capability_specs', {}).get('climate.mode', {}).get('choices', [])
                modes = [m for m in choices if m != 'off']
                current = item.get('current')
                mode = current if current in modes else 'auto' if 'auto' in modes else modes[0] if len(modes) == 1 else None
                if mode is None:
                    raise ValueError('climate_mode_required')
                return "climate.mode", {"hvac_mode": mode}
            if action.startswith("mode:"):
                return "climate.mode", {"hvac_mode": action.split(":")[1]}
            if action.startswith("temperature:"):
                return "climate.temperature", {"temperature": float(action.split(":")[1])}
        raise ValueError("unsupported_parameters")

    async def command(self, query, action, event_id, *, room=None, preview=False, target=None, dispatch_guard=None):
        from homeplane.action_contracts import DevicePlanRequest

        items = await self.inventory()
        targets = [i for i in items if i["id"] == target and i["controllable"]] if target else resolve_targets(query, items, room=room)
        if not targets or len(targets) > 128:
            return dict(
                status="clarify",
                code="device_selection_required",
                write_attempted=False,
                reply="请说明房间和具体设备。",
                candidates=[i for i in items if i["controllable"]],
            )
        if preview:
            parameters = [
                dict(
                    target=i["id"],
                    operation=self.parameters(i, action)[0],
                    desired=self.parameters(i, action)[1],
                )
                for i in targets
            ]
            return dict(status="preview", write_attempted=False, targets=parameters)
        outcomes = []
        async with self.lock:
            for item in targets:
                if dispatch_guard is not None and dispatch_guard() is not True:
                    outcomes.append(dict(target=item['id'],status='rejected',code='stale_interaction',
                                         write_attempted=False,acknowledged=False))
                    continue
                try:
                    operation, desired = self.parameters(item, action)
                except (ValueError, TypeError, KeyError):
                    outcomes.append(dict(target=item['id'], status='rejected', code='unsupported_parameters',
                                         write_attempted=False, acknowledged=False))
                    continue
                if item.get("source_kind") == "miot":
                    if action not in {"on", "off", "open", "close"} and not (action.startswith("position:") and action[9:].isdigit() and 0<=int(action[9:])<=100):
                        outcomes.append(
                            dict(
                                target=item["id"],
                                status="rejected",
                                code="unsupported_parameters",
                                write_attempted=False,
                            )
                        )
                        continue
                    result = await self.service.miot_cover(
                        "open" if action in {"on", "open"} else "close" if action in {"off", "close"} else action, event_id
                    )
                    outcomes.append(
                        dict(
                            target=item["id"],
                            status="reported_match"
                            if result.get("reached_target")
                            else "accepted"
                            if result.get("acknowledged")
                            else "unknown_outcome",
                            code="miot_ack" if result.get("acknowledged") else "readback_unavailable",
                            write_attempted=result.get("write_attempted", result.get("acknowledged")) is True,
                            acknowledged=result.get("acknowledged") is True,
                            physical_confirmation=False,
                        )
                    )
                    continue
                snapshot = await self.core.snapshot()
                key = hashlib.sha256((event_id + ":" + item["id"]).encode()).hexdigest()
                previous = self.core.store.light_action(key=key)
                if previous is not None:
                    if (previous.request.target != item['id'] or
                            getattr(previous.request, 'operation', 'light.set') != operation or
                            previous.request.desired != desired):
                        outcomes.append(dict(target=item['id'], status='rejected', code='source_conflict',
                                             write_attempted=False, acknowledged=False))
                        continue
                    trace = previous
                else:
                    entity = next((e for e in snapshot.entities if e.id == item["id"]), None)
                    if entity is None:
                        outcomes.append(dict(target=item['id'], status='rejected', code='target_not_found',
                                             write_attempted=False, acknowledged=False))
                        continue
                    request = DevicePlanRequest(
                        target=item["id"],
                        operation=operation,
                        desired=desired,
                        expected_state_version=entity.state_version,
                        expires_at=datetime.now(UTC) + timedelta(seconds=15),
                        idempotency_key=key,
                    )
                    trace = await self.core.actions.plan(request)
                    if trace.status == "planned":
                        try:
                            trace = await self.core.actions.execute(trace.action_id, dispatch_guard=dispatch_guard)
                        except ValueError:
                            outcomes.append(dict(target=item['id'],status='rejected',code='stale_interaction',
                                                 write_attempted=False,acknowledged=False))
                            continue
                outcomes.append(
                    dict(
                        target=item["id"],
                        status=trace.status,
                        code=trace.code,
                        action_id=trace.action_id,
                        write_attempted=trace.real_write_attempted,
                        acknowledged=any(e.payload.get('code') == 'ha_service_accepted' for e in trace.events),
                        physical_confirmation=trace.physically_confirmed,
                    )
                )
        return summarize_outcomes(outcomes)


def summarize_outcomes(outcomes):
    confirmed = sum(o['status'] == 'reported_match' for o in outcomes)
    accepted = sum(o.get('acknowledged', False) for o in outcomes)
    unknown = any(o['status'] == 'unknown_outcome' for o in outcomes)
    return dict(
        status='reported_match' if outcomes and confirmed == len(outcomes) else
               'unknown_outcome' if unknown and not confirmed else
               'accepted' if outcomes and accepted == len(outcomes) else
               'partial' if accepted or confirmed else 'unavailable',
        acknowledged=bool(outcomes) and all(o.get('acknowledged', False) for o in outcomes),
        reached_target=bool(outcomes) and all(o["status"] == "reported_match" for o in outcomes),
        write_attempted=any(o["write_attempted"] for o in outcomes),
        physical_confirmation=False,
        outcomes=outcomes,
    )


def supported_actions(item):
    """Only advertise actions represented by the current writable capabilities."""
    caps = set(item.get('capabilities', []))
    kind = item['kind']
    if kind in {'light','switch','fan'}:
        return ['on','off'] if caps & {'light.set','switch.set','fan.set'} else []
    if kind == 'cover':
        actions = ['open','close'] if caps & {'cover.state','cover.position','cover.open'} else []
        return actions + (['position:'+str(p) for p in (0,25,50,75,100)] if 'cover.position' in caps else [])
    if kind == 'climate' and 'climate.mode' in caps:
        choices = item.get('capability_specs', {}).get('climate.mode', {}).get('choices', [])
        return ['off' if c == 'off' else 'mode:'+c for c in choices]
    return []
