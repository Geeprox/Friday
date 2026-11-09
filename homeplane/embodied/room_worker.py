"""Stateful native Room owner. Its private pipe never records live frames."""

import base64
import json
import os
import re
import sys
from pathlib import Path

from .room_protocol import MAX_JPEG, MAX_REPLY, MAX_REQUEST, packet


def open_room(folder, picture):
    os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"] = str(4096 * 4096)
    from .room_engine import load_room

    return load_room(folder, picture)


def visible(picture):
    from .camera_director import visible_scene

    return bool(visible_scene(picture))


def draft(room):
    if room.registration is None:
        return None
    import numpy as np

    from .room_engine import calibration_draft, project

    result = calibration_draft(room.calibration_candidate)
    inverse = np.linalg.inv(room.registration)
    for item in result["anchors"] + result["seats"]:
        point = project(inverse, item["image"])
        if point:
            item["image"] = point
    return result


def serve(reader, writer, folder, *, factory=open_room, visibility=visible, draft_factory=draft):
    room = None
    nonce, sequence, frame = None, 0, None
    scene_visible = False
    while True:
        raw = reader.readline(MAX_REQUEST + 1)
        if not raw or len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            return
        try:
            request = json.loads(raw)
            if (
                not isinstance(request, dict)
                or set(request) != {"v", "nonce", "sequence", "action", "frame", "value"}
                or type(request["v"]) is not int
                or request["v"] != 1
                or type(request["sequence"]) is not int
                or request["sequence"] != sequence + 1
                or not isinstance(request["nonce"], str)
                or not re.fullmatch("[a-f0-9]{32}", request["nonce"])
                or nonce is not None
                and nonce != request["nonce"]
                or not isinstance(request["frame"], str)
                or not re.fullmatch("[a-f0-9]{32}", request["frame"])
                or not isinstance(request["value"], dict)
            ):
                return
            nonce, sequence = request["nonce"], request["sequence"]
            action, value = request["action"], request["value"]
            if action in {"open", "register"}:
                jpeg = value.get("jpeg")
                if not isinstance(jpeg, str) or len(jpeg) > (MAX_JPEG + 2) // 3 * 4:
                    return
                picture = base64.b64decode(jpeg, validate=True)
                if not 0 < len(picture) <= MAX_JPEG:
                    return
            if action == "open" and room is None and sequence == 1 and set(value) == {"jpeg"}:
                room = factory(folder, picture)
                result = {}
            elif (
                action == "register"
                and room is not None
                and set(value) == {"jpeg", "masks", "now", "motion_until", "reset"}
            ):
                if (
                    not isinstance(value["masks"], list)
                    or len(value["masks"]) > 64
                    or type(value["reset"]) is not bool
                ):
                    return
                if value["reset"]:
                    room.players.clear()
                    room.pending.clear()
                    room.holds.clear()
                    room.next_seat = 1
                if value["now"] < value["motion_until"]:
                    room.expect_motion(value["now"], value["motion_until"] - value["now"])
                room.register(picture, value["masks"], value["now"])
                scene_visible = visibility(picture)
                frame = request["frame"]
                result = dict(
                    matrix=room.registration.tolist() if room.registration is not None else None,
                    info=room.reg_info,
                    command_until=room.command_until,
                    visible=scene_visible,
                )
            elif (
                action == "layout"
                and room is not None
                and request["frame"] == frame
                and set(value) == {"people", "hands", "now", "width", "height", "objects"}
            ):
                people, hands = value["people"], value["hands"]
                if (
                    not isinstance(people, list)
                    or len(people) > 16
                    or not isinstance(hands, list)
                    or len(hands) > 32
                ):
                    return
                layout = room.update(
                    b"", people, hands, value["now"], value["width"], value["height"], value["objects"], True
                )
                result = dict(layout=layout, people=people, hands=hands, draft=draft_factory(room))
            else:
                return
            writer.write(
                packet(
                    dict(
                        v=1,
                        nonce=nonce,
                        sequence=sequence,
                        action=action,
                        frame=request["frame"],
                        result=result,
                    ),
                    MAX_REPLY,
                )
            )
            writer.flush()
            # The image is an ephemeral input, not an idle worker cache.
            if action in {"open", "register"}:
                del picture, jpeg
            del raw, request, value, result
        except Exception:
            return  # Fixed parent diagnostic; never emit private native errors.


if __name__ == "__main__" and len(sys.argv) == 2:
    serve(sys.stdin.buffer, sys.stdout.buffer, Path(sys.argv[1]))
