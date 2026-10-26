"""Frozen interface validation only; no native provider/process, filesystem content or NAS."""

import pytest
from pydantic import ValidationError

from homeplane.file_steward.contracts import StewardPageOutcome, StewardSourceAnchor
from homeplane.file_steward.pdf_text import StewardNativeTextReceipt, StewardNativeTextRequest


def anchor():
    return StewardSourceAnchor(
        content_id="a" * 64,
        original_ref={
            "source_ref": "fixture-source",
            "object_ref": "fixture-doc",
            "config_generation": 1,
            "connection_epoch": "fixture-epoch",
            "revision": "fixture-r1",
        },
        kind="page",
        page=1,
    )


def outcome(**updates):
    value = dict(
        outcome_version=2,
        anchor=anchor(),
        status="text",
        provider="fixture_ocr",
        provider_version="v2",
        unit_id="b" * 64,
        reason="ocr_text",
        provenance={
            "engine": "synthetic_fixture",
            "engine_version": "fixture_v1",
            "execution": "fixture",
            "method": "ocr",
        },
        quality={"assessment": "review_required", "character_count": 3, "ocr_min_confidence": 0.7},
    )
    value.update(updates)
    return StewardPageOutcome(**value)


def test_v1_stays_embedded_only_and_v2_has_truthful_ocr_quality():
    old = StewardPageOutcome(
        anchor=anchor(),
        status="needs_ocr",
        provider="fixture",
        provider_version="v1",
        reason="no_embedded_text",
    )
    assert old.outcome_version == 1
    assert outcome().provenance.execution == "fixture"
    with pytest.raises(ValidationError):
        outcome(outcome_version=1)
    with pytest.raises(ValidationError):
        outcome(
            provenance={"engine": "vision", "engine_version": "test", "execution": "fixture", "method": "ocr"}
        )


@pytest.mark.parametrize(
    "status,reason,code",
    [
        ("empty", "ocr_no_text", None),
        ("failed", "provider_failure", "provider_failed"),
        ("held", "provider_hold", "cancelled"),
    ],
)
def test_v2_nontext_outcomes_cannot_claim_text_units(status, reason, code):
    result = outcome(
        status=status, reason=reason, unit_id=None, failure_code=code, quality={"character_count": 0}
    )
    assert result.unit_id is None
    with pytest.raises(ValidationError):
        outcome(status=status, reason=reason, failure_code=code)


def test_request_is_one_page_with_total_file_cap_and_finite_budget():
    request = StewardNativeTextRequest(document_base64="RklYVFVSRSBP TkxZ", page=1, max_chars=4000)
    assert request.mode == "pdf_text" and request.max_pages == 256 and request.seconds == 5
    for change in (
        {"seconds": 5.1},
        {"max_pages": 257},
        {"max_chars": 4001},
        {"max_pixels": 4000001},
        {"languages": ["en"] * 9},
    ):
        with pytest.raises(ValidationError):
            StewardNativeTextRequest.model_validate({**request.model_dump(), **change})


def native(**updates):
    fields = dict(
        page=1,
        page_count=2,
        status="text",
        text="fixture receipt text",
        method="pdfkit",
        engine_version="fixture_contract_only",
        quality={"embedded_chars": 20, "meaningful_chars": 18},
        reason="embedded_text",
    )
    fields.update(updates)
    return StewardNativeTextReceipt(**fields)


def test_native_receipt_never_relabels_ocr_as_embedded_or_fakes_failure_text():
    assert native().receipt_version == 2
    assert native(status="ocr_text", method="vision_pdf").method == "vision_pdf"
    assert native(status="low_quality", method="vision_pdf").status == "low_quality"
    for changes in (
        {"method": "vision_pdf"},
        {"status": "ocr_text"},
        {"status": "failed"},
        {"status": "empty"},
        {"page_count": None},
    ):
        with pytest.raises(ValidationError):
            native(**changes)
    assert native(status="timeout", text="", method="none", page_count=None).page_count is None


def test_supported_languages_keep_full_33_entry_runtime_shape_without_truncation():
    languages = [f"fixture-language-{index}" for index in range(33)]
    assert native(languages_supported=languages).languages_supported == languages
    assert len(native(languages_supported=["en"] * 64).languages_supported) == 64
    with pytest.raises(ValidationError):
        native(languages_supported=["en"] * 65)
