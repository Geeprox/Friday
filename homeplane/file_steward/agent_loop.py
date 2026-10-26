"""Source-only Agent Loop seam over Friday owners. No new session or scheduler."""

import asyncio
import inspect
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field, model_validator

from homeplane.contracts import Contract
from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import FamilyMediaScanRequest, OpaqueRef

from .service import StewardError
from .session import session_projection


class StewardAgentReadScope(Contract):
    grant_id: OpaqueRef
    subject_ref: OpaqueRef
    source_ref: OpaqueRef
    config_generation: int = Field(ge=0)
    connection_epoch: OpaqueRef
    directory_refs: list[OpaqueRef] = Field(min_length=1, max_length=32)
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    max_observed_entries: int = Field(default=256, ge=1, le=1024)
    max_read_bytes: int = Field(default=8388608, ge=0, le=67108864)

    @model_validator(mode="after")
    def finite_scope(self):
        if not 0 < (self.expires_at - self.approved_at).total_seconds() <= 900:
            raise ValueError("steward_finite_source_scope_required")
        if len(set(self.directory_refs)) != len(self.directory_refs):
            raise ValueError("steward_duplicate_directory_scope")
        return self


class AgentLoopFiles:
    def __init__(self, steward, *, scope_authorizer=None, session_status=None, clock=None):
        self.steward, self.catalog = steward, steward.catalog
        self.authorizer, self.session_status = scope_authorizer, session_status
        self.clock = clock or (lambda: datetime.now(UTC))
        self.fixture_lock = asyncio.Lock()

    async def _authorized(self, scope, expected_context):
        if not self.authorizer:
            raise StewardError("steward_agent_scope_unbound")
        allowed = self.authorizer(scope)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True or not scope.approved_at <= self.clock() < scope.expires_at:
            raise StewardError("steward_agent_scope_denied")
        context = await self.steward._check(expected_context)
        if (context.source_ref, context.config_generation, context.connection_epoch) != (
            scope.source_ref,
            scope.config_generation,
            scope.connection_epoch,
        ):
            raise StewardError("steward_agent_binding_changed")
        if self.session_status:
            status = self.session_status()
            if inspect.isawaitable(status):
                status = await status
            if session_projection(status)["reason"] == "authentication_required":
                raise StewardError("steward_agent_authentication_required")
        return context

    async def query(self, scope, request):
        await self._authorized(scope, request.expected_context)
        page = await self.steward.page(request, directory_refs=scope.directory_refs,
                                       read_scope=digest(scope.model_dump(mode="json")))
        allowed = [{key: value for key, value in row.items() if key != "jobs"} for row in page["items"]]
        try:
            await self._authorized(scope, request.expected_context)  # discard late revoked results
        except BaseException:
            if request.search_mode == "indexed_cache" and hasattr(self.steward, "search"):
                self.steward.search.discard_cursor(page["next_cursor"])
            raise
        return {**page, "items": allowed, "grant_id": scope.grant_id, "source_read_enabled": False}

    async def step_fixture(self, scope, expected_context, *, directory_ref, limit=128, byte_budget=0):
        """Finite synthetic discovery only; a real ARL scheduler must be separately adopted."""
        if not self.catalog.simulated:
            raise StewardError("steward_executor_disabled")
        if self.fixture_lock.locked():
            raise StewardError("steward_busy")
        async with self.fixture_lock:
            await self._authorized(scope, expected_context)
            if directory_ref not in scope.directory_refs:
                raise StewardError("steward_agent_directory_denied")
            request = FamilyMediaScanRequest(
                expected_context=expected_context,
                directory_ref=None if directory_ref == "root" else directory_ref,
                limit=limit,
                byte_budget=byte_budget,
            )
            conn = self.steward.connection
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS steward_agent_windows (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, entries INTEGER NOT NULL, bytes INTEGER NOT NULL)"
                )
                row = conn.execute(
                    "SELECT fingerprint, entries, bytes FROM steward_agent_windows WHERE id=?",
                    (scope.grant_id,),
                ).fetchone()
                proof = digest(scope.model_dump(mode="json"))
                used_entries, used_bytes = (row[1], row[2]) if row else (0, 0)
                if row and row[0] != proof:
                    raise StewardError("steward_agent_grant_changed")
                if not row and conn.execute("SELECT COUNT(*) FROM steward_agent_windows").fetchone()[0] >= 64:
                    raise StewardError("steward_capacity_review_required")
                if (
                    used_entries + limit > scope.max_observed_entries
                    or used_bytes + byte_budget > scope.max_read_bytes
                ):
                    raise StewardError("steward_agent_window_exhausted")
                conn.execute(
                    "INSERT OR REPLACE INTO steward_agent_windows VALUES (?,?,?,?)",
                    (scope.grant_id, proof, used_entries + limit, used_bytes + byte_budget),
                )
            # Existing catalog owns checkpoints, hash state, fencing, reader and cancellation.
            result = await self.catalog.scan(request)
            await self._authorized(scope, result.context.fingerprint)
            return result

    def read_original(self, *_args, **_kwargs):
        raise StewardError("steward_source_read_executor_disabled")
