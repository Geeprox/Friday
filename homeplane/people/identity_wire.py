"""Bounded private socketpair packets; no listening socket or serialization of code."""

import json
import math
import socket
import struct
import time

PACKET_LIMIT = 8 * 1024 * 1024
REPLY_LIMIT = 128 * 1024
UNAVAILABLE_REASONS = frozenset(
    {
        "identity_request_unavailable",
        "identity_model_unavailable",
        "identity_context_invalid",
        "identity_frame_invalid",
        "identity_frame_expired",
        "identity_protocol_invalid",
        "identity_samples_required",
        "artifact_or_native_configuration_invalid",
        "selected_model_fingerprint_mismatch",
        "optional_dependency_missing",
        "native_initialization_failed",
    }
)

def body_track_revisions(people):
    """从同一识别帧中取得可用于身份计算的人体轨迹。"""
    if people is None:
        return None
    current = {}
    for person in people:
        tracking = person.get('tracking', {})
        if not isinstance(tracking, dict) or tracking.get('overlapping', False) is not False:
            continue
        revision = tracking.get('follow_revision', 0)
        if type(revision) is int and revision >= 0:
            current[str(person['id'])] = revision
    return current


def _nonfinite(_):
    raise ValueError("identity_protocol_invalid")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("identity_deadline")
    return remaining


def send_packet(connection, value, deadline, *, limit=PACKET_LIMIT):
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError):
        raise ValueError("identity_protocol_invalid") from None
    if not 0 < len(raw) <= limit:
        raise ValueError("identity_packet_budget")
    connection.settimeout(_remaining(deadline))
    connection.sendall(struct.pack(">I", len(raw)) + raw)


def receive_packet(connection, deadline, *, limit=PACKET_LIMIT):
    def read(size):
        chunks = bytearray()
        while len(chunks) < size:
            connection.settimeout(_remaining(deadline))
            part = connection.recv(size - len(chunks))
            if not part:
                raise EOFError("identity_pipe_closed")
            chunks.extend(part)
        return chunks

    length = struct.unpack(">I", read(4))[0]
    if not 0 < length <= limit:
        raise ValueError("identity_packet_budget")
    try:
        value = json.loads(read(length), parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("identity_protocol_invalid") from None
    if not isinstance(value, dict):
        raise ValueError("identity_protocol_invalid")
    return value


def abort_socket(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        connection.close()
    except OSError:
        pass
