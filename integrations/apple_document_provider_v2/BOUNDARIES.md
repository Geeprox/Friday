# Source and adoption boundaries

## Observed existing interface

Read-only source: `/path/to/private-work/2026-10-02/task-12/Friday-file-steward`.

`ReadOnlyPDFCapability.current(job) -> bool`, `read_complete(job, budget) -> bytes`,
`QualifiedPDFPageBridge.page(document: bytes, page: int, max_chars: int) -> StewardPDFPageReceipt`,
`ExtractionProvider.step(job, budget) -> ExtractionStep` remain the consumption structure.
The main owner supplies/finalizes the contracts; this candidate does not edit them or own persistence.

Each call reads a complete small input through the host's bounded capability, verifies its content SHA-256,
and observes exactly one 1-based page. A successful committed page can advance the existing page cursor;
limits, timeout, cancellation, and errors must retain the previous committed cursor. No page is counted twice.
`done` follows observed source page count, not text presence. A page with no detected OCR text remains a
page observation, and an embedded text layer is assessed for extraction quality before selection.

## Hard candidate limits

- Decoded source <= 8,388,608 bytes; host budget may be smaller.
- PDF <= 256 pages; one requested page per subprocess/invocation.
- Text <= 4,000 Unicode scalar values per receipt; no silent truncation.
- Request wall budget <= 5 seconds, including helper startup, checked by the Python caller.
- Raster <= 4,000,000 pixels. PDF rendering additionally caps the edge at 2,048; image decoding is refused
  by pixel-count metadata checks and PDF XObject image dimensions are inspected before rendering.
- Output <= 65,536 bytes; a 20 ms native watchdog exits above 384 MiB peak RSS or after its deadline.
  This is sampled monitoring, not a kernel memory ceiling. Cancellation kills/reaps the helper and discards late text.
- OCR uses Vision's available language identifiers at runtime. Unsupported requested languages are reported;
  no substitute model/download/remote service is introduced.

These limits qualify finite synthetic execution. They do not establish trusted live source authorization.
The enclosing host must provide current scope/source binding checks, bounded ranges, cumulative budget
reservation, serial native slot admission, and protection for indexed sensitive content.

## Sensitivity and provenance

Only bytes enter the native helper through stdin. No source path, credential, directory name or subject
identity enters the helper's argv/environment. Results contain local native engine/revision evidence and a
page/source method; the Python owner attaches the existing content/original reference after revalidation.
The helper never edits or exports a PDF. Fixtures and logs contain only synthetic text. Production callers
must keep text out of generic job arguments, telemetry and cloud payloads and retain exact original-ref scope.

## Final verification boundary

Prepared fixtures cover embedded text, English scan, Chinese image/scan, a blank page, mixed text plus raster,
poor embedded text, >256 source pages and invalid input. The 26 passing tests verify PDFKit and finite
source-byte/pixel/text bounds, runtime unsupported-language handling, a declared process-control double's
kill/reap, source revocation and resume cursor. Actual Vision attempts failed both in this bridge and in
an independent public-API probe; successful OCR/Chinese accuracy/empty/low-confidence routes remain
unqualified. `evidence/qualification.json` is explicitly blocked. No fake OCR strings are supplied.
