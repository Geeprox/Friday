"""Finite local orchestration over the primary catalog. No transport or filesystem executor."""

import asyncio
import inspect
import json
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import FamilyMediaAssetRequest, FamilyMediaQuery

from .contracts import (
    StewardBatchApproval,
    StewardBudget,
    StewardDestinationInventory,
    StewardJob,
    StewardJournalEntry,
    StewardPageOutcome,
    StewardPlanEntry,
    StewardRelocationPlan,
    StewardSuggestion,
    StewardUnit,
    relative_path,
)
from .policy import PRESETS, StewardPolicyRule, resolve_policy
from .providers import PROVIDERS
from .session import session_projection
from .settings_contracts import StewardSettingsRequest

MAX_JOBS, MAX_UNITS, MAX_PLANS, MAX_JOURNAL = 256, 4096, 64, 2048


class StewardError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fingerprint(item):
    return digest(
        [
            item.item_id,
            item.original_hash,
            item.size_bytes,
            item.original_ref.model_dump(mode="json") if item.original_ref else None,
        ]
    )


def path_key(value):
    # Conservative collision semantics across SMB/macOS: NFC + case folding.
    return unicodedata.normalize("NFC", value).casefold()


def relocation_effects(before_path, destination, before_root, target_root):
    before_folder, _, before_name = before_path.rpartition("/")
    after_folder, _, after_name = destination.rpartition("/")
    renamed = before_name != after_name
    moved = before_root != target_root or before_folder != after_folder
    operation = "rename_and_move" if renamed and moved else "rename" if renamed else "move" if moved else None
    return renamed, moved, operation


class FileSteward:
    def __init__(
        self,
        connection,
        catalog,
        *,
        clock=None,
        fixture_provider=None,
        fixture_paths=None,
        fixture_inventories=(),
        session_status=None,
    ):
        self.connection, self.catalog = connection, catalog
        self.session_status = session_status
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()
        self.fixture_provider = fixture_provider
        if fixture_paths is not None and not catalog.simulated:
            raise StewardError("steward_fixture_only")
        self.fixture_paths = {key: relative_path(path) for key, path in (fixture_paths or {}).items()}
        self.fixture_inventories = {}
        for inventory in fixture_inventories:
            self.register_fixture_inventory(inventory)
        if fixture_provider and (not catalog.simulated or fixture_provider.spec.environment != "fixture"):
            raise StewardError("steward_fixture_only")
        self.providers = dict(PROVIDERS)
        if fixture_provider:
            self.providers[fixture_provider.spec.key] = fixture_provider.spec
        with connection:
            for name in ("jobs", "plans", "journal", "states", "policies"):
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS steward_{name} (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS steward_units (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS steward_unit_job ON steward_units(job_id)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS steward_pages (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS steward_page_job ON steward_pages(job_id)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS steward_cursors (job_id TEXT NOT NULL, cursor TEXT NOT NULL, "
                "PRIMARY KEY(job_id,cursor))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS steward_policy_epoch "
                "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
            )
            connection.execute("INSERT OR IGNORE INTO steward_policy_epoch VALUES (1,0)")
        from .extraction_queue import StewardExtractionQueue
        from .settings import StewardSettings

        self.settings = StewardSettings(self)
        self.queue = StewardExtractionQueue(self)
        with connection:
            # Process interruption retains committed units/cursor and charged budget.
            for job in self.jobs():
                if (
                    (job.completed_units or job.cursor)
                    and job.status not in {"done", "needs_ocr"}
                    and not connection.execute(
                        "SELECT 1 FROM steward_cursors WHERE job_id=?", (job.job_id,)
                    ).fetchone()
                ):
                    self._save_job(
                        job.model_copy(update={"status": "stale", "reason": "cursor_history_unverified"})
                    )
                    continue
                if job.status == "running" and not self.queue.active_lease(job.job_id):
                    self._save_job(
                        job.model_copy(update={"status": "paused", "reason": "interrupted_resume_available"})
                    )
        self.queue.recover()

    def register_fixture_inventory(self, inventory):
        """Trusted synthetic setup only; no HTTP route accepts namespace evidence."""
        inventory = StewardDestinationInventory.model_validate(inventory)
        if not self.catalog.simulated or inventory.basis != "synthetic_fixture":
            raise StewardError("steward_fixture_only")
        self.fixture_inventories[inventory.root_ref] = inventory.model_copy(deep=True)

    def _trusted_inventory_fingerprint(self, root_ref):
        inventory = self.fixture_inventories.get(root_ref) if self.catalog.simulated else None
        return digest(inventory.model_dump(mode="json")) if inventory else None

    def _load(self, table, key, model):
        row = self.connection.execute(f"SELECT body FROM steward_{table} WHERE id=?", (key,)).fetchone()
        if not row:
            raise StewardError("steward_not_found")
        return model.model_validate_json(row[0])

    def _save(self, table, key, value, capacity):
        if not self.connection.execute(f"SELECT 1 FROM steward_{table} WHERE id=?", (key,)).fetchone():
            if self.connection.execute(f"SELECT COUNT(*) FROM steward_{table}").fetchone()[0] >= capacity:
                raise StewardError("steward_capacity_review_required")
        body = value.model_dump_json() if hasattr(value, "model_dump_json") else json.dumps(value)
        self.connection.execute(f"INSERT OR REPLACE INTO steward_{table} VALUES (?,?)", (key, body))
        if table == "policies":
            self.connection.execute("UPDATE steward_policy_epoch SET revision=revision+1 WHERE id=1")

    def _policy_epoch(self):
        return self.connection.execute("SELECT revision FROM steward_policy_epoch WHERE id=1").fetchone()[0]

    def _save_job(self, job):
        job.updated_at = self.clock()
        self._save("jobs", job.job_id, job, MAX_JOBS)

    def jobs(self):
        return [
            StewardJob.model_validate_json(r[0])
            for r in self.connection.execute("SELECT body FROM steward_jobs ORDER BY id LIMIT 256")
        ]

    def units(self, job_id):
        units = [
            StewardUnit.model_validate_json(r[0])
            for r in self.connection.execute(
                "SELECT body FROM steward_units WHERE job_id=? LIMIT 256", (job_id,)
            )
        ]
        return sorted(units, key=lambda unit: unit.ordinal)

    def page_outcomes(self, job_id):
        outcomes = [
            StewardPageOutcome.model_validate_json(row[0])
            for row in self.connection.execute(
                "SELECT body FROM steward_pages WHERE job_id=? LIMIT 256", (job_id,)
            )
        ]
        return sorted(outcomes, key=lambda outcome: outcome.anchor.page)

    async def _check(self, expected):
        context = await self.catalog.context()
        if context.fingerprint != expected:
            raise StewardError("steward_context_changed")
        return context

    async def _items(self, expected, item_ids, *, ready=False):
        await self._check(expected)
        items = await self.catalog.cached_items(expected, list(item_ids))
        if ready and any(
            item.status != "ready" or not item.original_ref or not item.original_hash for item in items
        ):
            raise StewardError("steward_source_recheck_required")
        return items

    async def context(self):
        context = await self.catalog.context()
        status = (
            self.session_status()
            if self.session_status
            else {"state": "online" if context.source_online else "offline"}
        )
        if inspect.isawaitable(status):
            status = await status
        return {
            "catalog": context,
            "session": session_projection(status, context),
            "executor_enabled": False,
            "providers": [
                {"key": p.key, "label": p.label, "enabled": False, "qualification": p.qualification}
                for p in PROVIDERS.values()
            ],
            "policy_presets": PRESETS,
            "policies": self.policies(),
            "queue": self.queue.public_records(),
            "limits": {
                "selected_files": 32,
                "jobs": MAX_JOBS,
                "units": MAX_UNITS,
                "concurrency": 1,
                "tick_seconds": 5,
                "tick_source_bytes": 8388608,
            },
        }

    def policies(self):
        return [
            StewardPolicyRule.model_validate_json(r[0])
            for r in self.connection.execute("SELECT body FROM steward_policies ORDER BY id LIMIT 256")
        ]

    async def set_policy(self, request):
        if self.lock.locked():
            raise StewardError("steward_busy")
        context = await self._check(request.expected_context)
        rule = request.rule
        if (rule.source_ref, rule.config_generation, rule.connection_epoch) != (
            context.source_ref,
            context.config_generation,
            context.connection_epoch,
        ):
            raise StewardError("steward_policy_binding_changed")
        with self.connection:
            self._save("policies", rule.rule_id, rule, 256)
        return rule

    async def policy_for(self, expected_context, item):
        context = await self._check(expected_context)
        scope = (await self.catalog.cached_scopes(expected_context, [item.item_id]))[0]
        return resolve_policy(
            self.policies(),
            source_ref=context.source_ref,
            config_generation=context.config_generation,
            connection_epoch=context.connection_epoch,
            item_id=item.item_id,
            ancestor_refs=[scope["directory_ref"]],
        )

    async def page(self, request, *, directory_refs=None, read_scope=None):
        if request.search_mode == "indexed_cache":
            if not hasattr(self, "search"):
                from .search import IndexedContentSearch

                self.search = IndexedContentSearch(self)
            return await self.search.page(request, directory_refs=directory_refs, read_scope=read_scope)
        page = await self.catalog.list_items(
            FamilyMediaQuery(
                expected_context=request.expected_context, cursor=request.cursor, limit=request.limit
            )
        )
        return await self._project_page(request, page, directory_refs=directory_refs)

    async def _project_page(self, request, page, *, directory_refs=None, exact_original=False, matching_unit_ids=None):
        jobs = self.jobs()
        job_units = {job.job_id: self.units(job.job_id) for job in jobs}
        job_pages = {job.job_id: self.page_outcomes(job.job_id) for job in jobs}
        rows = []
        for item in page.items:
            if directory_refs is not None:
                namespace = (await self.catalog.cached_scopes(request.expected_context, [item.item_id]))[0]
                if (namespace["directory_ref"] or "root") not in directory_refs:
                    continue
            matching = [job for job in jobs if job.content_id == item.original_hash]
            if directory_refs is not None or exact_original:
                # Project before title/search/status aggregation. Same hash is not authority
                # to reuse extraction from a different original location.
                projected = []
                for job in matching:
                    evidence = [*job_units[job.job_id], *job_pages[job.job_id]]
                    visible = [value for value in evidence if value.anchor.original_ref == item.original_ref]
                    if (
                        job.original_ref != item.original_ref
                        or (evidence and not visible)
                        or (not evidence and (job.completed_units or job.completed_pages))
                    ):
                        continue
                    if evidence and len(visible) != len(evidence) and job.status in {"done", "needs_ocr"}:
                        job = job.model_copy(update={"status": "paused", "reason": "scope_limited_reference"})
                    projected.append(job)
                matching = projected
            if matching_unit_ids is not None:
                eligible = matching_unit_ids.get(item.item_id, set())
                matching = [job for job in matching if any(
                    unit.unit_id in eligible and unit.anchor.original_ref == item.original_ref
                    for unit in job_units[job.job_id]
                )]
            priority = {
                "done": 0,
                "needs_ocr": 1,
                "running": 2,
                "paused": 3,
                "queued": 4,
                "failed": 5,
                "disabled": 6,
                "stale": 7,
                "offline": 8,
            }
            matching.sort(key=lambda job: (priority[job.status], job.provider, job.job_id))
            scoped_units = {
                job.job_id: [
                    u
                    for u in job_units[job.job_id]
                    if (directory_refs is None and not exact_original) or u.anchor.original_ref == item.original_ref
                    if matching_unit_ids is None or u.unit_id in matching_unit_ids.get(item.item_id, set())
                ]
                for job in matching
            }
            units = [u for job in matching for u in scoped_units[job.job_id]]
            title = next((u.title for u in units if u.title.strip()), "")
            if (
                request.query
                and request.query.casefold() not in (title + " " + " ".join(u.text for u in units)).casefold()
            ):
                continue
            policy = await self.policy_for(request.expected_context, item)
            rows.append(
                {
                    "item": item,
                    "title": title,
                    "jobs": matching,
                    "policy": policy,
                    "references": units[:8],
                    "reference_count": len(units),
                    "page_outcomes": [
                        outcome
                        for job in matching
                        for outcome in job_pages[job.job_id]
                        if (directory_refs is None and not exact_original) or outcome.anchor.original_ref == item.original_ref
                    ][:256],
                    "provider_states": [
                        {
                            "provider": job.provider,
                            "status": job.status,
                            "reference_count": len(scoped_units[job.job_id]),
                        }
                        for job in matching
                    ],
                    "extraction_status": "offline"
                    if item.status == "unavailable"
                    else "stale"
                    if item.status in {"stale", "missing"}
                    else matching[0].status
                    if matching
                    else "needs_extraction",
                }
            )
        return {
            "context_fingerprint": page.context_fingerprint,
            "items": rows,
            "next_cursor": page.next_cursor,
            "search_scope": "catalog_page",
            "simulated": page.simulated,
        }

    async def reconcile(self, request):
        if self.lock.locked():
            raise StewardError("steward_busy")
        # Reconcile one existing catalog page; never calls scan, list_page, stat, read_range.
        page = await self.catalog.list_items(
            FamilyMediaQuery(
                expected_context=request.expected_context, cursor=request.cursor, limit=request.limit
            )
        )
        changes = {"new": 0, "changed": 0, "unchanged": 0, "offline": 0, "stale": 0}
        with self.connection:
            for item in page.items:
                previous = self.connection.execute(
                    "SELECT body FROM steward_states WHERE id=?", (item.item_id,)
                ).fetchone()
                state = json.loads(previous[0]) if previous else None
                current = {
                    "fingerprint": fingerprint(item),
                    "content_id": item.original_hash,
                    "status": item.status,
                    "last_seen_at": item.last_seen_at.isoformat(),
                }
                outcome = (
                    "offline"
                    if item.status == "unavailable"
                    else "stale"
                    if item.status in {"stale", "missing"}
                    else "new"
                    if not state
                    else "changed"
                    if state["fingerprint"] != current["fingerprint"]
                    else "unchanged"
                )
                changes[outcome] += 1
                self._save("states", item.item_id, current, 10000)
                for job in self.jobs():
                    if job.item_id != item.item_id and job.content_id != item.original_hash:
                        continue
                    if item.status == "ready" and item.original_hash == job.content_id:
                        # Same complete content can resume at a revalidated new binding.
                        job.item_id, job.original_ref = item.item_id, item.original_ref
                        job.source_fingerprint = fingerprint(item)
                        if job.status in {"offline", "stale"}:
                            job.status = "done" if job.reason == "completed" else "paused"
                    elif job.item_id == item.item_id:
                        if job.status == "done":
                            job.reason = "completed"
                        job.status = "offline" if item.status == "unavailable" else "stale"
                    self._save_job(job)
        return {
            "changes": changes,
            "next_cursor": page.next_cursor,
            "source_scan_started": False,
            "records_removed": 0,
        }

    async def plan_jobs(self, request):
        if self.lock.locked():
            raise StewardError("steward_busy")
        spec = self.providers.get(request.provider)
        if not spec:
            raise StewardError("steward_provider_unknown")
        items = await self._items(request.expected_context, request.item_ids, ready=True)
        result = []
        with self.connection:
            for item in items:
                key = digest([item.original_hash, spec.key, spec.version])
                try:
                    job = self._load("jobs", key, StewardJob)
                except StewardError:
                    job = StewardJob(
                        job_id=key,
                        item_id=item.item_id,
                        content_id=item.original_hash,
                        source_fingerprint=fingerprint(item),
                        original_ref=item.original_ref,
                        provider=spec.key,
                        provider_version=spec.version,
                        status="disabled",
                        reason="provider_qualification_required",
                        updated_at=self.clock(),
                    )
                    self._save_job(job)
                result.append(job)
        result = list({job.job_id: job for job in result}.values())
        await self.queue.record_jobs(result, request.expected_context)
        return result

    def _step_failure(self, running, reason, *, paused=False):
        """A late worker cannot replace a committed cancel/recovery/revocation state."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self._load("jobs", running.job_id, StewardJob)
            if saved != running:
                return saved.reason
            saved.status, saved.reason = "paused" if paused else "failed", reason
            self._save_job(saved)
        return reason

    async def run_fixture_step(
        self, job_id, expected_context, budget=None, *, commit_guard=None, approval_view=None
    ):
        # Deliberately has no HTTP endpoint and cannot run a real adapter.
        adapter = self.fixture_provider
        if not adapter or not self.catalog.simulated or adapter.spec.environment != "fixture":
            raise StewardError("steward_executor_disabled")
        if self.lock.locked():
            raise StewardError("steward_busy")
        budget = budget or StewardBudget()
        async with self.lock:
            job = self._load("jobs", job_id, StewardJob)
            if job.provider != adapter.spec.key or job.provider_version != adapter.spec.version:
                raise StewardError("steward_executor_disabled")
            if job.status in {"done", "needs_ocr"}:
                return job
            if (job.completed_units or job.cursor) and not self.connection.execute(
                "SELECT 1 FROM steward_cursors WHERE job_id=?", (job.job_id,)
            ).fetchone():
                raise StewardError("steward_cursor_history_recheck_required")
            if job.status not in {"disabled", "queued", "paused", "failed"}:
                raise StewardError("steward_resume_recheck_required")
            item = (await self._items(expected_context, [job.item_id], ready=True))[0]
            if fingerprint(item) != job.source_fingerprint or item.original_hash != job.content_id:
                raise StewardError("steward_stale_fingerprint")
            approval = approval_view or await self.settings.query(
                StewardSettingsRequest(expected_context=expected_context, sample_item_id=job.item_id)
            )
            policy_epoch = self._policy_epoch()
            if job.charged_source_bytes + budget.source_bytes > 67108864 or job.attempts >= 64:
                raise StewardError("steward_job_budget_exhausted")
            job.status = "running"
            job.attempts += 1
            job.charged_source_bytes += (
                budget.source_bytes
            )  # reserve before call; failure/cancel does not refund
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                if commit_guard and not commit_guard():
                    raise StewardError("steward_queue_interrupted")
                if not self.settings.approval_current(approval, job.item_id):
                    raise StewardError("steward_settings_changed")
                if self._policy_epoch() != policy_epoch:
                    raise StewardError("steward_policy_changed")
                self._save_job(job)
            running = job.model_copy(deep=True)
            try:
                async with asyncio.timeout(budget.seconds):
                    result = await adapter.step(job.model_copy(deep=True), budget)
                current = (await self._items(expected_context, [job.item_id], ready=True))[0]
                if fingerprint(current) != job.source_fingerprint:
                    raise StewardError("steward_stale_fingerprint")
                units = [StewardUnit.model_validate(u) for u in result.units]
                outcomes = [StewardPageOutcome.model_validate(p) for p in result.page_outcomes]
                # Frozen v2 is an output contract, not qualification to index native/OCR results.
                if any(outcome.outcome_version != 1 for outcome in outcomes):
                    raise StewardError("steward_page_outcome_qualification_required")
                if (
                    len(units) > budget.units
                    or sum(len(u.text) + len(u.title) for u in units) > budget.text_chars
                    or job.completed_units + len(units) > 256
                    or (not result.done and (not result.next_cursor or result.next_cursor == job.cursor))
                    or (result.done and result.next_cursor is not None)
                ):
                    raise StewardError("steward_invalid_provider_result")
                if outcomes:
                    if (
                        len(outcomes) > budget.units
                        or result.source_pages is None
                        or not 1 <= result.source_pages <= 256
                        or (job.source_pages is not None and result.source_pages != job.source_pages)
                        or job.completed_pages + len(outcomes) > result.source_pages
                        or result.done != (job.completed_pages + len(outcomes) == result.source_pages)
                        or len(units) != sum(outcome.status == "text" for outcome in outcomes)
                    ):
                        raise StewardError("steward_invalid_page_progress")
                    by_id = {unit.unit_id: unit for unit in units}
                    for offset, outcome in enumerate(outcomes):
                        unit = by_id.get(outcome.unit_id)
                        if (
                            outcome.anchor.page != job.completed_pages + offset + 1
                            or outcome.anchor.content_id != job.content_id
                            or outcome.anchor.original_ref != job.original_ref
                            or outcome.provider != job.provider
                            or outcome.provider_version != job.provider_version
                            or (outcome.status == "text" and (not unit or unit.anchor != outcome.anchor))
                        ):
                            raise StewardError("steward_invalid_page_reference")
                    job.source_pages = result.source_pages
                elif result.source_pages is not None or job.source_pages is not None:
                    raise StewardError("steward_page_progress_required")
                for offset, unit in enumerate(units):
                    if (
                        unit.ordinal != job.completed_units + offset
                        or unit.anchor.content_id != job.content_id
                        or unit.anchor.original_ref != job.original_ref
                        or unit.provider != job.provider
                        or unit.provider_version != job.provider_version
                        or unit.unit_id != digest([job.job_id, unit.ordinal])
                    ):
                        raise StewardError("steward_invalid_provider_reference")
                job.cursor = result.next_cursor
                job.completed_units += len(units)
                job.completed_pages += len(outcomes)
                needs_ocr = any(
                    page.status == "needs_ocr" for page in [*self.page_outcomes(job.job_id), *outcomes]
                )
                job.status, job.reason = (
                    ("needs_ocr", "no_embedded_text")
                    if result.done and needs_ocr
                    else ("done", "completed")
                    if result.done
                    else ("paused", "resume_available")
                )
                # Validate cursor/limits and commit unit text + cursor atomically.
                job = StewardJob.model_validate(job.model_dump())
                # Advisory capacity read before taking the writer lock. Repeat it
                # in the transaction, where claim/configuration determine ordering.
                count = self.connection.execute("SELECT COUNT(*) FROM steward_units").fetchone()[0]
                pages_count = self.connection.execute("SELECT COUNT(*) FROM steward_pages").fetchone()[0]
                if count + len(units) > MAX_UNITS or pages_count + len(outcomes) > MAX_UNITS:
                    raise StewardError("steward_capacity_review_required")
                session = (await self.context())["session"]
                if session["reason"] == "authentication_required":
                    raise StewardError("authentication_required")
                if session.get("handoff_service"):
                    raise StewardError("content_grant_required")
                current = (await self._items(expected_context, [job.item_id], ready=True))[0]
                if fingerprint(current) != job.source_fingerprint:
                    raise StewardError("steward_stale_fingerprint")
                with self.connection:
                    self.connection.execute("BEGIN IMMEDIATE")
                    if commit_guard and not commit_guard():
                        raise StewardError("steward_queue_interrupted")
                    if self._load("jobs", job_id, StewardJob) != running:
                        raise StewardError("steward_queue_interrupted")
                    if not self.settings.approval_current(approval, job.item_id):
                        raise StewardError("steward_settings_changed")
                    if self._policy_epoch() != policy_epoch:
                        raise StewardError("steward_policy_changed")
                    if result.next_cursor:
                        if self.connection.execute(
                            "SELECT 1 FROM steward_cursors WHERE job_id=? AND cursor=?",
                            (job.job_id, result.next_cursor),
                        ).fetchone():
                            raise StewardError("steward_provider_cursor_revisited")
                        self.connection.execute(
                            "INSERT INTO steward_cursors VALUES (?,?)", (job.job_id, result.next_cursor)
                        )
                    count = self.connection.execute("SELECT COUNT(*) FROM steward_units").fetchone()[0]
                    pages_count = self.connection.execute("SELECT COUNT(*) FROM steward_pages").fetchone()[0]
                    if count + len(units) > MAX_UNITS or pages_count + len(outcomes) > MAX_UNITS:
                        raise StewardError("steward_capacity_review_required")
                    for unit in units:
                        self.connection.execute(
                            "INSERT INTO steward_units VALUES (?,?,?)",
                            (unit.unit_id, job.job_id, unit.model_dump_json()),
                        )
                    for outcome in outcomes:
                        self.connection.execute(
                            "INSERT INTO steward_pages VALUES (?,?,?)",
                            (
                                digest([job.job_id, "page", outcome.anchor.page]),
                                job.job_id,
                                outcome.model_dump_json(),
                            ),
                        )
                    self._save_job(job)
            except asyncio.CancelledError:
                self._step_failure(running, "interrupted_resume_available", paused=True)
                raise
            except Exception as error:
                reason = error.code if isinstance(error, StewardError) else "provider_step_failed"
                reason = self._step_failure(running, reason)
                raise StewardError(reason) from None
            return job

    async def suggest(self, request):
        item = (await self._items(request.expected_context, [request.item_id], ready=True))[0]
        policy = await self.policy_for(request.expected_context, item)
        units = [
            unit
            for job in self.jobs()
            if job.content_id == item.original_hash
            for unit in self.units(job.job_id)
        ]
        title = next((u.title.strip() for u in units if u.title.strip()), "")
        if not title:
            return StewardSuggestion(
                item_id=item.item_id,
                content_id=item.original_hash,
                title="",
                folder="",
                stem="",
                evidence_unit_ids=[],
                status="needs_extraction",
                policy=policy.model_dump(mode="json"),
            )
        text = title + " " + " ".join(u.text for u in units[:8])
        folder = (
            "知识/科学"
            if re.search(r"科学|物理|宇宙|science|physics", text, re.I)
            else "收藏/片段"
            if item.media_kind in {"video", "audio"}
            else "资料/文档"
        )
        safe = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', " ", unicodedata.normalize("NFC", title)).strip(" .")
        while len(safe.encode()) > 80:
            safe = safe[:-1]
        stem = (safe or "资料") + " · " + item.original_hash[:8]
        return StewardSuggestion(
            item_id=item.item_id,
            content_id=item.original_hash,
            title=title,
            folder=folder if policy.move_preview else "",
            stem=stem if policy.rename_preview else "",
            evidence_unit_ids=[u.unit_id for u in units[:8]],
            status="suggested" if policy.rename_preview or policy.move_preview else "display_title_only",
            policy=policy.model_dump(mode="json"),
        )

    async def compare(self, expected_context, left_id, right_id):
        left, right = await self._items(expected_context, [left_id, right_id], ready=True)
        policies = [await self.policy_for(expected_context, item) for item in (left, right)]
        review_allowed = all(policy.dedup_review and not policy.bundle_review_required for policy in policies)
        if left.original_hash == right.original_hash:
            evidence = await self.catalog.asset_evidence(
                FamilyMediaAssetRequest(expected_context=expected_context, asset_id=left.original_hash)
            )
            return {
                "relation": "exact_hash",
                "evidence": evidence,
                "delete_suggested": False,
                "review_allowed": review_allowed,
                "policies": policies,
            }
        snippets = []
        for item in (left, right):
            units = [
                u
                for job in self.jobs()
                if job.content_id == item.original_hash
                for u in self.units(job.job_id)
            ]
            snippets.append((units, " ".join(u.text for u in units[:8])[:4000]))
        score = (
            SequenceMatcher(None, snippets[0][1], snippets[1][1], autojunk=False).ratio()
            if all(s[1] for s in snippets)
            else None
        )
        return {
            "relation": "possible_version" if score is not None and score >= 0.85 else "different_or_unknown",
            "basis": "bounded_extracted_text" if score is not None else "no_comparable_extraction",
            "score": score,
            "evidence_unit_ids": [u.unit_id for s in snippets for u in s[0][:8]],
            "delete_suggested": False,
            "review_allowed": review_allowed,
            "policies": policies,
        }

    async def preview(self, request):
        if self.lock.locked():
            raise StewardError("steward_busy")
        ids = [s.item_id for s in request.selections]
        if len(set(ids)) != len(ids):
            raise StewardError("steward_duplicate_selection")
        items = await self._items(request.expected_context, ids, ready=True)
        server_inventory = (
            self.fixture_inventories.get(request.inventory.root_ref) if self.catalog.simulated else None
        )
        occupied = {
            path_key(p) for p in (server_inventory.paths if server_inventory else request.inventory.paths)
        }
        counts = {}
        for selection in request.selections:
            key = path_key(selection.destination)
            counts[key] = counts.get(key, 0) + 1
        age = (self.clock() - request.inventory.checked_at).total_seconds()
        inventory_fp = digest(request.inventory.model_dump(mode="json"))
        trusted_inventory = self._trusted_inventory_fingerprint(request.inventory.root_ref) == inventory_fp
        entries = []
        actual_renames, actual_moves = 0, 0
        for item, selection in zip(items, request.selections):
            if item.original_hash != selection.expected_content_id:
                raise StewardError("steward_stale_fingerprint")
            blockers = []
            policy = await self.policy_for(request.expected_context, item)
            before_path = self.fixture_paths.get(item.item_id)
            # A root change is a move even if both relative folders are identical.
            moved = item.original_ref.source_ref != request.inventory.root_ref
            renamed = False
            actual = None
            if not trusted_inventory:
                blockers.append("destination_namespace_unverified")
            if not before_path:
                blockers.append("source_namespace_diff_required")
            else:
                renamed, moved, actual = relocation_effects(
                    before_path,
                    selection.destination,
                    item.original_ref.source_ref,
                    request.inventory.root_ref,
                )
                if not actual or (selection.operation != actual and selection.operation != "restructure"):
                    blockers.append("declared_operation_mismatch")
                # The declared operation can never hide a change from the policy checks.
                if renamed and not policy.rename_preview:
                    blockers.append("policy_preserve_name")
                if moved and not policy.move_preview:
                    blockers.append("policy_preserve_location")
            actual_renames += int(renamed)
            actual_moves += int(moved)
            if moved and not policy.move_preview:
                blockers.append("policy_preserve_location")
            if policy.source == "policy_conflict":
                blockers.append("directory_policy_conflict")
            if policy.bundle_review_required:
                blockers.append("dependent_bundle_review_required")
            if selection.operation in {"rename", "rename_and_move"} and not policy.rename_preview:
                blockers.append("policy_preserve_name")
            if selection.operation in {"move", "rename_and_move", "restructure"} and not policy.move_preview:
                blockers.append("policy_preserve_location")
            if selection.operation == "restructure" and not policy.structure_preview:
                blockers.append("policy_preserve_tree")
            if (
                not request.inventory.complete
                or request.inventory.basis != "synthetic_fixture"
                or not self.catalog.simulated
            ):
                blockers.append("destination_recheck_required")
            if not 0 <= age < 60:
                blockers.append("destination_inventory_expired")
            key = path_key(selection.destination)
            if key in occupied or counts[key] > 1:
                blockers.append("destination_collision")
            entries.append(
                StewardPlanEntry(
                    entry_id=digest([item.item_id, inventory_fp, selection.destination]),
                    item_id=item.item_id,
                    content_id=item.original_hash,
                    source_fingerprint=fingerprint(item),
                    original_ref=item.original_ref,
                    before_relative_path=before_path,
                    destination=selection.destination,
                    operation=actual or selection.operation,
                    policy_fingerprint=policy.fingerprint,
                    policy_rule_ids=policy.matched_rule_ids,
                    policy_label=policy.label,
                    blockers=list(dict.fromkeys(blockers)),
                )
            )
        plan_id = secrets.token_hex(32)
        now = self.clock()
        plan = StewardRelocationPlan(
            plan_id=plan_id,
            plan_fingerprint=digest(
                [
                    plan_id,
                    request.expected_context,
                    inventory_fp,
                    [e.model_dump(mode="json") for e in entries],
                ]
            ),
            context_fingerprint=request.expected_context,
            inventory_fingerprint=inventory_fp,
            target_root_ref=request.inventory.root_ref,
            entries=entries,
            created_at=now,
            expires_at=now + timedelta(seconds=60),
            status="blocked" if any(e.blockers for e in entries) else "awaiting_review",
            counts={
                "selected": len(entries),
                "blocked": sum(bool(e.blockers) for e in entries),
                "rename": actual_renames,
                "move": actual_moves,
                "protected": sum(
                    any(
                        b.startswith("policy_preserve") or b == "dependent_bundle_review_required"
                        for b in e.blockers
                    )
                    for e in entries
                ),
                "collision": sum("destination_collision" in e.blockers for e in entries),
            },
        )
        with self.connection:
            self._save("plans", plan.plan_id, plan, MAX_PLANS)
            for entry in plan.entries:
                self._journal(plan, entry, "planned")
        return plan

    def _journal(self, plan, entry, phase):
        key = digest([plan.plan_id, entry.entry_id, phase])
        journal = StewardJournalEntry(
            journal_id=key,
            plan_id=plan.plan_id,
            entry_id=entry.entry_id,
            content_id=entry.content_id,
            before=entry.original_ref,
            target_root_ref=plan.target_root_ref,
            before_root_ref=entry.original_ref.source_ref,
            before_relative_path=entry.before_relative_path,
            destination=entry.destination,
            source_fingerprint=entry.source_fingerprint,
            inventory_fingerprint=plan.inventory_fingerprint,
            policy_fingerprint=entry.policy_fingerprint,
            policy_rule_ids=entry.policy_rule_ids,
            operation=entry.operation,
            phase=phase,
            recorded_at=self.clock(),
        )
        self._save("journal", key, journal, MAX_JOURNAL)

    async def approve(self, request: StewardBatchApproval):
        if self.lock.locked():
            raise StewardError("steward_busy")
        await self._check(request.expected_context)
        plan = self._load("plans", request.plan_id, StewardRelocationPlan)
        if (
            plan.plan_fingerprint != request.expected_plan_fingerprint
            or plan.inventory_fingerprint != request.expected_inventory_fingerprint
        ):
            raise StewardError("steward_stale_plan")
        if plan.status not in {"awaiting_review", "blocked"}:
            raise StewardError("steward_review_already_recorded")
        selected = set(request.selected_entry_ids)
        if len(selected) != len(request.selected_entry_ids) or selected != {e.entry_id for e in plan.entries}:
            # A subset or edited destinations always gets a new preview/digest.
            raise StewardError("steward_batch_selection_changed")
        if request.decision == "approve_preview":
            if (
                plan.context_fingerprint != request.expected_context
                or not plan.created_at <= self.clock() < plan.expires_at
            ):
                raise StewardError("steward_stale_plan")
            if plan.status == "blocked":
                raise StewardError("steward_plan_blocked")
            if self._trusted_inventory_fingerprint(plan.target_root_ref) != plan.inventory_fingerprint:
                raise StewardError("steward_destination_changed")
            items = await self._items(request.expected_context, [e.item_id for e in plan.entries], ready=True)
            if any(fingerprint(item) != entry.source_fingerprint for item, entry in zip(items, plan.entries)):
                raise StewardError("steward_stale_fingerprint")
            for item, entry in zip(items, plan.entries):
                policy = await self.policy_for(request.expected_context, item)
                if policy.fingerprint != entry.policy_fingerprint:
                    raise StewardError("steward_policy_changed")
                before_path = self.fixture_paths.get(item.item_id)
                if not before_path or before_path != entry.before_relative_path:
                    raise StewardError("steward_plan_diff_changed")
                renamed, moved, actual = relocation_effects(
                    before_path, entry.destination, item.original_ref.source_ref, plan.target_root_ref
                )
                if (
                    actual != entry.operation
                    or (renamed and not policy.rename_preview)
                    or (moved and not policy.move_preview)
                ):
                    # Existing pre-fix plans cannot inherit the old root-only rename approval.
                    raise StewardError("steward_plan_diff_changed")
        plan.status = "reviewed_preview" if request.decision == "approve_preview" else "rejected"
        with self.connection:
            self._save("plans", plan.plan_id, plan, MAX_PLANS)
            for entry in plan.entries:
                self._journal(
                    plan, entry, "preview_approved" if plan.status == "reviewed_preview" else "rejected"
                )
        return plan

    def plans(self):
        return [
            StewardRelocationPlan.model_validate_json(r[0])
            for r in self.connection.execute("SELECT body FROM steward_plans ORDER BY rowid DESC LIMIT 64")
        ]

    def execute(self, *_args, **_kwargs):
        raise StewardError("steward_executor_disabled")
