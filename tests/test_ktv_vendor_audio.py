"""Pure byte/layout tests. No native image is loaded and no device is used."""
import hashlib
import struct
from pathlib import Path

import pytest

from scripts.prepare_ktv_vendor_audio import (
    NEW_WORD,
    OLD_WORD,
    PATCH_OFFSET,
    PATCHED_SHA256,
    PAYLOAD_FLAGS_OFFSET,
    PAYLOAD_NAME,
    PAYLOAD_RX_SHA256,
    PAYLOAD_SHA256,
    PAYLOAD_SONAME_OFFSET,
    RX_LENGTH,
    RX_SHA256,
    derive,
    derive_payload,
    prepare,
    silent_constructor,
)


def test_constructor_patches_only_producer_of_both_gain_stores():
    before = struct.pack("<IIIII", 0xAAAAAAAA, OLD_WORD, 0xBD104903, 0xBD504903, 0xBBBBBBBB)
    after = silent_constructor(before, 4)
    assert after[:4] == before[:4] and after[8:] == before[8:]
    assert struct.unpack_from("<I", after, 4)[0] == NEW_WORD
    # AArch64 FMOV S3,WZR encodes register 31 (constant zero) and destination S3.
    assert (NEW_WORD >> 5) & 31 == 31 and NEW_WORD & 31 == 3


@pytest.mark.parametrize("offset", [-4, 1, 4, 8])
def test_instruction_boundaries_reject_unknown_or_partial_input(offset):
    with pytest.raises(ValueError):
        silent_constructor(struct.pack("<I", OLD_WORD), offset)


def test_unknown_binary_cannot_be_patched_even_with_matching_instruction():
    value = b"\0" * PATCH_OFFSET + struct.pack("<I", OLD_WORD)
    with pytest.raises(ValueError, match="exact_reviewed_vendor_build_required"):
        derive(value)


def test_rejected_input_does_not_create_output_or_modify_source(tmp_path):
    source = tmp_path / "source.so"
    source.write_bytes(b"unreviewed")
    output = tmp_path / "candidate"
    with pytest.raises(ValueError):
        prepare(source, output)
    assert source.read_bytes() == b"unreviewed" and not output.exists()
    link = tmp_path / "link.so"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="canonical_vendor_file_required"):
        prepare(link, output)
    assert not output.exists()


def test_local_reviewed_image_when_explicitly_available(tmp_path):
    # CI has no proprietary image. Locally this verifies the actual frozen
    # build as well as the public synthetic instruction-boundary tests above.
    source = Path(__file__).resolve().parents[1] / ".artifacts/ktv-latency-20261031/reverse/libtlkg.hal.globalmic.so"
    if not source.is_file():
        pytest.skip("local vendor evidence not included in open-source tests")
    original = source.read_bytes()
    patched, receipt = derive(original)
    assert hashlib.sha256(patched).hexdigest() == PATCHED_SHA256
    assert hashlib.sha256(patched[:RX_LENGTH]).hexdigest() == RX_SHA256
    assert patched[:PATCH_OFFSET] == original[:PATCH_OFFSET]
    assert patched[PATCH_OFFSET + 4:] == original[PATCH_OFFSET + 4:]
    assert receipt["device_contacted"] is False
    output = tmp_path / "derived"
    prepare(source, output)
    assert (output / "libtlkg.hal.globalmic.so").read_bytes() == patched
    assert (output.stat().st_mode & 0o777) == 0o700
    assert ((output / "libtlkg.hal.globalmic.so").stat().st_mode & 0o777) == 0o600
    with pytest.raises(ValueError, match="new_private_output_required"):
        prepare(source, output)


def test_exact_system_payload_has_only_three_bounded_changes(tmp_path):
    source = Path(__file__).resolve().parents[1] / ".artifacts/ktv-latency-20261031/reverse/libtlkg.hal.globalmic.so"
    if not source.is_file():
        pytest.skip("local vendor evidence not included in open-source tests")
    original = source.read_bytes()
    payload, receipt = derive_payload(original)
    allowed = set(range(PATCH_OFFSET, PATCH_OFFSET + 4))
    allowed.update(range(PAYLOAD_SONAME_OFFSET, PAYLOAD_SONAME_OFFSET + 25))
    allowed.update(range(PAYLOAD_FLAGS_OFFSET, PAYLOAD_FLAGS_OFFSET + 8))
    assert all(a == b or i in allowed for i, (a, b) in enumerate(zip(original, payload, strict=True)))
    assert hashlib.sha256(payload).hexdigest() == PAYLOAD_SHA256
    assert hashlib.sha256(payload[:RX_LENGTH]).hexdigest() == PAYLOAD_RX_SHA256
    assert payload[PAYLOAD_SONAME_OFFSET:PAYLOAD_SONAME_OFFSET + 25].split(b"\0")[0].decode() == PAYLOAD_NAME
    assert struct.unpack_from("<Q", payload, PAYLOAD_FLAGS_OFFSET)[0] == 10
    assert receipt["runtime_relocation_verification_required"]
    output = tmp_path / "system-payload"
    prepare(source, output, system_payload=True)
    assert (output / PAYLOAD_NAME).read_bytes() == payload
    assert source.read_bytes() == original
