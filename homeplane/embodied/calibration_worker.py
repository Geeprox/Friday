"""Native calibration validation and atomic commit, over one private pipe lease."""

import base64
import json
import os
import re
import sys
from pathlib import Path

from .calibration_store import MAX_BUNDLE, CalibrationStore, encode, manual_config


def validate(config, reference):
    os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"] = str(4096 * 4096)
    from .room_engine import Room

    Room(config, reference)


def serve(reader, writer, folder, *, validator=validate, store_factory=CalibrationStore):
    nonce = None

    def reply(kind, **values):
        writer.write(
            (json.dumps(dict(type=kind, nonce=nonce, **values), separators=(",", ":")) + "\n").encode()
        )
        writer.flush()

    try:
        raw = reader.readline(MAX_BUNDLE + 1)
        if len(raw) > MAX_BUNDLE or not raw.endswith(b"\n"):
            return
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"v", "nonce", "config", "reference_jpeg"}
            or type(value["v"]) is not int
            or value["v"] != 1
            or not isinstance(value["nonce"], str)
            or not re.fullmatch("[a-f0-9]{32}", value["nonce"])
            or not isinstance(value["config"], dict)
            or not isinstance(value["reference_jpeg"], str)
        ):
            return
        nonce = value["nonce"]
        config = manual_config(value["config"])
        reference = base64.b64decode(value["reference_jpeg"], validate=True)
        _, digest = encode(config, reference)
        validator(config, reference)
        reply("prepared", revision=digest)
        commit = reader.readline(512)
        if (
            not commit.endswith(b"\n")
            or len(commit) >= 512
            or json.loads(commit) != dict(action="commit", nonce=nonce, revision=digest)
        ):
            return
        store_factory(folder).save(config, reference, nonce=nonce)
        reply("saved", revision=digest)
    except (ValueError, TypeError, KeyError, RecursionError):
        reply("error", reason="calibration_invalid")
    except Exception:
        reply("error", reason="calibration_unavailable")


if __name__ == "__main__" and len(sys.argv) == 2:
    serve(sys.stdin.buffer, sys.stdout.buffer, Path(sys.argv[1]))
