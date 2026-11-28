"""HA REST 与 WebSocket 共用的有界消息解码和只读状态契约。"""

import json
from datetime import datetime


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def validate_states(rows):
    if not isinstance(rows, list) or len(rows) > 10000 or any(
        not isinstance(r, dict) or not isinstance(r.get("entity_id"), str) or len(r["entity_id"]) > 256
        or not isinstance(r.get("state"), str) or len(r["state"]) > 1024
        or not isinstance(r.get("attributes", {}), dict)
        or any(r.get("attributes", {}).get(k) is not None and not isinstance(r["attributes"][k], str)
               for k in ("friendly_name", "unit_of_measurement", "device_class")) for r in rows
    ) or len({r["entity_id"] for r in rows}) != len(rows):
        raise ValueError("invalid_states")


def validate_registry(rows, kind):
    key = "entity_id" if kind == "entity" else "area_id" if kind == "area" else "id"
    if not isinstance(rows, list) or len(rows) > 10000 or any(
        not isinstance(r, dict) or not isinstance(r.get(key), str)
        or any(r.get(k) is not None and not isinstance(r[k], str)
               for k in ("id", "device_id", "area_id", "unique_id", "platform", "name")) for r in rows
    ) or len({r[key] for r in rows}) != len(rows):
        raise ValueError("invalid_registry")


def decode_json(raw):
    def reject_constant(_value):
        raise ValueError("Non-finite JSON constant")
    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid HA JSON") from exc


def parse_frame(raw):
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("HA message exceeds byte budget")
    value = decode_json(raw)
    if not isinstance(value, dict):
        raise ValueError("HA message must be an object")
    return value
