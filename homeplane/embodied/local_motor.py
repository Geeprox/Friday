"""固定发行摘要的本地云台接口；macOS 与 Linux ABI 分别核验。

只复用摄像头已有授权会话，暴露四个方向的短步进，不提供通用命令入口。
Linux 地址来自对应 x86_64 ELF 的静态调用链，不能移用于其他版本。
"""

import ctypes as c
import hashlib
import json
import math
import platform
import time
from pathlib import Path

SHA256 = "df4610cae665ec2f4dbc40f203d0a59d6161f18fbd551219bdf853ab101bf4f0"
NEW = 0x1B10
SEND = 0x6F570
LINUX_SHA256 = "fde805072a663d975da26e1b91836acad2da9c9e19038e382ad4118871c65ecc"
LINUX_NEW = 0x86DE0
LINUX_SEND = 0x895A0


def abi_profile(system=None, machine=None):
    """没有精确匹配的架构与发行包时拒绝调用，不猜测函数地址。"""
    target = (system or platform.system(), machine or platform.machine())
    if target == ('Darwin', 'arm64'):
        return SHA256, NEW, SEND
    if target == ('Linux', 'x86_64'):
        return LINUX_SHA256, LINUX_NEW, LINUX_SEND
    raise RuntimeError('unsupported_motor_host')


def verify_binary(data, system=None, machine=None):
    digest, anchor, send = abi_profile(system, machine)
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError('motor_sdk_hash_changed')
    if (system or platform.system()) == 'Linux':
        if data[:6] != b'\x7fELF\x02\x01' or data[18:20] != b'\x3e\x00':
            raise RuntimeError('motor_sdk_format_changed')
    return anchor, send


def check_motor_deadline(expires_mono):
    """Parent and child share the host monotonic clock; no queued old impulses."""
    if type(expires_mono) not in (int, float) or not math.isfinite(expires_mono):
        raise ValueError('invalid_motor_deadline')
    remaining = expires_mono-time.monotonic()
    if remaining <= 0:
        raise RuntimeError('motor_request_expired')
    if remaining > 1.5:
        raise ValueError('invalid_motor_deadline')


class LocalMotor:
    def __init__(self, camera):
        abi_profile()
        self.camera = camera
        lib = camera._lib_miot_camera
        path = Path(lib._name)
        data = path.read_bytes()
        anchor, send = verify_binary(data)
        self.base = c.cast(lib.miot_camera_new, c.c_void_p).value - anchor
        if self.base <= 0 or self.base % 4096 or c.string_at(self.base + send, 32) != data[send : send + 32]:
            raise RuntimeError("motor_address_verification_failed")
        self.send = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_uint32, c.c_char_p, c.c_uint32)(self.base + send)
        self.last = -10.0

    def step(self, direction, *, expires_mono):
        operation = {"left": 1, "right": 2, "up": 3, "down": 4}.get(direction)
        if operation is None:
            raise ValueError("direction_not_allowed")
        check_motor_deadline(expires_mono)
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
        # A worker-pool wait or delayed SDK/session inspection may outlive the
        # decision. Refuse it at the final native boundary as well.
        check_motor_deadline(expires_mono)
        self.last = time.monotonic()
        began = time.monotonic()
        code = self.send(session, 0x112, payload, len(payload))
        return {
            "direction": direction,
            "native_code": code,
            "send_ms": round((time.monotonic() - began) * 1000, 2),
            "transport": "existing_local_miss_session",
            "physical_movement_verified": False,
        }
