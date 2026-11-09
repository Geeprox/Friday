"""Private face-computation child. Importing it starts no model or database."""

import argparse
import base64
import math
import os
import re
import socket
import time
from pathlib import Path

from .identity_wire import REPLY_LIMIT, UNAVAILABLE_REASONS, receive_packet, send_packet

REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JPEG_LIMIT = 4 * 1024 * 1024


def frame_arguments(value):
    if set(value) != {"jpeg", "people", "faces", "moving", "sample", "captured_at"}:
        raise ValueError("identity_frame_invalid")
    encoded, people, faces = value["jpeg"], value["people"], value["faces"]
    if (
        not isinstance(encoded, str)
        or len(encoded) > ((JPEG_LIMIT + 2) // 3) * 4
        or people is not None
        and (not isinstance(people, list) or len(people) > 16)
        or not isinstance(faces, list)
        or len(faces) > 32
        or type(value["moving"]) is not bool
        or type(value["sample"]) is not bool
        or type(value["captured_at"]) not in (int, float)
        or not math.isfinite(value["captured_at"])
    ):
        raise ValueError("identity_frame_invalid")
    if not 0 <= time.monotonic() - value["captured_at"] <= 1:
        raise ValueError("identity_frame_expired")
    jpeg = base64.b64decode(encoded, validate=True)
    if len(jpeg) > JPEG_LIMIT or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("identity_frame_invalid")
    return jpeg, people, faces, {key: value[key] for key in ("moving", "sample", "captured_at")}


def open_pipeline(value):
    # This boundary is reached only by the owning camera's first face request.
    from .live import FaceIdentityPipeline, IdentityStore

    if (
        set(value) != {"database", "models", "camera", "session"}
        or not all(isinstance(value[k], str) for k in value)
        or not REF.fullmatch(value["camera"])
        or not REF.fullmatch(value["session"])
    ):
        raise ValueError("identity_context_invalid")
    store = IdentityStore(Path(value["database"]))
    pipeline = None
    try:
        pipeline = FaceIdentityPipeline(store, value["camera"], value["session"], Path(value["models"]))
        if not pipeline.activate():
            raise ValueError(
                pipeline.reason if pipeline.reason in UNAVAILABLE_REASONS else "identity_model_unavailable"
            )
        return pipeline, store
    except BaseException:
        try:
            if pipeline is not None:
                pipeline.close()
        finally:
            store.close()
        raise


def serve(connection, *, pipeline_factory=open_pipeline):
    """Fixed commands only. The factory is trusted test injection, never wire data."""
    pipeline = store = None
    nonce, sequence = None, 0
    try:
        while True:
            request = receive_packet(connection, time.monotonic() + 60)
            if (
                set(request) != {"v", "nonce", "sequence", "action", "value"}
                or type(request["v"]) is not int
                or request["v"] != 1
                or type(request["sequence"]) is not int
                or request["sequence"] != sequence + 1
                or not isinstance(request["nonce"], str)
                or not re.fullmatch("[a-f0-9]{32}", request["nonce"])
                or nonce is not None
                and request["nonce"] != nonce
                or not isinstance(request["value"], dict)
            ):
                raise ValueError("identity_protocol_invalid")
            nonce, sequence = request["nonce"], request["sequence"]
            action, value = request["action"], request["value"]
            result = {}
            try:
                if action == "init" and pipeline is None and sequence == 1:
                    pipeline, store = pipeline_factory(value)
                    result = {"reason": pipeline.reason}
                elif pipeline is None:
                    raise ValueError("identity_context_invalid")
                elif action == "frame":
                    jpeg, people, faces, options = frame_arguments(value)
                    rows = pipeline.process(jpeg, people, faces, **options)
                    result = dict(rows=rows, reason=pipeline.reason, diagnostics=pipeline.diagnostics())
                elif action == "discard" and not value:
                    pipeline.discard()
                elif action == "enroll" and set(value) == {"track", "person", "expires"}:
                    if (
                        not all(
                            isinstance(value[k], str) and REF.fullmatch(value[k]) for k in ("track", "person")
                        )
                        or type(value["expires"]) not in (int, float)
                        or not 0 <= value["expires"] - time.monotonic() <= 2
                    ):
                        raise ValueError("identity_samples_required")
                    result = pipeline.enroll(value["track"], value["person"])
                else:
                    raise ValueError("identity_protocol_invalid")
                reply = dict(v=1, nonce=nonce, sequence=sequence, action=action, ok=True, result=result)
            except Exception as error:
                # Never transmit tracebacks, paths, model text or household data.
                reason = (
                    str(error)
                    if isinstance(error, ValueError) and str(error) in UNAVAILABLE_REASONS
                    else "identity_request_unavailable"
                )
                if action == "frame" and pipeline is not None and reason == "identity_frame_expired":
                    reply = dict(
                        v=1,
                        nonce=nonce,
                        sequence=sequence,
                        action=action,
                        ok=True,
                        result=dict(rows=[], reason=pipeline.reason, diagnostics=pipeline.diagnostics()),
                    )
                    send_packet(connection, reply, time.monotonic() + 2, limit=REPLY_LIMIT)
                    continue
                reply = dict(
                    v=1,
                    nonce=nonce,
                    sequence=sequence,
                    action=action,
                    ok=False,
                    result={"reason": reason},
                )
            send_packet(connection, reply, time.monotonic() + 2, limit=REPLY_LIMIT)
            if not reply["ok"]:
                break
    finally:
        try:
            if pipeline is not None:
                pipeline.close()
        finally:
            if store is not None:
                store.close()
            connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    args = parser.parse_args()
    if args.fd < 3 or args.owner_pid <= 1 or os.getppid() != args.owner_pid:
        parser.error("invalid_identity_owner")
    from homeplane.embodied.camera_guardian import exit_with_owner

    exit_with_owner(args.owner_pid)
    try:
        serve(socket.socket(fileno=args.fd))
    except (OSError, EOFError, ValueError, TimeoutError):
        pass
