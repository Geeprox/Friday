# Friday family media (F2)

This module catalogs family photos and videos over the F1 injected NAS reader. It
stores local metadata and review-only recipes. Existing photos and recordings
remain authoritative. No import starts a connection, enumerates directories,
reads originals, renders derivatives, invokes a model or operates a device.

## Run the synthetic integration

From a Friday checkout with its existing Python dependencies installed:

```sh
python -m homeplane.family_media
python -m pytest tests/test_homeplane_family_media.py -q
python -m homeplane.family_media.schemas --output schemas
```

The runnable example creates a temporary local SQLite database and an in-memory
protocol fixture containing explicitly simulated bytes. It demonstrates pending
video arrival, complete original SHA-256, exact duplicate photo grouping,
organization/crop review and a source-original byte range. Its bytes are test
payloads; decoding and photo/video appearance are unqualified.

## Consumer integration

The integration owner supplies the existing **local** SQLite connection and F1's
`homeplane.nas_connection.NASConnectionManager`. This module does not construct a
second transport, mount a share, load credentials or close that shared manager.
The provided router is opt-in and must be mounted under Friday's existing
authentication, origin and request guards. Protected main files are unchanged.

In the combined isolated integration, `create_app` now supplies that exact shared
manager/TraceStore connection and mounts this router under existing guards. No
source is enabled or scanned by default. F3 uses the trusted optional
`asset_evidence(request, source_ref=approved_source_ref)` filter, applied before
counts and history projection; ordinary owner-wide calls retain the original default.
See `docs/FRIDAY-READ-SOURCES-INTEGRATION.md`; the original repository remains unchanged.

```python
from homeplane.family_media import FamilyMediaCatalog
from homeplane.family_media.api import create_router
from homeplane.family_media.contracts import FamilyMediaQuery, FamilyMediaScanRequest

catalog = FamilyMediaCatalog(trace_store.connection, nas_manager, simulated=False)
app.include_router(create_router(catalog))  # integration owner, with existing guards

context = await catalog.context()  # cached F1 status; no list/media IO
result = await catalog.scan(
    FamilyMediaScanRequest(
        expected_context=context.fingerprint,
        directory_ref=approved_directory_ref,
        limit=128,
        byte_budget=4 * 1024 * 1024,
    )
)
page = await catalog.list_items(
    FamilyMediaQuery(
        expected_context=result.context.fingerprint,
        limit=50,
    )
)
```

The F1 seam is `status()` (synchronous cached getter; async fixtures also work),
`await list_page(source_ref, cursor, limit, directory_ref=...)`,
`await stat(source_ref, object_ref, expected_revision)` and
`await read_range(source_ref, object_ref, expected_revision, offset, length)`.
Only `online` plus reachable/authenticated/readable all exactly `True` admits IO.
Every result is fenced by source reference, configuration generation and
connection epoch. The private `reader.py` models validate that seam; they are
not a new NAS API or transport.

Public consumer models are in `contracts.py` and `schemas/FamilyMedia*.json`.

| Consumer call | Result / IO |
| --- | --- |
| `await context()` | Fingerprint, source admission, completion policy and latest current-binding checkpoint; no enumeration or media IO |
| `await scan(FamilyMediaScanRequest)` | One bounded listing page and optional bounded hash progress; durable enumeration checkpoint |
| `await list_items(FamilyMediaQuery)` | At most 100 local records; opaque cursor, optional kind or exact SHA-256 filter; no source enumeration or bytes |
| `await asset_evidence(FamilyMediaAssetRequest)` | Exact content identity and at most 128 original-location records, with total/ready/other counts; no bytes |
| `await resolve(FamilyMediaResolveRequest)` | Explicit current-binding stat; unavailable/expired binding remains visible |
| `await prepare_original(FamilyMediaResolveRequest)` | Ready original recheck and opaque same-origin 300-second plan |
| `await read_original(plan_id, offset, length)` | At most 1 MiB original bytes; per-plan budget reserved before IO |
| `revoke_original(plan_id)` | Cancels that original plan; active late results discarded |
| `await review_source(FamilyMediaSourceReview)` | Records or withdraws a reviewed completion convention for this exact source/configuration |
| `await preview_recipe(FamilyMediaRecipeRequest)` | Source/hash-bound awaiting-review recipe, with explicit operations; no rendered edit |
| `await review_recipe(FamilyMediaRecipeReview)` | Rechecks the source before recording reviewed; rejected records are retained |
| `recipes(item_id, limit=50)` | Bounded durable local recipe records; reviewed is historical review evidence, not proof of current applicability |
| `await close()` | Cancels/joins this catalog's active operations; shared F1 manager and SQLite connection remain owner-managed |

HTTP endpoints are under `/api/family-media`: `GET context`, `POST scan`,
`POST source-review`, `POST query`, `POST asset`, `POST resolve`,
`POST original-plans`, `GET originals/{plan_id}`, `DELETE original-plans/{plan_id}`,
`POST recipes/preview` and `POST recipes/review`. Their response schemas are
declared in OpenAPI. Media responses use octet-stream, `no-store` and `nosniff`;
large originals require a single bounded Range. A future format-qualified photo
viewer/player can consume this seam; this module does not claim native decoding.

## Identity, quality and checkpoint semantics

- `asset_id == original_hash` only after streaming the **entire** original through
  SHA-256. No pathname, filename, mtime, EXIF guess or near-duplicate inference is
  used as content identity. `captured_at` remains null. The supplied reader only
  provides media kind and metadata; no capture-time or codec probe is invented.
- `item_id` identifies an observed source/object binding. It expires when F1's
  connection epoch changes. Re-list and re-hash establish new original bindings
  under the same content hash. Original counts are observed bindings, including
  old bindings; `ready_original_count` is the current qualified subset. Consumers
  must not interpret historical aliases as extra physical copies.
- Ready requires nonempty source bytes, at least two seconds of unchanged
  observed metadata, completion evidence and a completed SHA-256. F1 `closed=None`
  always stays pending unless an operator has reviewed
  `closed_files_operator_reviewed` for that configured source. An explicitly open
  file is vetoed even under that policy. Stable size/mtime alone is insufficient.
- Upload/arrival or a revised original resets hash eligibility. The previous
  complete hash is retained in bounded local history and stays addressable as
  stale evidence. The per-item previous-hash summary shows eight most recent
  hashes; older hashes remain in the separate history table.
- Enumeration completion is **not an atomic snapshot or proof of absence**.
  Omitted known items become stale; only F1's explicit `nas_missing` stat result
  establishes missing. Outages remain unavailable. Reconnect/source changes
  require re-listing. Metadata older than 60 seconds or after clock rollback is
  stale; explicit stat can revalidate a same-binding original.
- The completed hash describes the bytes actually streamed. SMB/SFTP revision
  fencing is metadata-based; coarse SFTP timestamps and in-place changes that
  preserve metadata cannot prove an immutable version. Every item says
  `immutable_source_verified=False` and `source_revision_basis=metadata`.
- Enumeration checkpoints are committed with page observations only after all
  checks succeed. Cancellation/failure keeps the previous cursor and rejects late
  results. An expired or already-consumed F1 cursor requires explicit
  `restart_scan=True`; recovery never silently infers a completed scan.
- Up to four unfinished hashlib jobs live in bounded RAM, with only 1 MiB input
  chunks. Their state is discarded on catalog restart, cancellation or source
  change. Unfinished hashes restart from byte zero; durable scan checkpoints and
  completed hashes survive. No media bytes are stored in SQLite or disk cache.
- Scan budget is at most 128 entries, 8 MiB requested bytes and four hash jobs,
  under a default 12-second async deadline. `bytes_requested` includes failed/short
  read reservations; `bytes_read` counts successfully accepted complete chunks.
  Hashing and reader waits use this module's lock, never the Core control lock.
- Local capacity is 10,000 current/expired location records, 10,000 prior-hash
  evidence records and 1,000 recipes. Reaching capacity returns backpressure
  without deleting originals or dropping stored hash evidence. Obsolete local
  checkpoints and completion policies retain at most 64 each. Pagination has at
  most 64 cursors with 60-second lifetime.
- Original access has eight plans, two active readers, 1 MiB per range, 64 MiB per
  plan and monotonic 300-second expiry. Failure/cancellation never replenishes the
  budget. Source/revision/context change, revoke, close and late expiry discard
  bytes. Client buffering is ordinary bounded access, with no second full archive.

Camera+time archive identities and playback remain in existing `archive_*`
modules. The family catalog follows their ready/pending/missing/expired-source,
opaque-reference, range-budget, cancellation and no-copy semantics and reuses
their single HTTP range parser. It does not change camera workers, contracts or
timestamp mapping. Query evidence retains unknown causal relation.

## Recipe boundary

An organize operation creates a **virtual album/label recipe**; it does not move
or rename the authoritative file. Crop, quarter-turn rotation and bounded
exposure changes are photo recipes only. Crop coordinates are normalized against
the original after EXIF orientation, independent of operation list order; the
future renderer must verify that orientation before rendering. Video supports
organization only until duration/keyframe/decoder evidence is qualified. Unknown,
delete, overwrite, export, shell and trim operations are rejected.

Review checks the original hash and exact source reference again. A recipe is
never automatically rendered or applied. Original bytes and original hash remain
unchanged. A future renderer/exporter requires its own reviewed derivative scope;
that functionality is not provided here.

## Actual-data setup still required

1. Integrate the F1 owned patch and bind its credential resolver to the existing
   local credential store. Reuse the existing connection references; do not ask
   again for already-known credentials or migrate them through F2. This delivery
   used fake transports and synthetic credentials exclusively.
2. Bind this catalog to Friday's existing **Mac-local** SQLite store and shared
   F1 owner. Mount the opt-in router under existing guards and add Console/query
   consumers through the integration owner. F2 edits none of those protected files.
3. Resolve the exact previously approved family photo/video directory references,
   depth, file-count/time/byte budgets and source retention scope. No real directory
   has been listed or read for this delivery. Camera archive mapping stays separate.
4. Review the completion convention for that source: exclude active uploads and
   unfinished originals. F1 metadata cannot supply closure evidence. Until this
   review, actual F1 entries stay pending and no full-stream hashing occurs.
5. Run a separately scoped real read-only window on a few approved originals,
   including a photo, video, duplicate, partial arrival, absent original and
   reconnect. Record actual SHA-256, metadata revision limitations, latency and
   source noninterference. No live NAS/device/credential/media action is authorized
   by this synthetic result.
6. Qualify real image/video MIME, EXIF orientation and browser decoding before
   advertising a photo preview, crop appearance or video playback. Capture dates
   require a bounded reviewed metadata adapter; mtime must not become capture time.

New local-model work remains canceled. The blocked Friday security review was
not retried. Live hardware, physical controls, full MVP acceptance and publishing
are outside this F2 evidence.
