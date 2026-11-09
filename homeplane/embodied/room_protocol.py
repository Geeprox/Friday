"""Bounded private Room pipe values. No native imports or inferred geometry."""

import json
import math

MAX_REQUEST = 3_000_000
MAX_REPLY = 512_000
MAX_JPEG = 2_000_000
STATUSES = {
    "checking",
    "locked",
    "moving",
    "settling",
    "camera_moved",
    "background_unavailable",
    "unavailable",
}
MODES = {"unconfigured", "relative_calibrated", "approximate"}
PERSON_FIELDS = {"position", "hands", "seat", "motion_anchor", "motion_torso"}
HAND_FIELDS = {"person_id", "gesture", "raw_gesture", "ray"}


def tree(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 14 or count[0] > 50000:
        raise ValueError("room_protocol_invalid")
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        if not -1e12 <= value <= 1e12 or not math.isfinite(value):
            raise ValueError("room_protocol_invalid")
    elif isinstance(value, str):
        if len(value) > MAX_REQUEST:
            raise ValueError("room_protocol_invalid")
    elif isinstance(value, list):
        if len(value) > 512:
            raise ValueError("room_protocol_invalid")
        for item in value:
            tree(item, depth + 1, count)
    elif isinstance(value, dict):
        if len(value) > 128 or any(not isinstance(k, str) or len(k) > 64 for k in value):
            raise ValueError("room_protocol_invalid")
        for item in value.values():
            tree(item, depth + 1, count)
    else:
        raise ValueError("room_protocol_invalid")


def packet(value, limit):
    tree(value)
    raw = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > limit:
        raise ValueError("room_protocol_invalid")
    return raw


def metadata(value):
    if not isinstance(value, dict) or set(value) != {"matrix", "info", "command_until", "visible"}:
        raise ValueError("room_protocol_invalid")
    tree(value)
    if (
        not isinstance(value["info"], dict)
        or value["info"].get("status") not in STATUSES
        or type(value["visible"]) is not bool
        or type(value["command_until"]) not in (int, float)
    ):
        raise ValueError("room_protocol_invalid")
    matrix = value["matrix"]
    if matrix is not None:
        if (
            value["info"]["status"] != "locked"
            or not isinstance(matrix, list)
            or len(matrix) != 3
            or any(not isinstance(row, list) or len(row) != 3 for row in matrix)
            or any(type(n) not in (int, float) or not -1e8 <= n <= 1e8 for row in matrix for n in row)
        ):
            raise ValueError("room_protocol_invalid")
    return value


def layout_result(value, people, hands):
    if not isinstance(value, dict) or set(value) != {"layout", "people", "hands", "draft"}:
        raise ValueError("room_protocol_invalid")
    tree(value)
    layout = value["layout"]
    if (
        not isinstance(layout, dict)
        or layout.get("mode") not in MODES
        or layout.get("metric") is not False
        or not isinstance(layout.get("registration"), dict)
        or layout["registration"].get("status") not in STATUSES
        or not isinstance(layout.get("seats"), list)
        or len(layout["seats"]) > 12
        or not isinstance(layout.get("pointing"), list)
        or len(layout["pointing"]) > 32
        or not isinstance(value["people"], list)
        or len(value["people"]) != len(people)
        or not isinstance(value["hands"], list)
        or len(value["hands"]) != len(hands)
        or value["draft"] is not None
        and not isinstance(value["draft"], dict)
    ):
        raise ValueError("room_protocol_invalid")
    for original, reply, fields in [(a, b, PERSON_FIELDS) for a, b in zip(people, value["people"])] + [
        (a, b, HAND_FIELDS) for a, b in zip(hands, value["hands"])
    ]:
        if not isinstance(reply, dict) or {k: v for k, v in original.items() if k not in fields} != {
            k: v for k, v in reply.items() if k not in fields
        }:
            raise ValueError("room_protocol_invalid")
    return value


def unavailable_layout():
    return dict(
        mode="unconfigured",
        position_status="unconfigured",
        metric=False,
        note="空间信息暂不可用",
        registration={"status": "unavailable"},
        seats=[],
        furniture=[],
        outline=[],
        pointing=[],
        observed_furniture=[],
        seat_mode="dynamic_players",
        camera=None,
    )
