"""Receipt-consumer regressions only: saved observations and explicit protocol doubles.

No native helper/compile/perform, live source, or new PDFKit/OCR qualification.
Only this file is run in the repair; frozen tests/test_provider.py stays historical.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT.parents[1]  # current main repository; frozen protocol remains local below
sys.path.insert(0, str(OWNER))
import homeplane.file_steward  # noqa: E402 -- resolve the read-only owner package first

homeplane.file_steward.__path__.insert(0, str(ROOT / "protocol-snapshot/homeplane/file_steward"))
from homeplane.family_media.contracts import FamilyMediaOriginalRef  # noqa: E402
from homeplane.file_steward.contracts import StewardBudget, StewardJob  # noqa: E402
from homeplane.file_steward.pdf_text import StewardNativeTextReceipt  # noqa: E402

spec = importlib.util.spec_from_file_location("repair_consumer_provider", ROOT / "apple_document_provider.py")
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)
SAVED = json.loads((ROOT / "evidence/native-smoke-final.json").read_text())
RECORDED = [StewardNativeTextReceipt.model_validate(row["receipt"]) for row in SAVED["results"]]
DATA = b"PROTOCOL ONLY; THIS IS NOT A DOCUMENT OR NATIVE INPUT"
FAILURES = (
    "locked",
    "restricted",
    "malformed",
    "missing",
    "limit",
    "unsupported",
    "cancelled",
    "timeout",
    "failed",
)


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Consumer regressions must not launch subprocesses/native helpers")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


class Source:
    def __init__(self, deny_at=None):
        self.deny_at = deny_at
        self.checks = 0

    async def current(self, job):
        self.checks += 1
        return self.deny_at is None or self.checks < self.deny_at

    async def read_complete(self, job, budget):
        return DATA


class ReplayBridge:
    def __init__(self, receipt):
        self.receipt = receipt
        self.calls = []

    async def extract(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.receipt


def job(**updates):
    values = dict(
        job_id="a" * 64,
        item_id="b" * 64,
        content_id=hashlib.sha256(DATA).hexdigest(),
        source_fingerprint="c" * 64,
        original_ref=FamilyMediaOriginalRef(
            source_ref="protocol-only-source",
            object_ref="protocol-only-document",
            config_generation=1,
            connection_epoch="protocol-only-epoch",
            revision="protocol-only-revision",
        ),
        provider=provider.SPEC.key,
        provider_version=provider.SPEC.version,
        status="queued",
        updated_at=datetime.now(UTC),
    )
    return StewardJob(**(values | updates))


def receipt(method="pdfkit", status="text", *, text=None, page=1, page_count=None):
    """Constructed text exercises the protocol only; it is not an observed OCR receipt."""
    if text is None:
        text = "PROTOCOL ONLY" if status in {"text", "ocr_text", "low_quality"} else ""
    if page_count is None and method != "vision_image":
        page_count = 4
    return StewardNativeTextReceipt(
        page=page,
        page_count=page_count,
        status=status,
        text=text,
        method=method,
        engine_version="protocol_double_v1",
        reason="protocol_only_payload",
    )


def instance(value, *, image=False, mode="pdf_auto", source=None):
    bridge = ReplayBridge(value)
    cls = provider.AppleImageProvider if image else provider.ApplePDFProvider
    return cls(source=source or Source(), bridge=bridge, native_qualified=True, mode=mode)


def consume(value, *, item=None, budget=None, image=False, mode="pdf_auto", source=None):
    item = item or job()
    before = item.model_dump_json()
    adapter = instance(value, image=image, mode=mode, source=source)
    method = adapter.extract_image if image else adapter.step
    try:
        result = asyncio.run(method(item, budget or StewardBudget()))
    finally:
        assert item.model_dump_json() == before, "Consumer may never mutate a job/cursor"
    return result, adapter


def refused(value, code, *, outcome=False, **kwargs):
    with pytest.raises(provider.AppleProviderError) as caught:
        consume(value, **kwargs)
    error = caught.value
    assert error.code == code
    assert (error.outcome is not None) == outcome
    if error.receipt is not None:
        assert isinstance(error.receipt, StewardNativeTextReceipt)
        expected = value.model_dump() if isinstance(value, StewardNativeTextReceipt) else value
        assert error.receipt.model_dump() == expected, "Rejected evidence must stay intact"
    if error.outcome is not None:
        assert error.outcome.unit_id is None
        assert error.outcome.quality.character_count == 0
    return error


def test_saved_evidence_and_frozen_contract_are_only_replayed():
    assert len(RECORDED) == 6
    assert RECORDED[0].method == "pdfkit" and len(RECORDED[0].text) == 74
    assert (RECORDED[0].page, RECORDED[0].page_count) == (1, 4)
    assert all(row.status == "failed" and not row.text for row in RECORDED[1:])
    assert all(row.reason == "vision_perform_failed" for row in RECORDED[1:])
    assert all(len(row.languages_supported) == 33 for row in RECORDED[1:])
    assert str(ROOT / "protocol-snapshot") in sys.modules[StewardNativeTextReceipt.__module__].__file__


@pytest.mark.parametrize("as_dict", [False, True])
def test_saved_embedded_receipt_replays_with_truthful_provenance(as_dict):
    value = RECORDED[0].model_dump() if as_dict else RECORDED[0]
    result, adapter = consume(value, mode="pdf_text")
    assert len(result.units) == len(result.page_outcomes) == 1
    assert result.units[0].text == RECORDED[0].text
    assert (result.next_cursor, result.done, result.source_pages) == ("pdf-page-2", False, 4)
    outcome = result.page_outcomes[0]
    assert outcome.provenance.engine == "pdfkit"
    assert outcome.provenance.method == "embedded_text" and outcome.provenance.execution == "native"
    assert adapter.bridge.calls[0][1]["mode"] == "pdf_text"


def test_needs_ocr_is_only_an_embedded_page_observation():
    result, _ = consume(receipt(status="needs_ocr"), mode="pdf_text")
    assert not result.units
    assert result.page_outcomes[0].status == "needs_ocr"
    assert result.page_outcomes[0].reason == "no_embedded_text"
    assert result.page_outcomes[0].provenance.engine == "pdfkit"


@pytest.mark.parametrize("method", ["pdfkit", "vision_pdf"])
@pytest.mark.parametrize("status", FAILURES)
def test_known_pdf_failure_preserves_engine_without_advancing_cursor(method, status):
    error = refused(
        receipt(method, status, page=2),
        "steward_apple_" + status,
        outcome=True,
        item=job(completed_pages=1, completed_units=1, cursor="pdf-page-2", source_pages=4),
    )
    assert error.outcome.provenance.engine == ("pdfkit" if method == "pdfkit" else "vision")
    assert error.outcome.provenance.method == ("embedded_text" if method == "pdfkit" else "ocr")
    assert error.outcome.anchor.page == 2


@pytest.mark.parametrize("status", FAILURES)
def test_supervisor_failure_keeps_typed_receipt_without_fabricated_outcome(status):
    value = provider.AppleNativeBridge._supervisor_receipt(status, 2, "protocol_supervisor_failure")
    error = refused(value, "steward_apple_" + status, item=job(completed_pages=1, cursor="pdf-page-2"))
    assert error.receipt.method == "none"
    assert error.receipt.engine_version == "apple_native_v2:supervisor_v1"


@pytest.mark.parametrize(
    "status", ["failed", "timeout", "cancelled", "limit", "unsupported", "malformed", "missing"]
)
def test_image_supervisor_failure_retains_region_and_receipt_without_unit(status):
    value = provider.AppleNativeBridge._supervisor_receipt(status, 1, "protocol_supervisor_failure")
    result, _ = consume(value, image=True)
    assert result.receipt.model_dump() == value.model_dump()
    assert result.unit is None
    assert result.anchor.kind == "image_region" and result.anchor.page is None
    assert result.anchor.region == (0, 0, 1, 1)


@pytest.mark.parametrize("image", [False, True])
def test_low_quality_never_creates_a_committed_unit(image):
    value = receipt("vision_image" if image else "vision_pdf", "low_quality")
    if image:
        result, _ = consume(value, image=True)
        assert result.unit is None and result.receipt.text == "PROTOCOL ONLY"
    else:
        error = refused(value, "steward_apple_low_quality", outcome=True)
        assert error.outcome.status == "held" and error.receipt.text == "PROTOCOL ONLY"


@pytest.mark.parametrize("image", [False, True])
def test_one_character_budget_rejects_overlong_injected_text(image):
    value = receipt("vision_image", "ocr_text") if image else RECORDED[0]
    refused(value, "steward_apple_text_budget_exceeded", image=image, budget=StewardBudget(text_chars=1))


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("text,budget_chars", [("甲🙂x", 3), ("x" * 4000, 4000), ("x" * 4000, 32000)])
def test_unicode_scalar_and_four_thousand_character_boundaries(image, text, budget_chars):
    value = receipt("vision_image" if image else "pdfkit", "ocr_text" if image else "text", text=text)
    result, _ = consume(value, image=image, budget=StewardBudget(text_chars=budget_chars))
    assert (result.unit if image else result.units[0]).text == text


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize(
    "update",
    [
        {"text": "x" * 4001},
        {"page": 257},
        {"method": "invented"},
        {"status": "invented"},
        {"status": "failed", "text": "forbidden failure text"},
    ],
)
def test_mutated_typed_receipt_is_fully_revalidated(image, update):
    value = receipt("vision_image" if image else "pdfkit", "ocr_text" if image else "text")
    value = value.model_copy(update=update)
    error = refused(value, "steward_apple_invalid_native_receipt", image=image)
    assert error.receipt is None


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("value", [None, {}, {"unrecognized": True}, "not a receipt"])
def test_non_receipt_is_rejected_with_provider_error(image, value):
    refused(value, "steward_apple_invalid_native_receipt", image=image)


@pytest.mark.parametrize("status", ["failed", "ocr_text", "empty", "low_quality"])
def test_image_receipt_cannot_enter_pdf_success_or_failure_outcome(status):
    refused(receipt("vision_image", status), "steward_apple_pdf_provenance_changed")


@pytest.mark.parametrize("status", ["failed", "ocr_text", "empty", "low_quality"])
def test_embedded_only_mode_rejects_pdf_vision_before_success_or_failure_outcome(status):
    refused(receipt("vision_pdf", status), "steward_apple_pdf_provenance_changed", mode="pdf_text")


@pytest.mark.parametrize("status", ["ocr_text", "failed", "empty", "low_quality"])
def test_image_wrong_page_is_rejected_even_on_failure(status):
    refused(receipt("vision_image", status, page=2), "steward_apple_image_provenance_changed", image=True)


def test_image_wrong_page_supervisor_is_rejected():
    value = provider.AppleNativeBridge._supervisor_receipt("failed", 2, "protocol_wrong_page")
    refused(value, "steward_apple_image_provenance_changed", image=True)


@pytest.mark.parametrize(
    "method,status",
    [("pdfkit", "text"), ("pdfkit", "failed"), ("vision_pdf", "ocr_text"), ("vision_pdf", "failed")],
)
def test_pdf_receipt_cannot_enter_image_seam(method, status):
    refused(receipt(method, status), "steward_apple_image_provenance_changed", image=True)


@pytest.mark.parametrize("method", ["vision_image", "none"])
def test_image_page_count_is_rejected(method):
    refused(receipt(method, "failed", page_count=4), "steward_apple_image_provenance_changed", image=True)


@pytest.mark.parametrize(
    "method,status",
    [("pdfkit", "text"), ("vision_pdf", "failed"), ("vision_image", "failed"), ("none", "failed")],
)
def test_pdf_wrong_page_is_rejected_before_creating_outcome(method, status):
    refused(receipt(method, status, page=2), "steward_apple_page_changed")


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize("deny_at", [1, 2, 3])
def test_source_fence_still_precedes_receipt_consumption(image, deny_at):
    value = receipt("vision_image" if image else "pdfkit", "ocr_text" if image else "text")
    refused(value, "steward_apple_source_grant_denied", image=image, source=Source(deny_at))


@pytest.mark.parametrize("image", [False, True])
def test_changed_cursor_does_not_call_injected_bridge(image):
    item = job(cursor="pdf-page-2")
    adapter = instance(RECORDED[0], image=image)
    before = item.model_dump_json()
    method = adapter.extract_image if image else adapter.step
    expected = "steward_apple_image_resume_requires_owner" if image else "steward_apple_cursor_changed"
    with pytest.raises(provider.AppleProviderError) as caught:
        asyncio.run(method(item, StewardBudget()))
    assert caught.value.code == expected
    assert caught.value.receipt is caught.value.outcome is None
    assert not adapter.bridge.calls and item.model_dump_json() == before
