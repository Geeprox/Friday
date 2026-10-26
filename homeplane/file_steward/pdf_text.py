"""PDFKit page adapter. No paths, transport, native process launcher or cloud fallback."""

import hashlib
from typing import Literal, Protocol

from pydantic import Field, model_validator

from homeplane.contracts import Contract
from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import OpaqueRef

from .contracts import StewardBudget, StewardJob, StewardPageOutcome, StewardSourceAnchor, StewardUnit
from .providers import PROVIDERS, ExtractionStep
from .service import StewardError


class StewardNativeTextRequest(Contract):
    """Native wire v2. A request processes one page/image, not max_pages pages."""

    request_version: Literal[2] = 2
    document_base64: str = Field(max_length=11184812)
    page: int = Field(ge=1, le=256)
    max_chars: int = Field(ge=1, le=4000)
    mode: Literal["pdf_text", "pdf_auto", "image_ocr"] = "pdf_text"
    max_pages: int = Field(default=256, ge=1, le=256)
    languages: list[OpaqueRef] = Field(default_factory=list, max_length=8)
    max_pixels: int = Field(default=4000000, ge=1, le=4000000)
    seconds: float = Field(default=5, gt=0, le=5)


class StewardNativeTextQuality(Contract):
    embedded_chars: int = Field(default=0, ge=0, le=1000000)
    meaningful_chars: int = Field(default=0, ge=0, le=4000)
    flags: list[OpaqueRef] = Field(default_factory=list, max_length=8)
    ocr_min_confidence: float | None = Field(default=None, ge=0, le=1)


class StewardNativeTextReceipt(Contract):
    """Provider owner's small native wire; distinct from embedded-only PDF wrapper."""

    receipt_version: Literal[2] = 2
    page: int = Field(ge=1, le=256)
    page_count: int | None = Field(default=None, ge=1, le=100000)
    status: Literal[
        "text",
        "needs_ocr",
        "ocr_text",
        "empty",
        "low_quality",
        "locked",
        "restricted",
        "malformed",
        "missing",
        "limit",
        "unsupported",
        "cancelled",
        "timeout",
        "failed",
    ]
    text: str = Field(default="", max_length=4000)
    method: Literal["pdfkit", "vision_pdf", "vision_image", "none"]
    engine_version: str = Field(min_length=1, max_length=120)
    quality: StewardNativeTextQuality = Field(default_factory=StewardNativeTextQuality)
    languages_used: list[OpaqueRef] = Field(default_factory=list, max_length=8)
    languages_supported: list[OpaqueRef] = Field(default_factory=list, max_length=64)
    reason: OpaqueRef

    @model_validator(mode="after")
    def native_evidence(self):
        if self.status in {"text", "ocr_text"} and not self.text.strip():
            raise ValueError("steward_native_text_required")
        if self.status not in {"text", "ocr_text", "low_quality"} and self.text:
            raise ValueError("steward_native_unexpected_text")
        if self.status == "text" and self.method != "pdfkit":
            raise ValueError("steward_native_embedded_method_required")
        if self.status in {"ocr_text", "low_quality"} and self.method not in {"vision_pdf", "vision_image"}:
            raise ValueError("steward_native_ocr_method_required")
        if self.method in {"pdfkit", "none"} and self.quality.ocr_min_confidence is not None:
            raise ValueError("steward_native_unexpected_ocr_confidence")
        if self.method != "vision_image" and self.status in {
            "text",
            "needs_ocr",
            "ocr_text",
            "empty",
            "low_quality",
        }:
            if self.page_count is None or self.page > self.page_count:
                raise ValueError("steward_native_page_count_required")
        return self


class StewardPDFPageReceipt(Contract):
    """One genuine embedded-text result from a qualified native bridge, or a fixture receipt."""

    page: int = Field(ge=1, le=256)
    page_count: int | None = Field(default=None, ge=1, le=100000)
    status: Literal[
        "text", "needs_ocr", "locked", "restricted", "malformed", "missing", "limit", "unsupported"
    ]
    text: str = Field(default="", max_length=4000)
    engine: Literal["pdfkit", "vision", "synthetic_fixture"]
    engine_version: str = Field(min_length=1, max_length=120)
    execution: Literal["native", "fixture"]
    text_kind: Literal["embedded", "ocr"] = "embedded"

    @model_validator(mode="after")
    def receipt_evidence(self):
        if self.status in {"text", "needs_ocr"} and (self.page_count is None or self.page > self.page_count):
            raise ValueError("steward_pdf_invalid_page_count")
        if (self.status == "text") != bool(self.text.strip()):
            raise ValueError("steward_pdf_invalid_page_receipt")
        if self.status != "text" and self.text:
            raise ValueError("steward_pdf_unexpected_text")
        if (self.execution == "fixture") != (self.engine == "synthetic_fixture"):
            raise ValueError("steward_pdf_invalid_provenance")
        return self


class ReadOnlyPDFCapability(Protocol):
    """Trusted owner checks grant/cancel/binding before and after every bounded range.

    A future owner composes this over the EXISTING reader. Return complete bytes only
    within budget; no read implementation is provided here. No path/secret in jobs.
    """

    async def current(self, job: StewardJob) -> bool: ...

    async def read_complete(self, job: StewardJob, budget: StewardBudget) -> bytes: ...


class QualifiedPDFPageBridge(Protocol):
    """Owner-supplied, sandboxed native slot; stdin bytes only, bounded output/time/RSS."""

    async def page(self, document: bytes, page: int, max_chars: int) -> StewardPDFPageReceipt: ...


class PDFTextProvider:
    spec = PROVIDERS["pdfkit_text"]

    def __init__(self, *, source=None, bridge=None, native_qualified=False):
        self.source = source
        self.bridge = bridge
        self.native_qualified = native_qualified

    async def step(self, job, budget):
        if not self.native_qualified or self.source is None or self.bridge is None:
            raise StewardError("steward_pdf_qualification_required")
        if (job.provider, job.provider_version) != (self.spec.key, self.spec.version):
            raise StewardError("steward_pdf_provider_changed")
        if not await self.source.current(job):
            raise StewardError("steward_pdf_source_grant_denied")
        # Deliberately finite pilot: complete small PDFs only; not streaming arbitrary files.
        document = await self.source.read_complete(job, budget)
        if not isinstance(document, bytes) or not 0 < len(document) <= budget.source_bytes:
            raise StewardError("steward_pdf_source_budget_exceeded")
        if hashlib.sha256(document).hexdigest() != job.content_id:
            raise StewardError("steward_stale_fingerprint")
        if not await self.source.current(job):
            raise StewardError("steward_pdf_source_grant_denied")
        page = job.completed_pages + 1
        if job.cursor not in {None if page == 1 else f"pdf-page-{page}"}:
            raise StewardError("steward_pdf_cursor_changed")
        result = StewardPDFPageReceipt.model_validate(
            await self.bridge.page(document, page, min(4000, budget.text_chars))
        )
        if not await self.source.current(job):
            raise StewardError("steward_pdf_source_grant_denied")
        if result.page != page:
            raise StewardError("steward_pdf_page_changed")
        expected_engine = "synthetic_fixture" if self.spec.environment == "fixture" else "pdfkit"
        if result.engine != expected_engine or result.text_kind != "embedded":
            raise StewardError("steward_pdf_provenance_changed")
        if result.status not in {"text", "needs_ocr"}:
            raise StewardError("steward_pdf_" + result.status)
        if result.page_count > 256:
            raise StewardError("steward_pdf_page_limit")
        if len(result.text) > budget.text_chars:
            raise StewardError("steward_pdf_text_budget_exceeded")
        anchor = StewardSourceAnchor(
            content_id=job.content_id, original_ref=job.original_ref, kind="page", page=page
        )
        units = []
        if result.status == "text":
            units.append(
                StewardUnit(
                    unit_id=digest([job.job_id, job.completed_units]),
                    ordinal=job.completed_units,
                    text=result.text,
                    anchor=anchor,
                    provider=job.provider,
                    provider_version=job.provider_version,
                )
            )
        outcome = StewardPageOutcome(
            anchor=anchor,
            status=result.status,
            provider=job.provider,
            provider_version=job.provider_version,
            unit_id=units[0].unit_id if units else None,
            reason="embedded_text" if units else "no_embedded_text",
        )
        done = page == result.page_count
        return ExtractionStep(
            units, None if done else f"pdf-page-{page + 1}", done, [outcome], result.page_count
        )
