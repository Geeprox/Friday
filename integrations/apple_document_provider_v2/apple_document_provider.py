"""Finite Apple native providers. Host capabilities only; no NAS reader or queue mutation."""

import asyncio
import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from homeplane.family_media.catalog import digest
from homeplane.file_steward.contracts import (
    StewardExtractionProvenance,
    StewardExtractionQuality,
    StewardPageOutcome,
    StewardSourceAnchor,
    StewardUnit,
)
from homeplane.file_steward.pdf_text import (
    StewardNativeTextReceipt,
    StewardNativeTextRequest,
    StewardPDFPageReceipt,
)
from homeplane.file_steward.providers import ExtractionStep, ProviderSpec
from homeplane.file_steward.service import StewardError

SOURCE_LIMIT = 8_388_608
OUTPUT_LIMIT = 65_536
SPEC = ProviderSpec(
    "apple_document", "apple_pdfkit_vision_v2", "Apple 文档", "local", "host_scope_native_slot_required"
)


class AppleProviderError(StewardError):
    """The caller can inspect page failure evidence; no failed page is committed here."""

    def __init__(self, code, *, receipt=None, outcome=None):
        super().__init__(code)
        self.receipt = receipt
        self.outcome = outcome


class AppleNativeBridge:
    """Short-lived bytes-only helper. Development qualification defaults to denied.

    The host must admit a native slot and pin executable_sha256 from a reviewed build.
    This source does not grant live-source access or qualify an OS sandbox for production.
    Native code also watches peak RSS (384 MiB) and elapsed time; the parent bounds IO.
    """

    def __init__(self, *, executable=None, executable_sha256=None, native_qualified=False):
        self.executable = Path(executable) if executable is not None else None
        self.executable_sha256 = executable_sha256
        self.native_qualified = native_qualified
        self.active_processes = set()
        self.last_termination = None
        self.last_exit_code = None

    async def _read_output(self, stream):
        output = bytearray()
        while chunk := await stream.read(4096):
            output.extend(chunk)
            if len(output) > OUTPUT_LIMIT:
                raise AppleProviderError("steward_apple_output_limit")
        return bytes(output)

    @staticmethod
    def _supervisor_receipt(status, page, reason):
        return StewardNativeTextReceipt(
            page=page,
            status=status,
            method="none",
            engine_version="apple_native_v2:supervisor_v1",
            reason=reason,
            quality={"flags": [reason]},
        )

    async def native(self, request: StewardNativeTextRequest) -> StewardNativeTextReceipt:
        request = StewardNativeTextRequest.model_validate(request)
        if not self.native_qualified or self.executable is None or self.executable_sha256 is None:
            raise AppleProviderError("steward_apple_native_qualification_required")
        # This host-selected local executable is the only path read by this launcher.
        if self.executable.is_symlink() or not self.executable.is_file():
            raise AppleProviderError("steward_apple_executable_changed")
        if hashlib.sha256(self.executable.read_bytes()).hexdigest() != self.executable_sha256:
            raise AppleProviderError("steward_apple_executable_changed")
        try:
            source = base64.b64decode(request.document_base64, validate=True)
        except (binascii.Error, ValueError):
            return self._supervisor_receipt("malformed", request.page, "invalid_base64")
        if not 0 < len(source) <= SOURCE_LIMIT:
            return self._supervisor_receipt("limit", request.page, "source_byte_limit")
        payload = json.dumps(request.model_dump(), ensure_ascii=True, allow_nan=False).encode()
        del source
        process = None
        reader = None
        self.last_termination = None
        self.last_exit_code = None
        try:
            async with asyncio.timeout(request.seconds):
                process = await asyncio.create_subprocess_exec(
                    str(self.executable),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "TMPDIR": "/tmp"},
                )
                self.active_processes.add(process.pid)
                reader = asyncio.create_task(self._read_output(process.stdout))
                try:
                    for offset in range(0, len(payload), 65_536):
                        process.stdin.write(payload[offset : offset + 65_536])
                        await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
                output = await reader
                code = await process.wait()
                if code == 124:
                    self.last_termination = "native_timeout"
                    return self._supervisor_receipt("timeout", request.page, "native_timeout")
                if code == 125:
                    self.last_termination = "peak_rss_limit"
                    return self._supervisor_receipt("limit", request.page, "peak_rss_limit")
                if code != 0:
                    self.last_termination = "native_failed"
                    return self._supervisor_receipt("failed", request.page, "native_failed")
                try:
                    receipt = StewardNativeTextReceipt.model_validate_json(output)
                except ValueError:
                    raise AppleProviderError("steward_apple_invalid_native_receipt") from None
                if receipt.page != request.page or len(receipt.text) > request.max_chars:
                    raise AppleProviderError("steward_apple_invalid_native_receipt")
                return receipt
        except TimeoutError:
            self.last_termination = "host_timeout"
            return self._supervisor_receipt("timeout", request.page, "host_timeout")
        except asyncio.CancelledError:
            self.last_termination = "cancelled"
            raise
        finally:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.shield(process.wait())
                self.last_exit_code = process.returncode
                self.active_processes.discard(process.pid)
            if reader is not None and not reader.done():
                reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass

    async def extract(
        self,
        document,
        page,
        max_chars,
        *,
        mode="pdf_text",
        seconds=5,
        max_pages=256,
        languages=(),
        max_pixels=4_000_000,
    ):
        if not isinstance(document, bytes) or not 0 < len(document) <= SOURCE_LIMIT:
            raise AppleProviderError("steward_apple_source_budget_exceeded")
        return await self.native(
            StewardNativeTextRequest(
                document_base64=base64.b64encode(document).decode("ascii"),
                page=page,
                max_chars=max_chars,
                mode=mode,
                seconds=seconds,
                max_pages=max_pages,
                languages=list(languages),
                max_pixels=max_pixels,
            )
        )

    async def page(self, document: bytes, page: int, max_chars: int) -> StewardPDFPageReceipt:
        """Compatibility seam for main owner's embedded-only PDFTextProvider."""
        receipt = await self.extract(document, page, max_chars, mode="pdf_text")
        if receipt.status not in {
            "text",
            "needs_ocr",
            "locked",
            "restricted",
            "malformed",
            "missing",
            "limit",
            "unsupported",
        }:
            raise AppleProviderError("steward_apple_" + receipt.status, receipt=receipt)
        return StewardPDFPageReceipt(
            page=receipt.page,
            page_count=receipt.page_count,
            status=receipt.status,
            text=receipt.text,
            engine="pdfkit",
            engine_version=receipt.engine_version,
            execution="native",
            text_kind="embedded",
        )


class ApplePDFProvider:
    """Existing step(job,budget) shape; one page, v2 outcomes, fail closed on held pages."""

    spec = SPEC

    def __init__(self, *, source=None, bridge=None, native_qualified=False, mode="pdf_auto", languages=()):
        if mode not in {"pdf_text", "pdf_auto"}:
            raise ValueError("steward_apple_pdf_mode_required")
        self.source = source
        self.bridge = bridge
        self.native_qualified = native_qualified
        self.mode = mode
        self.languages = tuple(languages)

    async def _source(self, job, budget):
        if not self.native_qualified or self.source is None or self.bridge is None:
            raise AppleProviderError("steward_apple_native_qualification_required")
        if (job.provider, job.provider_version) != (self.spec.key, self.spec.version):
            raise AppleProviderError("steward_apple_provider_changed")
        if not await self.source.current(job):
            raise AppleProviderError("steward_apple_source_grant_denied")
        document = await self.source.read_complete(job, budget)
        if not isinstance(document, bytes) or not 0 < len(document) <= min(SOURCE_LIMIT, budget.source_bytes):
            raise AppleProviderError("steward_apple_source_budget_exceeded")
        if hashlib.sha256(document).hexdigest() != job.content_id:
            raise AppleProviderError("steward_stale_fingerprint")
        if not await self.source.current(job):
            raise AppleProviderError("steward_apple_source_grant_denied")
        return document

    @staticmethod
    def _checked_receipt(receipt, max_chars):
        try:
            value = receipt.model_dump() if isinstance(receipt, StewardNativeTextReceipt) else receipt
            receipt = StewardNativeTextReceipt.model_validate(value)
        except ValueError:
            raise AppleProviderError("steward_apple_invalid_native_receipt") from None
        if len(receipt.text) > max_chars:
            raise AppleProviderError("steward_apple_text_budget_exceeded", receipt=receipt)
        return receipt

    async def step(self, job, budget):
        async with asyncio.timeout(budget.seconds):
            started = asyncio.get_running_loop().time()
            document = await self._source(job, budget)
            page = job.completed_pages + 1
            if job.cursor != (None if page == 1 else f"pdf-page-{page}"):
                raise AppleProviderError("steward_apple_cursor_changed")
            receipt = await self.bridge.extract(
                document,
                page,
                min(4000, budget.text_chars),
                mode=self.mode,
                languages=self.languages,
                seconds=max(0.001, budget.seconds - (asyncio.get_running_loop().time() - started)),
            )
            if not await self.source.current(job):
                raise AppleProviderError("steward_apple_source_grant_denied")
            receipt = self._checked_receipt(receipt, min(4000, budget.text_chars))
            if receipt.page != page:
                raise AppleProviderError("steward_apple_page_changed", receipt=receipt)
            if receipt.method == "vision_image" or (
                self.mode == "pdf_text" and receipt.method == "vision_pdf"
            ):
                raise AppleProviderError("steward_apple_pdf_provenance_changed", receipt=receipt)
            if receipt.method == "none":
                # PageOutcome v2 cannot truthfully identify an unknown engine.
                # Keep the typed receipt; the owner can persist supervisor evidence.
                raise AppleProviderError("steward_apple_" + receipt.status, receipt=receipt)
            anchor = StewardSourceAnchor(
                content_id=job.content_id,
                original_ref=job.original_ref,
                kind="page",
                page=page,
            )
            is_ocr = receipt.method in {"vision_pdf", "vision_image"}
            provenance = StewardExtractionProvenance(
                engine="vision" if is_ocr else "pdfkit",
                engine_version=receipt.engine_version,
                execution="native",
                method="ocr" if is_ocr else "embedded_text",
                language_hints=receipt.languages_used,
            )
            if receipt.status not in {"text", "ocr_text", "empty", "needs_ocr"}:
                failure = (
                    receipt.status
                    if receipt.status
                    in {"locked", "restricted", "malformed", "missing", "limit", "unsupported", "cancelled"}
                    else "provider_failed"
                )
                held = receipt.status in {
                    "locked",
                    "restricted",
                    "limit",
                    "unsupported",
                    "cancelled",
                    "low_quality",
                }
                outcome = StewardPageOutcome(
                    outcome_version=2,
                    anchor=anchor,
                    status="held" if held else "failed",
                    reason="provider_hold" if held else "provider_failure",
                    provider=job.provider,
                    provider_version=job.provider_version,
                    provenance=provenance,
                    quality=StewardExtractionQuality(assessment="review_required", character_count=0),
                    failure_code=failure,
                )
                # Owner may persist explicit failure evidence separately; this never advances a cursor.
                raise AppleProviderError("steward_apple_" + receipt.status, receipt=receipt, outcome=outcome)
            if (
                receipt.page_count is None
                or not page <= receipt.page_count <= 256
                or receipt.method == "vision_image"
            ):
                raise AppleProviderError("steward_apple_pdf_page_count_required", receipt=receipt)
            if job.source_pages is not None and receipt.page_count != job.source_pages:
                raise AppleProviderError("steward_apple_page_changed", receipt=receipt)
            units = []
            if receipt.status in {"text", "ocr_text"}:
                units.append(
                    StewardUnit(
                        unit_id=digest([job.job_id, job.completed_units]),
                        ordinal=job.completed_units,
                        text=receipt.text,
                        anchor=anchor,
                        provider=job.provider,
                        provider_version=job.provider_version,
                    )
                )
            reason = {
                "text": "embedded_text",
                "ocr_text": "ocr_text",
                "empty": "ocr_no_text",
                "needs_ocr": "no_embedded_text",
            }[receipt.status]
            outcome = StewardPageOutcome(
                outcome_version=2,
                anchor=anchor,
                status="text" if units else receipt.status,
                reason=reason,
                provider=job.provider,
                provider_version=job.provider_version,
                unit_id=units[0].unit_id if units else None,
                provenance=provenance,
                quality=StewardExtractionQuality(
                    assessment="review_required" if is_ocr else "unassessed",
                    character_count=len(receipt.text),
                ),
            )
            done = page == receipt.page_count
            return ExtractionStep(
                units, None if done else f"pdf-page-{page + 1}", done, [outcome], receipt.page_count
            )


@dataclass(frozen=True)
class AppleImageObservation:
    """Separate image DTO. A whole-image source region, never a fabricated PDF page."""

    anchor: StewardSourceAnchor
    receipt: StewardNativeTextReceipt
    unit: StewardUnit | None


class AppleImageProvider(ApplePDFProvider):
    async def extract_image(self, job, budget) -> AppleImageObservation:
        async with asyncio.timeout(budget.seconds):
            started = asyncio.get_running_loop().time()
            document = await self._source(job, budget)
            if job.cursor is not None or job.completed_pages or job.completed_units:
                raise AppleProviderError("steward_apple_image_resume_requires_owner")
            receipt = await self.bridge.extract(
                document,
                1,
                min(4000, budget.text_chars),
                mode="image_ocr",
                languages=self.languages,
                seconds=max(0.001, budget.seconds - (asyncio.get_running_loop().time() - started)),
            )
            if not await self.source.current(job):
                raise AppleProviderError("steward_apple_source_grant_denied")
            receipt = self._checked_receipt(receipt, min(4000, budget.text_chars))
            if (
                receipt.page != 1
                or receipt.page_count is not None
                or (
                    receipt.method != "vision_image"
                    and not (
                        receipt.method == "none"
                        and receipt.status
                        in {"failed", "timeout", "cancelled", "limit", "unsupported", "malformed", "missing"}
                    )
                )
            ):
                raise AppleProviderError("steward_apple_image_provenance_changed", receipt=receipt)
            anchor = StewardSourceAnchor(
                content_id=job.content_id,
                original_ref=job.original_ref,
                kind="image_region",
                region=(0, 0, 1, 1),
            )
            unit = None
            if receipt.status == "ocr_text":
                unit = StewardUnit(
                    unit_id=digest([job.job_id, 0]),
                    ordinal=0,
                    text=receipt.text,
                    anchor=anchor,
                    provider=job.provider,
                    provider_version=job.provider_version,
                )
            return AppleImageObservation(anchor, receipt, unit)

    async def step(self, job, budget):
        # Main owner's PageOutcome v2 is PDF-page-only; adoption needs its distinct image seam.
        raise AppleProviderError("steward_apple_image_queue_not_adopted")
