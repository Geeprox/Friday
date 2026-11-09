"""Derive a private, exact-build silent-start fix; never contacts a device.

This is a firmware input for an authorized platform integration, not an APK
installer. The cache bridge is a separate requirement. Original vendor bytes
are neither included in this repository nor modified in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA256 = "17b3aad1b5f73d0e425cc10e032c55f19061a69eff7757fe7af8bb0f034d3cac"
PATCHED_SHA256 = "e6d083de0c2d1c1aa76d9abca054d58570405ccc3d2a8710fc4f4b1646c00466"
RX_SHA256 = "618a2f2d4a60b17d0d3e52167d564f8064a2129314a97efffbce5273f30c4ef8"
RX_LENGTH = 614408
PATCH_OFFSET = 0x39A54
OLD_WORD = 0x1E624003  # fcvt s3,d0: pow(10,0/20) produced unity.
NEW_WORD = 0x1E2703E3  # fmov s3,wzr: both subsequent gain stores receive zero.
PAYLOAD_NAME = "libfridaygm.payload.so"
PAYLOAD_SONAME_OFFSET = 0x1D475
PAYLOAD_FLAGS_OFFSET = 0x9A598
PAYLOAD_SHA256 = "1bb2966772f03d4c341c9fbe268a0b273970e0fe72cc2ce79cca69819631cdc3"
PAYLOAD_RX_SHA256 = "c40ec407e67879bc712cd73526cda573fa28dfc8aaffdfd49dc849d27137332d"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def silent_constructor(value: bytes, offset: int = PATCH_OFFSET) -> bytes:
    """One instruction only; the existing two stores and all setters remain."""
    if offset < 0 or offset + 4 > len(value) or offset % 4:
        raise ValueError("constructor_instruction_unavailable")
    if struct.unpack_from("<I", value, offset)[0] != OLD_WORD:
        raise ValueError("constructor_instruction_changed")
    result = bytearray(value)
    struct.pack_into("<I", result, offset, NEW_WORD)
    return bytes(result)


def validate_layout(value: bytes) -> None:
    if len(value) < 64:
        raise ValueError("vendor_layout_changed")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", value)
    if (header[0][:7] != b"\x7fELF\x02\x01\x01" or header[1:4] != (3, 183, 1)
            or header[5] != 64 or header[9:11] != (56, 8)):
        raise ValueError("vendor_layout_changed")
    loads = [struct.unpack_from("<IIQQQQQQ", value, 64 + i * 56) for i in range(8)]
    if loads[0] != (1, 5, 0, 0, 0, RX_LENGTH, RX_LENGTH, 4096):
        raise ValueError("vendor_rx_layout_changed")
    # The running-image RX digest must remain meaningful after loading. No
    # dynamic relocation is permitted to write into the executable load range.
    shoff, shsize, shcount = header[6], header[11], header[12]
    if shsize != 64 or shoff + shcount * shsize > len(value):
        raise ValueError("vendor_sections_changed")
    for index in range(shcount):
        section = struct.unpack_from("<IIQQQQIIQQ", value, shoff + index * shsize)
        if section[1] not in (4, 9):
            continue
        if section[1] != 4 or section[9] != 24 or section[4] + section[5] > len(value):
            raise ValueError("vendor_relocations_changed")
        for offset in range(section[4], section[4] + section[5], 24):
            destination = struct.unpack_from("<Q", value, offset)[0]
            if 0 <= destination < RX_LENGTH:
                raise ValueError("vendor_rx_relocation")


def derive(value: bytes) -> tuple[bytes, dict]:
    if _sha(value) != SOURCE_SHA256:
        raise ValueError("exact_reviewed_vendor_build_required")
    validate_layout(value)
    patched = silent_constructor(value)
    if _sha(patched) != PATCHED_SHA256 or _sha(patched[:RX_LENGTH]) != RX_SHA256:
        raise ValueError("derived_vendor_digest_changed")
    return patched, {
        "schema": 1, "profile": "globalmic-1.1.3-arm64-silent-constructor-v1",
        "source_sha256": SOURCE_SHA256, "patched_sha256": PATCHED_SHA256,
        "mapped_rx_sha256": RX_SHA256, "mapped_rx_bytes": RX_LENGTH,
        "instruction_rva": PATCH_OFFSET, "before_word": OLD_WORD, "after_word": NEW_WORD,
        "initial_mic_gain": 0, "initial_system_gain": 0,
        "gain_setter_behavior_changed": False, "dry_fifo_added_frames": 0,
        "strict_cache_bridge_required": True, "legacy_cache_hook_approved": False,
        "system_deployment_required": True, "ordinary_apk_can_install_system_fix": False,
        "device_contacted": False, "original_modified": False,
        "system_image_modified": False, "acoustic_validation": False,
    }


def derive_payload(value: bytes) -> tuple[bytes, dict]:
    """Private system-module payload: silent start and independently bound name.

    The shim forwards the original eleven slots. DF_SYMBOLIC asks Bionic to
    bind self definitions locally; the shim additionally checks real relocated
    pointers before exposing any ops, so this flag alone is never proof.
    """
    patched, receipt = derive(value)
    result = bytearray(patched)
    original = b"libtlkg.hal.globalmic.so\0"
    name = PAYLOAD_NAME.encode() + b"\0"
    if (result[PAYLOAD_SONAME_OFFSET:PAYLOAD_SONAME_OFFSET + len(original)] != original
            or struct.unpack_from("<QQ", result, PAYLOAD_FLAGS_OFFSET - 8) != (30, 8)):
        raise ValueError("payload_dynamic_profile_changed")
    result[PAYLOAD_SONAME_OFFSET:PAYLOAD_SONAME_OFFSET + len(original)] = name.ljust(len(original), b"\0")
    struct.pack_into("<Q", result, PAYLOAD_FLAGS_OFFSET, 10)
    body = bytes(result)
    if _sha(body) != PAYLOAD_SHA256 or _sha(body[:RX_LENGTH]) != PAYLOAD_RX_SHA256:
        raise ValueError("derived_payload_digest_changed")
    receipt.update(profile="globalmic-1.1.3-arm64-system-module-payload-v1",
                   patched_sha256=PAYLOAD_SHA256, mapped_rx_sha256=PAYLOAD_RX_SHA256,
                   payload_name=PAYLOAD_NAME, payload_soname_offset=PAYLOAD_SONAME_OFFSET,
                   payload_flags_offset=PAYLOAD_FLAGS_OFFSET, dt_flags_before=8, dt_flags_after=10,
                   self_symbolic_requested=True, runtime_relocation_verification_required=True,
                   system_module_shim_required=True)
    return body, receipt


def prepare(source: Path, output: Path, *, system_payload: bool = False) -> dict:
    source, output = source.absolute(), output.absolute()
    info = source.lstat()
    if source.resolve() != source or not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
        raise ValueError("canonical_vendor_file_required")
    patched, receipt = (derive_payload if system_payload else derive)(source.read_bytes())
    if output.exists() or output.is_symlink() or output.parent.resolve() != output.parent:
        raise ValueError("new_private_output_required")
    output.mkdir(mode=0o700)
    # Exclusive creation also prevents replacing an existing output or linking a
    # derived vendor image into a public source path by accident.
    for name, body in (((PAYLOAD_NAME if system_payload else "libtlkg.hal.globalmic.so"), patched),
                       ("receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())):
        with (output / name).open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(body)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--system-payload", action="store_true", help="prepare the exact system module payload; never installs it")
    args = parser.parse_args()
    destination = args.output.absolute()
    if not any(destination.is_relative_to(ROOT / name) for name in (".artifacts", "data")):
        parser.error("private_workspace_output_required")
    receipt = prepare(args.source, destination, system_payload=args.system_payload)
    print(json.dumps({key: receipt[key] for key in (
        "profile", "patched_sha256", "mapped_rx_sha256", "device_contacted",
        "original_modified", "strict_cache_bridge_required", "system_deployment_required",
    )}))


if __name__ == "__main__":
    main()
