"""Hash-pinned ARM64 adapter for the SDK's existing authenticated MISS session.

REA recovered the four-argument miss_cmd_send ABI, camera.session at +0x38,
and camera.state at +0x40. Only directional motor opcode 0x112 is exposed.
This is an experimental internal ABI, never a generic command interface.
"""

import ctypes as c
import hashlib
import json
import platform
import time
from pathlib import Path

SHA256 = "df4610cae665ec2f4dbc40f203d0a59d6161f18fbd551219bdf853ab101bf4f0"
NEW = 0x1B10
SEND = 0x6F570


class LocalMotor:
    def __init__(self, camera):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("unsupported_motor_host")
        self.camera = camera
        lib = camera._lib_miot_camera
        path = Path(lib._name)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError("motor_sdk_hash_changed")
        self.base = c.cast(lib.miot_camera_new, c.c_void_p).value - NEW
        if self.base % 4096 or c.string_at(self.base + SEND, 16) != data[SEND : SEND + 16]:
            raise RuntimeError("motor_address_verification_failed")
        self.send = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_uint32, c.c_char_p, c.c_size_t)(self.base + SEND)
        self.last = -10.0

    def step(self, direction):
        operation = {"left": 1, "right": 2, "up": 3, "down": 4}.get(direction)
        if operation is None:
            raise ValueError("direction_not_allowed")
        now = time.monotonic()
        if now - self.last < 0.7:
            raise RuntimeError("motor_rate_limited")
        outer = self.camera._c_instance.value
        if not outer or c.c_uint32.from_address(outer + 0x40).value != 4:
            raise RuntimeError("camera_not_connected")
        session = c.c_void_p.from_address(outer + 0x38).value
        if not session or c.c_uint32.from_address(session).value != 4:
            raise RuntimeError("motor_session_not_ready")
        payload = json.dumps({"operation": operation}, separators=(",", ":")).encode()
        self.last = now
        began = time.monotonic()
        code = self.send(session, 0x112, payload, len(payload))
        return {
            "direction": direction,
            "native_code": code,
            "send_ms": round((time.monotonic() - began) * 1000, 2),
            "transport": "existing_local_miss_session",
            "physical_movement_verified": False,
        }
