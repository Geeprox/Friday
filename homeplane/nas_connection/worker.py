"""Fixed read-only stdin/stdout protocol. Credentials enter only the initial frame."""

import json
import logging
import sys
from pathlib import Path

# -I script execution: add only this checkout's package root, not environment paths.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from homeplane.nas_connection.adapters import SFTPReader, SMBReader
from homeplane.nas_connection.contracts import NASConnectionConfig, NASConnectionError, NASCredentials

MAX_INPUT = 32768
MAX_OUTPUT = 2 * 1024 * 1024


def error_code(exc):
    if isinstance(exc, NASConnectionError):
        return exc.code
    if isinstance(exc, ImportError):
        return "nas_dependency_missing"
    if isinstance(exc, FileNotFoundError):
        return "nas_missing"
    if isinstance(exc, PermissionError):
        return "nas_access_denied"
    if isinstance(exc, TimeoutError):
        return "nas_timeout"
    # Library response status codes, never exception strings or server messages.
    code = getattr(exc, "status", None)
    if code in {0xC000006D, 0xC000006A, 0xC0000071, 0xC0000072, 0xC0000193, 0xC0000234}:
        return "nas_auth_failed"
    if code in {0xC000000F, 0xC0000034, 0xC000003A}:
        return "nas_missing"
    if code == 0xC0000022:
        return "nas_access_denied"
    try:
        import paramiko
        if isinstance(exc, paramiko.AuthenticationException):
            return "nas_auth_failed"
        if isinstance(exc, paramiko.BadHostKeyException):
            return "nas_host_key_mismatch"
    except ImportError:
        pass
    return "nas_transport_failed"


def main(factory=None):
    logging.disable(logging.CRITICAL)
    reader = None
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_INPUT + 1)
            if not line:
                return
            identity = None
            try:
                if len(line) > MAX_INPUT or not line.endswith(b"\n"):
                    raise NASConnectionError("nas_budget_exceeded")
                message = json.loads(line)
                if set(message) != {"id", "op", "payload"} or type(message["id"]) is not int:
                    raise NASConnectionError("nas_invalid_request")
                identity, operation, payload = message["id"], message["op"], message["payload"]
                if operation == "connect" and reader is None:
                    config = NASConnectionConfig.model_validate(payload["config"])
                    credentials = NASCredentials(**payload["credentials"])
                    credentials.payload()
                    selected = factory or (SMBReader if config.transport == "smb" else SFTPReader)
                    reader = selected(config, credentials)
                    result = reader.health()
                    del credentials
                elif reader is None:
                    raise NASConnectionError("nas_unavailable")
                elif operation == "health" and payload == {}:
                    result = reader.health()
                elif operation == "list" and set(payload) == {"path", "cursor", "limit"}:
                    result = reader.list_page(**payload)
                elif operation == "stat" and set(payload) == {"path"}:
                    result = reader.stat(**payload)
                elif operation == "read" and set(payload) == {"path", "revision", "offset", "length"}:
                    result = reader.read_range(**payload)
                else:
                    raise NASConnectionError("nas_invalid_request")
                reply = {"id": identity, "result": result}
            except Exception as exc:
                reply = {"id": identity, "error": error_code(exc)}
            # Drop the serialized password/config before accepting another frame.
            line = message = payload = None
            output = json.dumps(reply).encode() + b"\n"
            if len(output) > MAX_OUTPUT:
                output = json.dumps({"id": identity, "error": "nas_budget_exceeded"}).encode() + b"\n"
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()
            if identity is None:
                return
    finally:
        if reader:
            reader.close()


if __name__ == "__main__":
    main()
