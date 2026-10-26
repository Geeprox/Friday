"""Local, incremental metadata catalog over F1's injected bounded reader.

Original identity is a complete streamed SHA-256, never a pathname, mtime or
perceptual guess. Hash state is bounded RAM only: process restart safely restarts
unfinished hashes at zero. Persistent enumeration checkpoints never claim an
atomic SMB/SFTP snapshot. There is no original write, delete, export or model IO.
"""

import asyncio
import hashlib
import inspect
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import (
    FamilyMediaAssetEvidence,
    FamilyMediaAssetRequest,
    FamilyMediaCheckpoint,
    FamilyMediaContext,
    FamilyMediaItem,
    FamilyMediaOriginalPlan,
    FamilyMediaOriginalRef,
    FamilyMediaPage,
    FamilyMediaQuery,
    FamilyMediaRecipe,
    FamilyMediaRecipeRequest,
    FamilyMediaRecipeReview,
    FamilyMediaResolution,
    FamilyMediaResolveRequest,
    FamilyMediaScanRequest,
    FamilyMediaScanResult,
    FamilyMediaSourceReview,
)
from .reader import (
    RANGE_BYTES,
    BoundedNASReader,
    ReaderEntry,
    ReaderPage,
    ReaderStatus,
    error_code,
    wire,
)

MAX_ITEMS = 10000
MAX_RECIPES = 1000
MAX_HISTORY = 10000
MAX_HASH_JOBS = 4
FRESH_SECONDS = 60
STABLE_SECONDS = 2
SESSION_BYTES = 67108864


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FamilyMediaError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class HashJob:
    revision: str
    size: int
    hasher: Any
    offset: int = 0


@dataclass
class OriginalLease:
    plan: FamilyMediaOriginalPlan
    reference: FamilyMediaOriginalRef
    deadline: float
    remaining: int = SESSION_BYTES


class FamilyMediaCatalog:
    def __init__(
        self,
        connection: sqlite3.Connection,
        reader: BoundedNASReader,
        *,
        simulated=False,
        clock=None,
        monotonic=None,
        io_timeout=12,
        indexed_kinds=("image", "video"),
    ):
        self.connection, self.reader = connection, reader
        self.simulated = bool(simulated)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.io_timeout = io_timeout
        if not indexed_kinds or not set(indexed_kinds) <= {"image", "video", "audio", "other"}:
            raise ValueError("family_media_invalid_indexed_kinds")
        self.indexed_kinds = frozenset(indexed_kinds)
        self.boot_id = secrets.token_hex(32)
        self.lock, self.closed = asyncio.Lock(), False
        self.hash_jobs, self.cursors, self.leases = {}, {}, {}
        self.active_readers = 0
        self.tasks = set()
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS family_media_meta (key TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS family_media_items ("
                "item_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL, binding TEXT NOT NULL, "
                "scope TEXT NOT NULL, original_hash TEXT, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS family_media_asset_idx ON family_media_items(original_hash)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS family_media_recipes (recipe_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS family_media_history ("
                "asset_id TEXT NOT NULL, item_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(asset_id,item_id))"
            )
        self.revision = self._meta("revision", 0)
        # Unfinished hashlib objects cannot be serialized safely; never reuse a
        # stored byte offset as proof that those bytes were hashed in this boot.

    def _meta(self, key, default=None):
        result = self.connection.execute("SELECT body FROM family_media_meta WHERE key=?", (key,)).fetchone()
        return json.loads(result[0]) if result else default

    def _set_meta(self, key, value):
        self.connection.execute(
            "INSERT OR REPLACE INTO family_media_meta VALUES (?,?)", (key, json.dumps(value))
        )
        for prefix in ("checkpoint:", "policy:"):
            if key.startswith(prefix):
                # Bound obsolete local checkpoints/policies across reconnects;
                # original files and original-hash evidence are never pruned.
                self.connection.execute(
                    "DELETE FROM family_media_meta WHERE key LIKE ? AND rowid NOT IN "
                    "(SELECT rowid FROM family_media_meta WHERE key LIKE ? ORDER BY rowid DESC LIMIT 64)",
                    (prefix + "%", prefix + "%"),
                )

    def _bump(self):
        self.revision += 1
        self._set_meta("revision", self.revision)
        self.cursors.clear()

    async def _status(self):
        if self.closed:
            raise FamilyMediaError("family_media_closed")
        try:
            async with asyncio.timeout(min(self.io_timeout, 3)):
                value = self.reader.status()
                if inspect.isawaitable(value):
                    value = await value
                return ReaderStatus.model_validate(wire(value))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not echo transport text, paths, hostnames or authentication data.
            raise FamilyMediaError("family_media_status_unavailable") from None

    def _policy(self, status):
        key = "policy:" + digest([status.source_ref, status.config_generation])
        return self._meta(key, "unreviewed")

    def _context(self, status):
        binding = digest(status.binding)
        saved = self._meta("last_checkpoint")
        checkpoint = (
            FamilyMediaCheckpoint.model_validate(saved)
            if saved and saved["binding_fingerprint"] == binding
            else None
        )
        failure = self._meta("failure")
        failure = failure["code"] if failure and failure["binding"] == binding else None
        return FamilyMediaContext(
            fingerprint=digest(
                [self.boot_id, self.revision, status.model_dump(), self._policy(status), failure]
            ),
            source_ref=status.source_ref,
            config_generation=status.config_generation,
            connection_epoch=status.connection_epoch,
            source_online=status.online,
            failure=failure or (None if status.online else "nas_unavailable"),
            completion_convention=self._policy(status),
            checkpoint=checkpoint,
            simulated=self.simulated,
        )

    async def context(self):
        return self._context(await self._status())

    async def _check(self, expected, *, online=False):
        status = await self._status()
        context = self._context(status)
        if expected != context.fingerprint:
            raise FamilyMediaError("family_media_context_changed")
        if online and not status.online:
            raise FamilyMediaError("family_media_source_unavailable")
        return status, context

    async def _fence(self, status):
        current = await self._status()
        if not current.online or current.binding != status.binding:
            self.hash_jobs.clear()
            raise FamilyMediaError("family_media_source_changed")

    def _save(self, row):
        old = self.connection.execute(
            "SELECT body FROM family_media_items WHERE item_id=?", (row["item_id"],)
        ).fetchone()
        previous = json.loads(old[0]) if old else None
        if previous and previous["original_hash"] and previous["original_hash"] != row["original_hash"]:
            present = self.connection.execute(
                "SELECT 1 FROM family_media_history WHERE asset_id=? AND item_id=?",
                (previous["original_hash"], row["item_id"]),
            ).fetchone()
            if (
                not present
                and self.connection.execute("SELECT COUNT(*) FROM family_media_history").fetchone()[0]
                >= MAX_HISTORY
            ):
                raise FamilyMediaError("family_media_history_capacity")
            previous["status"], previous["reason"] = "stale", "original_revision_superseded"
            self.connection.execute(
                "INSERT OR REPLACE INTO family_media_history VALUES (?,?,?)",
                (previous["original_hash"], row["item_id"], json.dumps(previous)),
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO family_media_items VALUES (?,?,?,?,?,?)",
            (
                row["item_id"],
                row["source_ref"],
                row["binding"],
                row["scope"],
                row["original_hash"],
                json.dumps(row),
            ),
        )

    def _load(self, item_id):
        result = self.connection.execute(
            "SELECT body FROM family_media_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if not result:
            raise FamilyMediaError("family_media_not_found")
        return json.loads(result[0])

    def _completion(self, row, status):
        if row["closed"] is True:
            return "source_closed"
        if row["closed"] is not False and self._policy(status) == "closed_files_operator_reviewed":
            return "operator_reviewed_stable_metadata"
        return "unreviewed"

    def _stable(self, row):
        age = (self.clock() - datetime.fromisoformat(row["stable_since"])).total_seconds()
        return age >= STABLE_SECONDS

    def _item(self, row, status):
        state, reason = row["status"], row["reason"]
        failure = self._meta("failure")
        binding = digest(status.binding)
        if row["binding"] != binding:
            state, reason = "stale", "source_binding_expired_relist_required"
        elif not status.online or (failure and failure["binding"] == binding):
            state, reason = "unavailable", "source_unavailable"
        elif (
            not 0
            <= (self.clock() - datetime.fromisoformat(row["last_seen_at"])).total_seconds()
            < FRESH_SECONDS
        ):
            state, reason = "stale", "metadata_freshness_expired"
        elif state == "ready" and self._completion(row, status) == "unreviewed":
            state, reason = "pending", "completion_unreviewed"
        elif state == "ready" and not self._stable(row):
            state, reason = "pending", "waiting_for_stable_closed_original"
        job = self.hash_jobs.get(row["item_id"])
        progress = row["size"] if row["original_hash"] else (job.offset if job else 0)
        reference = (
            FamilyMediaOriginalRef(
                source_ref=row["source_ref"],
                object_ref=row["object_ref"],
                config_generation=row["config_generation"],
                connection_epoch=row["connection_epoch"],
                revision=row["revision"],
            )
            if state == "ready"
            else None
        )
        return FamilyMediaItem(
            item_id=row["item_id"],
            asset_id=row["original_hash"],
            media_kind=row["media_kind"],
            size_bytes=row["size"],
            status=state,
            reason=reason,
            original_hash=row["original_hash"],
            previous_original_hashes=row["previous_hashes"],
            hash_basis="full_stream_sha256" if row["original_hash"] else "not_verified",
            hash_progress_bytes=progress,
            completion_evidence=self._completion(row, status),
            last_seen_at=row["last_seen_at"],
            original_ref=reference,
            simulated=self.simulated,
        )

    async def review_source(self, request: FamilyMediaSourceReview):
        status, _ = await self._check(request.expected_context, online=True)
        if self.lock.locked():
            raise FamilyMediaError("family_media_busy")
        with self.connection:
            self._set_meta(
                "policy:" + digest([status.source_ref, status.config_generation]),
                request.completion_convention,
            )
            self._bump()
        self.leases.clear()
        return self._context(status)

    def _observe(self, entry, status, scope, scan_id):
        binding = digest(status.binding)
        # This is an ephemeral original-location identity. Only SHA-256 is a
        # permanent content identity; re-list after owner reconnect/restart.
        item_id = digest([binding, entry.object_ref])
        existing = self.connection.execute(
            "SELECT body FROM family_media_items WHERE item_id=?", (item_id,)
        ).fetchone()
        previous = json.loads(existing[0]) if existing else None
        unchanged = bool(
            previous
            and previous["revision"] == entry.revision
            and previous["size"] == entry.size
            and previous["media_kind"] == entry.media_kind
            and previous["modified_ns"] == entry.modified_ns
        )
        now = self.clock().isoformat()
        history = previous["previous_hashes"] if previous else []
        if previous and (not unchanged or entry.closed is False) and previous["original_hash"]:
            history = list(dict.fromkeys([previous["original_hash"], *history]))[:8]
        if not unchanged or entry.closed is False:
            self.hash_jobs.pop(item_id, None)
        sha = previous["original_hash"] if unchanged and entry.closed is not False else None
        row = {
            "item_id": item_id,
            "source_ref": status.source_ref,
            "object_ref": entry.object_ref,
            "config_generation": status.config_generation,
            "connection_epoch": status.connection_epoch,
            "binding": binding,
            "scope": scope,
            "scan_id": scan_id,
            "revision": entry.revision,
            "size": entry.size,
            "modified_ns": entry.modified_ns,
            "media_kind": entry.media_kind,
            "closed": entry.closed,
            "stable_since": previous["stable_since"]
            if unchanged and previous["closed"] == entry.closed
            else now,
            "last_seen_at": now,
            "original_hash": sha,
            "previous_hashes": history,
            "status": "ready" if sha else "pending",
            "reason": "original_hash_verified_metadata_revision"
            if sha
            else "waiting_for_stable_closed_original",
        }
        if self._completion(row, status) == "unreviewed":
            row["status"], row["reason"] = "pending", "completion_unreviewed"
        if not row["size"]:
            row["status"], row["reason"] = "pending", "empty_or_partial_original"
        return row

    @staticmethod
    def _same_entry(row, entry):
        return (
            entry.object_ref == row["object_ref"]
            and entry.revision == row["revision"]
            and entry.size == row["size"]
            and entry.modified_ns == row["modified_ns"]
            and entry.kind == "file"
            and entry.media_kind == row["media_kind"]
            and entry.closed is not False
        )

    async def _stat(self, row, status):
        entry = ReaderEntry.model_validate(
            wire(
                await self.reader.stat(
                    status.source_ref,
                    row["object_ref"],
                    row["revision"],
                )
            )
        )
        await self._fence(status)
        if not self._same_entry(row, entry):
            raise FamilyMediaError("family_media_original_changed")
        return entry

    async def _hash(self, row, status, budget):
        if row["status"] == "missing" or row["original_hash"] or not row["size"]:
            return 0, 0, False
        if self._completion(row, status) == "unreviewed" or not self._stable(row) or row["closed"] is False:
            return 0, 0, False
        identifier, used, charged = row["item_id"], 0, 0
        job = self.hash_jobs.get(identifier)
        if job is None:
            if len(self.hash_jobs) >= MAX_HASH_JOBS:
                return 0, 0, False
            job = HashJob(row["revision"], row["size"], hashlib.sha256())
            self.hash_jobs[identifier] = job
        try:
            await self._stat(row, status)
            row["last_seen_at"] = self.clock().isoformat()
            while budget > 0 and job.offset < job.size:
                length = min(RANGE_BYTES, budget, job.size - job.offset)
                # Even a failed/short read consumes the requested-byte budget.
                charged += length
                budget -= length
                chunk = await self.reader.read_range(
                    status.source_ref, row["object_ref"], row["revision"], job.offset, length
                )
                await self._fence(status)
                if not isinstance(chunk, bytes) or len(chunk) != length:
                    raise FamilyMediaError("family_media_short_read")
                job.hasher.update(chunk)
                job.offset += length
                used += length
            if job.offset == job.size:
                await self._stat(row, status)
                row["original_hash"] = job.hasher.hexdigest()
                row["status"], row["reason"] = "ready", "original_hash_verified_metadata_revision"
                self.hash_jobs.pop(identifier, None)
                return used, charged, True
            row["status"], row["reason"] = "pending", "original_hash_in_progress"
            return used, charged, False
        except asyncio.CancelledError:
            self.hash_jobs.pop(identifier, None)
            raise
        except FamilyMediaError as error:
            self.hash_jobs.pop(identifier, None)
            if error.code == "family_media_source_changed":
                raise
            row["status"], row["reason"] = "pending", error.code
            return used, charged, False
        except Exception as error:
            code = error_code(error)
            self.hash_jobs.pop(identifier, None)
            if code == "nas_source_changed":
                raise FamilyMediaError("family_media_source_changed") from None
            row["status"] = "missing" if code == "nas_missing" else "unavailable"
            row["reason"] = code
            return used, charged, False

    async def _tracked(self, method, *args):
        task = asyncio.current_task()
        if self.closed:
            raise FamilyMediaError("family_media_closed")
        self.tasks.add(task)
        try:
            return await method(*args)
        finally:
            self.tasks.discard(task)

    async def scan(self, request: FamilyMediaScanRequest):
        return await self._tracked(self._scan, request)

    async def _scan(self, request: FamilyMediaScanRequest):
        if self.lock.locked():
            raise FamilyMediaError("family_media_busy")
        async with self.lock:
            status, _ = await self._check(request.expected_context, online=True)
            binding, scope = digest(status.binding), request.directory_ref or "root"
            key = "checkpoint:" + digest([binding, scope])
            old = self._meta(key)
            continuing = old and not old["enumeration_complete"] and not request.restart_scan
            scan_id = old["scan_id"] if continuing else secrets.token_hex(16)
            cursor = old["next_cursor"] if continuing else None
            try:
                async with asyncio.timeout(self.io_timeout):
                    page = ReaderPage.model_validate(
                        wire(
                            await self.reader.list_page(
                                status.source_ref,
                                cursor,
                                request.limit,
                                directory_ref=request.directory_ref,
                            )
                        )
                    )
                    await self._fence(status)
                    if [page.source_ref, page.config_generation, page.connection_epoch] != status.binding:
                        raise FamilyMediaError("family_media_source_changed")
                    if (
                        len(page.entries) > request.limit
                        or len({e.object_ref for e in page.entries}) != len(page.entries)
                        or (page.enumeration_complete and (page.next_cursor or not page.completed_scan_token))
                        or (
                            not page.enumeration_complete
                            and (not page.next_cursor or page.next_cursor == cursor)
                        )
                    ):
                        raise FamilyMediaError("family_media_invalid_reader_page")
                    rows = {
                        row["item_id"]: row
                        for row in (
                            self._observe(entry, status, scope, scan_id)
                            for entry in page.entries
                            if entry.kind == "file" and entry.media_kind in self.indexed_kinds
                        )
                    }
                    count = self.connection.execute("SELECT COUNT(*) FROM family_media_items").fetchone()[0]
                    new_count = sum(
                        not self.connection.execute(
                            "SELECT 1 FROM family_media_items WHERE item_id=?",
                            (identifier,),
                        ).fetchone()
                        for identifier in rows
                    )
                    if count + new_count > MAX_ITEMS:
                        raise FamilyMediaError("family_media_capacity")
                    # Continue bounded active hashes even when their listing page
                    # is behind the saved cursor. Do not create a second scan.
                    for identifier in list(self.hash_jobs):
                        candidate = rows.get(identifier) or self._load(identifier)
                        if candidate["binding"] == binding and candidate["scope"] == scope:
                            rows.setdefault(identifier, candidate)
                        else:
                            self.hash_jobs.pop(identifier, None)
                    candidates = self.connection.execute(
                        "SELECT body FROM family_media_items WHERE binding=? AND scope=? AND original_hash IS NULL "
                        "ORDER BY item_id LIMIT 128",
                        (binding, scope),
                    ).fetchall()
                    for result in candidates:
                        candidate = json.loads(result[0])
                        rows.setdefault(candidate["item_id"], candidate)
                    used, charged, completed, attempted = 0, 0, 0, 0
                    ordered = sorted(
                        rows.values(), key=lambda row: (row["item_id"] not in self.hash_jobs, row["item_id"])
                    )
                    for row in ordered:
                        if charged >= request.byte_budget or attempted >= MAX_HASH_JOBS:
                            break
                        if (
                            row["original_hash"]
                            or row["status"] == "missing"
                            or not row["size"]
                            or not self._stable(row)
                            or self._completion(row, status) == "unreviewed"
                            or row["closed"] is False
                        ):
                            continue
                        attempted += 1
                        amount, reserved, done = await self._hash(row, status, request.byte_budget - charged)
                        used += amount
                        charged += reserved
                        completed += int(done)
                    await self._fence(status)
                    checkpoint = FamilyMediaCheckpoint(
                        binding_fingerprint=binding,
                        directory_ref=request.directory_ref,
                        scan_id=scan_id,
                        next_cursor=page.next_cursor,
                        entries_seen=(old["entries_seen"] if continuing else 0) + len(page.entries),
                        enumeration_complete=page.enumeration_complete,
                        completed_scan_token=page.completed_scan_token,
                        checked_at=self.clock(),
                    )
                    with self.connection:
                        for row in rows.values():
                            self._save(row)
                        if page.enumeration_complete:
                            known = self.connection.execute(
                                "SELECT body FROM family_media_items WHERE binding=? AND scope=?",
                                (binding, scope),
                            ).fetchall()
                            for result in known:
                                row = json.loads(result[0])
                                if row["scan_id"] != scan_id and row["status"] != "missing":
                                    row["status"], row["reason"] = (
                                        "stale",
                                        "not_seen_in_non_atomic_enumeration",
                                    )
                                    self.hash_jobs.pop(row["item_id"], None)
                                    self._save(row)
                        self._set_meta(key, checkpoint.model_dump(mode="json"))
                        self._set_meta("last_checkpoint", checkpoint.model_dump(mode="json"))
                        self._set_meta("failure", None)
                        self._bump()
                    return FamilyMediaScanResult(
                        context=self._context(status),
                        files_observed=sum(
                            e.kind == "file" and e.media_kind in self.indexed_kinds for e in page.entries
                        ),
                        bytes_read=used,
                        bytes_requested=charged,
                        hashes_completed=completed,
                        checkpoint=checkpoint,
                        simulated=self.simulated,
                    )
            except asyncio.CancelledError:
                self.hash_jobs.clear()
                raise
            except Exception as error:
                self.hash_jobs.clear()
                code = error.code if isinstance(error, FamilyMediaError) else error_code(error)
                with self.connection:
                    self._set_meta("failure", {"binding": binding, "code": code})
                    self._bump()
                raise FamilyMediaError(code) from None

    async def list_items(self, request: FamilyMediaQuery):
        status, context = await self._check(request.expected_context)
        self.cursors = {key: value for key, value in self.cursors.items() if value[0] > self.monotonic()}
        after = ""
        filters = (request.media_kind, request.asset_id)
        if request.cursor:
            cursor = self.cursors.get(request.cursor)
            if not cursor or cursor[1] != context.fingerprint or cursor[2] != filters:
                raise FamilyMediaError("family_media_cursor_expired")
            after = cursor[3]
        sql, args = "SELECT body FROM family_media_items WHERE item_id>?", [after]
        if request.asset_id:
            sql += " AND original_hash=?"
            args.append(request.asset_id)
        if request.media_kind:
            sql += " AND json_extract(body,'$.media_kind')=?"
            args.append(request.media_kind)
        sql += " ORDER BY item_id LIMIT ?"
        args.append(request.limit + 1)
        rows = [json.loads(row[0]) for row in self.connection.execute(sql, args)]
        next_cursor = None
        if len(rows) > request.limit:
            if len(self.cursors) >= 64:
                raise FamilyMediaError("family_media_cursor_capacity")
            next_cursor = secrets.token_hex(32)
            self.cursors[next_cursor] = (
                self.monotonic() + 60,
                context.fingerprint,
                filters,
                rows[request.limit - 1]["item_id"],
            )
        return FamilyMediaPage(
            context_fingerprint=context.fingerprint,
            items=[self._item(row, status) for row in rows[: request.limit]],
            next_cursor=next_cursor,
            simulated=self.simulated,
        )

    async def cached_items(self, expected_context, item_ids):
        """Bounded consumer lookup without source stat/list/bytes. Catalog remains primary."""
        if not 1 <= len(item_ids) <= 32 or len(set(item_ids)) != len(item_ids):
            raise FamilyMediaError("family_media_invalid_selection")
        status, _ = await self._check(expected_context)
        return [self._item(self._load(item_id), status) for item_id in item_ids]

    async def cached_scopes(self, expected_context, item_ids):
        """Exact observed scope only. Opaque refs cannot prove a parent/child hierarchy."""
        await self.cached_items(expected_context, item_ids)
        return [{"directory_ref": None if self._load(key)["scope"] == "root" else self._load(key)["scope"],
                 "ancestry_complete": False} for key in item_ids]

    async def asset_evidence(self, request: FamilyMediaAssetRequest, *, source_ref=None):
        status, context = await self._check(request.expected_context)
        if source_ref is not None and source_ref != status.source_ref:
            raise FamilyMediaError("family_media_context_changed")
        # Original hashes remain addressable after replacement. A history row is
        # evidence only; it cannot expose a current source binding for old bytes.
        rows = self.connection.execute(
            "SELECT body FROM family_media_items WHERE original_hash=? AND (? IS NULL OR source_ref=?) "
            "UNION ALL SELECT h.body FROM family_media_history h WHERE h.asset_id=? "
            "AND (? IS NULL OR json_extract(h.body,'$.source_ref')=?) "
            "AND NOT EXISTS (SELECT 1 FROM family_media_items i WHERE i.item_id=h.item_id AND i.original_hash=h.asset_id)",
            (request.asset_id, source_ref, source_ref, request.asset_id, source_ref, source_ref),
        ).fetchall()
        if not rows:
            raise FamilyMediaError("family_media_asset_not_found")
        # At most MAX_ITEMS+MAX_HISTORY local metadata rows; return only 128.
        # Compute the aggregate over all known rows, not an arbitrary first page.
        items = [self._item(json.loads(row[0]), status) for row in rows]
        states = {item.status for item in items}
        aggregate = next(
            state for state in ("ready", "pending", "unavailable", "stale", "missing") if state in states
        )
        items.sort(key=lambda item: (item.status != "ready", item.item_id))
        return FamilyMediaAssetEvidence(
            context_fingerprint=context.fingerprint,
            asset_id=request.asset_id,
            original_hash=request.asset_id,
            status=aggregate,
            originals=items[:128],
            original_count=len(items),
            ready_original_count=sum(item.status == "ready" for item in items),
            other_original_count=sum(item.status != "ready" for item in items),
            partial=len(items) > 128,
            simulated=self.simulated,
        )

    async def resolve(self, request: FamilyMediaResolveRequest):
        return await self._tracked(self._resolve_guarded, request)

    async def _resolve_guarded(self, request):
        if self.lock.locked():
            raise FamilyMediaError("family_media_busy")
        async with self.lock:
            return await self._resolve(request)

    async def _resolve(self, request):
        status, context = await self._check(request.expected_context)
        row = self._load(request.item_id)
        item = self._item(row, status)
        # Stale but same-binding items can be revalidated by explicit stat;
        # unknown object refs on a new connection require re-listing first.
        can_check = status.online and row["binding"] == digest(status.binding)
        checked = False
        if can_check:
            try:
                async with asyncio.timeout(self.io_timeout):
                    await self._stat(row, status)
                    await self._check(request.expected_context, online=True)
                checked = True
                row["last_seen_at"] = self.clock().isoformat()
                if (
                    row["original_hash"]
                    and self._completion(row, status) != "unreviewed"
                    and self._stable(row)
                ):
                    row["status"], row["reason"] = "ready", "original_hash_verified_metadata_revision"
                else:
                    row["status"], row["reason"] = "pending", "original_hash_or_completion_pending"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = error.code if isinstance(error, FamilyMediaError) else error_code(error)
                if code in {
                    "family_media_source_changed",
                    "family_media_context_changed",
                    "nas_source_changed",
                }:
                    raise FamilyMediaError("family_media_source_changed") from None
                row["status"] = (
                    "missing"
                    if code == "nas_missing"
                    else (
                        "stale"
                        if code
                        in {"nas_revision_changed", "nas_object_unknown", "family_media_original_changed"}
                        else "unavailable"
                    )
                )
                row["reason"] = code
                checked = code in {"nas_missing", "nas_revision_changed", "family_media_original_changed"}
                self.hash_jobs.pop(row["item_id"], None)
            with self.connection:
                self._save(row)
                # A successful recheck of a ready original must not revoke other
                # active plans or its own recipe preview. Actual state changes do.
                refreshed = self._item(row, status)
                if (refreshed.status, refreshed.reason) != (item.status, item.reason):
                    self._bump()
            context = self._context(status)
            item = self._item(row, status)
        return FamilyMediaResolution(
            context_fingerprint=context.fingerprint,
            item=item,
            source_checked=checked,
            simulated=self.simulated,
        )

    def _prune_leases(self):
        self.leases = {key: lease for key, lease in self.leases.items() if lease.deadline > self.monotonic()}

    async def prepare_original(self, request: FamilyMediaResolveRequest):
        self._prune_leases()
        if len(self.leases) >= 8:
            raise FamilyMediaError("family_media_original_capacity")
        result = await self.resolve(request)
        if result.item.status != "ready":
            raise FamilyMediaError("family_media_original_not_ready")
        self._prune_leases()
        if len(self.leases) >= 8:
            raise FamilyMediaError("family_media_original_capacity")
        plan_id = secrets.token_hex(32)
        plan = FamilyMediaOriginalPlan(
            plan_id=plan_id,
            context_fingerprint=result.context_fingerprint,
            item_id=result.item.item_id,
            original_hash=result.item.original_hash,
            media_kind=result.item.media_kind,
            size_bytes=result.item.size_bytes,
            stream_url="/api/family-media/originals/" + plan_id,
            expires_at=self.clock() + timedelta(seconds=300),
            simulated=self.simulated,
        )
        self.leases[plan.plan_id] = OriginalLease(plan, result.item.original_ref, self.monotonic() + 300)
        return plan

    async def read_original(self, plan_id, offset, length):
        return await self._tracked(self._read_original, plan_id, offset, length)

    async def _read_original(self, plan_id, offset, length):
        self._prune_leases()
        lease = self.leases.get(plan_id)
        if lease is None:
            raise FamilyMediaError("family_media_original_expired")
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or not 0 < length <= RANGE_BYTES
            or offset + length > lease.plan.size_bytes
        ):
            raise FamilyMediaError("family_media_invalid_range")
        if self.active_readers >= 2:
            raise FamilyMediaError("family_media_original_busy")
        if length > lease.remaining:
            raise FamilyMediaError("family_media_original_byte_budget")
        status, _ = await self._check(lease.plan.context_fingerprint, online=True)
        if lease is not self.leases.get(plan_id) or self.active_readers >= 2:
            raise FamilyMediaError("family_media_original_expired_or_busy")
        if length > lease.remaining:
            raise FamilyMediaError("family_media_original_byte_budget")
        lease.remaining -= length  # Reserve before IO; errors never replenish it.
        self.active_readers += 1
        try:
            async with asyncio.timeout(self.io_timeout):
                ref = lease.reference
                chunk = await self.reader.read_range(
                    ref.source_ref, ref.object_ref, ref.revision, offset, length
                )
                await self._fence(status)
                await self._check(lease.plan.context_fingerprint, online=True)
                if lease.deadline <= self.monotonic() or lease is not self.leases.get(plan_id):
                    raise FamilyMediaError("family_media_original_expired")
                if not isinstance(chunk, bytes) or len(chunk) != length:
                    raise FamilyMediaError("family_media_short_read")
                return chunk
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = error.code if isinstance(error, FamilyMediaError) else error_code(error)
            raise FamilyMediaError(code) from None
        finally:
            self.active_readers -= 1

    def revoke_original(self, plan_id):
        self.leases.pop(plan_id, None)
        return {"status": "revoked", "media_copied": False}

    async def preview_recipe(self, request: FamilyMediaRecipeRequest):
        result = await self.resolve(
            FamilyMediaResolveRequest(
                expected_context=request.expected_context,
                item_id=request.item_id,
            )
        )
        item = result.item
        if item.status != "ready" or item.original_hash != request.expected_original_hash:
            raise FamilyMediaError("family_media_recipe_original_changed_or_not_ready")
        if item.media_kind != "image" and any(op.operation != "organize" for op in request.operations):
            raise FamilyMediaError("family_media_image_recipe_required")
        operations = [op.operation for op in request.operations]
        if len(set(operations)) != len(operations):
            raise FamilyMediaError("family_media_duplicate_recipe_operation")
        recipe = FamilyMediaRecipe(
            recipe_id=secrets.token_hex(32),
            context_fingerprint=result.context_fingerprint,
            item_id=item.item_id,
            original_hash=item.original_hash,
            original_ref=item.original_ref,
            operations=request.operations,
            status="awaiting_review",
            created_at=self.clock(),
            expires_at=self.clock() + timedelta(seconds=300),
            simulated=self.simulated,
        )
        with self.connection:
            if (
                self.connection.execute("SELECT COUNT(*) FROM family_media_recipes").fetchone()[0]
                >= MAX_RECIPES
            ):
                raise FamilyMediaError("family_media_recipe_capacity")
            self.connection.execute(
                "INSERT INTO family_media_recipes VALUES (?,?)",
                (recipe.recipe_id, recipe.model_dump_json()),
            )
        return recipe

    def recipes(self, item_id, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FamilyMediaError("family_media_invalid_request")
        rows = self.connection.execute(
            "SELECT body FROM family_media_recipes WHERE json_extract(body,'$.item_id')=? ORDER BY recipe_id LIMIT ?",
            (item_id, limit),
        )
        return [FamilyMediaRecipe.model_validate_json(row[0]) for row in rows]

    async def review_recipe(self, request: FamilyMediaRecipeReview):
        await self._check(request.expected_context, online=True)
        saved = self.connection.execute(
            "SELECT body FROM family_media_recipes WHERE recipe_id=?",
            (request.recipe_id,),
        ).fetchone()
        if not saved:
            raise FamilyMediaError("family_media_recipe_not_found")
        recipe = FamilyMediaRecipe.model_validate_json(saved[0])
        if recipe.status != "awaiting_review":
            if recipe.status != request.decision:
                raise FamilyMediaError("family_media_recipe_already_reviewed")
            return recipe
        if (
            not recipe.created_at <= self.clock() < recipe.expires_at
            or recipe.context_fingerprint != request.expected_context
        ):
            raise FamilyMediaError("family_media_recipe_expired_or_context_changed")
        if request.decision == "reviewed":
            result = await self.resolve(
                FamilyMediaResolveRequest(
                    expected_context=request.expected_context,
                    item_id=recipe.item_id,
                )
            )
            if (
                result.item.status != "ready"
                or result.item.original_hash != recipe.original_hash
                or result.item.original_ref != recipe.original_ref
            ):
                raise FamilyMediaError("family_media_recipe_original_changed_or_not_ready")
        recipe.status, recipe.reviewed_at = request.decision, self.clock()
        with self.connection:
            self.connection.execute(
                "UPDATE family_media_recipes SET body=? WHERE recipe_id=?",
                (recipe.model_dump_json(), recipe.recipe_id),
            )
        return recipe

    async def close(self):
        # The injected manager's connection belongs to F1, not to this consumer.
        self.closed = True
        self.hash_jobs.clear()
        self.cursors.clear()
        self.leases.clear()
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            # Cancellation is propagated into the owner, which reaps its worker.
            await asyncio.gather(*pending, return_exceptions=True)
