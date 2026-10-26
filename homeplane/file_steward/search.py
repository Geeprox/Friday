"""Bounded derived index of already owned units; catalog remains original authority.

No reader, provider, OCR, model, scan, stat, source bytes, or new read capability.
Literal matching returns the matching unit's exact original/page/time citation.
"""
import asyncio
import secrets
import sqlite3
import time
import unicodedata

from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import FamilyMediaPage

from .contracts import StewardUnit
from .service import MAX_UNITS, StewardError


def normalized(text):
    return unicodedata.normalize("NFC", text).casefold()


class IndexedContentSearch:
    def __init__(self, steward):
        self.steward = steward
        self.connection = steward.connection
        self.monotonic = time.monotonic
        self.cursors = {}
        try:
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS steward_search_revision "
                    "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, indexed INTEGER NOT NULL, "
                    "provider_binding TEXT NOT NULL)"
                )
                self.connection.execute("INSERT OR IGNORE INTO steward_search_revision VALUES (1,0,-1,'')")
                self.connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS steward_search_units USING fts5("
                    "unit_id UNINDEXED, item_id UNINDEXED, content_id UNINDEXED, binding UNINDEXED, "
                    "object_ref UNINDEXED, revision UNINDEXED, text, tokenize='trigram')"
                )
                for table in ("jobs", "units", "pages"):
                    for operation in ("INSERT", "UPDATE", "DELETE"):
                        self.connection.execute(
                            f"CREATE TRIGGER IF NOT EXISTS steward_search_{table}_{operation.lower()} "
                            f"AFTER {operation} ON steward_{table} BEGIN "
                            "UPDATE steward_search_revision SET revision=revision+1 WHERE id=1; END"
                        )
        except sqlite3.OperationalError:
            raise StewardError("steward_local_search_unavailable") from None

    def revision(self):
        return self.connection.execute("SELECT revision FROM steward_search_revision WHERE id=1").fetchone()[0]

    def provider_binding(self):
        return digest([[key, spec.version, spec.environment, spec.qualification]
                       for key, spec in sorted(self.steward.providers.items())])

    def refresh_index(self):
        revision, indexed, providers = self.connection.execute(
            "SELECT revision,indexed,provider_binding FROM steward_search_revision WHERE id=1"
        ).fetchone()
        provider_stamp = self.provider_binding()
        if revision == indexed and providers == provider_stamp:
            return revision
        jobs = {job.job_id: job for job in self.steward.jobs()}
        saved = self.connection.execute("SELECT job_id,body FROM steward_units ORDER BY id LIMIT ?", (MAX_UNITS + 1,)).fetchall()
        if len(saved) > MAX_UNITS:
            raise StewardError("steward_capacity_review_required")
        rows = []
        try:
            for job_id, body in saved:
                job = jobs.get(job_id)
                unit = StewardUnit.model_validate_json(body)
                if not job or job.status in {"disabled", "stale", "offline"}:
                    continue
                spec = self.steward.providers.get(job.provider)
                if (
                    not spec or spec.version == "unqualified" or spec.version != job.provider_version
                    or unit.provider != job.provider or unit.provider_version != job.provider_version
                    or unit.anchor.content_id != job.content_id or unit.anchor.original_ref != job.original_ref
                ):
                    continue
                ref = unit.anchor.original_ref
                binding = digest([ref.source_ref, ref.config_generation, ref.connection_epoch])
                if job.item_id != digest([binding, ref.object_ref]):
                    continue
                rows.append((unit.unit_id, job.item_id, job.content_id, binding, ref.object_ref, ref.revision,
                             normalized(unit.title + " " + unit.text)))
        except ValueError:
            raise StewardError("steward_search_cache_invalid") from None
        with self.connection:
            self.connection.execute("DELETE FROM steward_search_units")
            self.connection.executemany("INSERT INTO steward_search_units VALUES (?,?,?,?,?,?,?)", rows)
            self.connection.execute("UPDATE steward_search_revision SET indexed=?,provider_binding=? WHERE id=1", (revision, provider_stamp))
        self.cursors.clear()
        return revision

    def matching_units(self, context, query, after, directories):
        binding = digest([context.source_ref, context.config_generation, context.connection_epoch])
        sql = (
            "SELECT s.item_id,s.unit_id FROM steward_search_units s JOIN family_media_items i ON i.item_id=s.item_id "
            "AND i.original_hash=s.content_id AND i.binding=s.binding "
            "AND json_extract(i.body,'$.object_ref')=s.object_ref "
            "AND json_extract(i.body,'$.revision')=s.revision WHERE s.binding=? AND s.item_id>?"
        )
        args = [binding, after]
        if directories is not None:
            if not directories:
                return {}
            sql += " AND i.scope IN (" + ",".join("?" for _ in directories) + ")"
            args.extend(directories)
        if query:
            # Quote all FTS syntax as a literal phrase, and retain exact substring
            # semantics including %, _, quotes and short Chinese/Unicode queries.
            if len(query) >= 3:
                sql += " AND steward_search_units MATCH ?"
                args.append('"' + query.replace('"', '""') + '"')
            sql += " AND s.text LIKE ? ESCAPE '\\'"
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql += " ORDER BY s.item_id,s.unit_id LIMIT ?"
        args.append(MAX_UNITS + 1)
        matches = {}
        try:
            rows = self.connection.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            raise StewardError("steward_local_search_unavailable") from None
        if len(rows) > MAX_UNITS:
            raise StewardError("steward_capacity_review_required")
        for item_id, unit_id in rows:
            matches.setdefault(item_id, set()).add(unit_id)
        return matches

    def discard_cursor(self, cursor):
        self.cursors.pop(cursor, None)

    async def page(self, request, *, directory_refs=None, read_scope=None):
        context = await self.steward._check(request.expected_context)
        query = normalized(request.query)
        if "\x00" in query:
            raise StewardError("steward_invalid_search_query")
        directories = sorted(set(directory_refs)) if directory_refs is not None else None
        revision = self.refresh_index()
        providers = self.provider_binding()
        policy = self.steward._policy_epoch()
        fingerprint = digest([request.expected_context, query, request.limit, directories, read_scope, revision, policy, providers])
        current_time = self.monotonic()
        self.cursors = {k: v for k, v in self.cursors.items() if v[0] > current_time}
        after = ""
        if request.cursor:
            value = self.cursors.get(request.cursor)
            if not value or value[1] != fingerprint:
                raise StewardError("steward_search_cursor_changed")
            after = value[2]
        matches = self.matching_units(context, query, after, directories)
        await asyncio.sleep(0)  # Cancellation boundary; index is fully committed, no background owner.
        rows = []
        ids = list(matches)
        projection_request = request.model_copy(update={"query": "", "cursor": None})
        for offset in range(0, len(ids), 32):
            items = await self.steward.catalog.cached_items(request.expected_context, ids[offset:offset + 32])
            items = [item for item in items if item.status == "ready" and item.original_ref is not None]
            page = FamilyMediaPage(context_fingerprint=request.expected_context, items=items, simulated=context.simulated)
            projected = await self.steward._project_page(
                projection_request, page, directory_refs=directories, exact_original=True, matching_unit_ids=matches
            )
            for row in projected["items"]:
                matching = matches[row["item"].item_id]
                # The ordinary projection intentionally caps citations. Search must
                # retrieve a late matching unit, rather than display unrelated first pages.
                references = [u for job in row["jobs"] for u in self.steward.units(job.job_id)
                              if u.unit_id in matching and u.anchor.original_ref == row["item"].original_ref]
                if references:
                    row.update(references=references[:8], matched_reference_count=len(references))
                    rows.append(row)
                if len(rows) > request.limit:
                    break
            if len(rows) > request.limit:
                break
        await self.steward._check(request.expected_context)
        if self.revision() != revision or self.steward._policy_epoch() != policy or self.provider_binding() != providers:
            raise StewardError("steward_search_changed")
        cursor = None
        if len(rows) > request.limit:
            if len(self.cursors) >= 64:
                raise StewardError("steward_search_cursor_capacity")
            cursor = secrets.token_hex(32)
            self.cursors[cursor] = (self.monotonic() + 60, fingerprint, rows[request.limit - 1]["item"].item_id)
        return {"context_fingerprint": request.expected_context, "items": rows[:request.limit],
                "next_cursor": cursor, "search_scope": "indexed_cache", "simulated": context.simulated,
                "coverage": {"basis": "current_bound_local_extracted_content", "source_online": context.source_online,
                             "unindexed_content_included": False, "inaccessible_originals_included": False,
                             "original_bytes_checked": False, "source_read_enabled": False,
                             "complete_source_scan_claimed": False}}
