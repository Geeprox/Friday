"""Existing same-origin guards; explicit selected text reads, no file writes or cloud uploads."""

from fastapi import APIRouter, HTTPException
from pydantic import Field

from homeplane.contracts import Contract
from homeplane.family_media.catalog import FamilyMediaError
from homeplane.family_media.contracts import Digest

from .contracts import (
    StewardBatchApproval,
    StewardJobRequest,
    StewardPageRequest,
    StewardPreviewRequest,
    StewardSuggestionRequest,
)
from .extraction_queue import StewardQueueControl
from .policy import StewardPolicyEdit
from .service import StewardError
from .settings_contracts import StewardSettingsEdit, StewardSettingsRequest
from .local_text import SelectedTextPreparation, SelectedTextRequest


class CompareRequest(Contract):
    expected_context: Digest
    item_ids: list[Digest] = Field(min_length=2, max_length=2)


def create_router(steward):
    router = APIRouter(prefix="/api/files", tags=["files-preview"])

    async def call(method, *args):
        try:
            return await method(*args)
        except (StewardError, FamilyMediaError) as error:
            raise HTTPException(409, detail={"code": error.code}) from None

    @router.get("/context")
    async def context():
        return await call(steward.context)

    @router.post("/query")
    async def query(request: StewardPageRequest):
        return await call(steward.page, request)

    @router.post("/policies/preview")
    async def policy(request: StewardPolicyEdit):
        return await call(steward.set_policy, request)

    @router.post("/settings/query")
    async def settings(request: StewardSettingsRequest):
        return await call(steward.settings.query, request)

    @router.post("/settings/preview")
    async def settings_preview(request: StewardSettingsEdit):
        return await call(steward.settings.save, request)

    @router.get("/queue")
    async def queue():
        return steward.queue.public_records()

    @router.post("/queue/review")
    async def queue_review(request: StewardQueueControl):
        return await call(steward.queue.control, request)

    @router.post("/reconcile")
    async def reconcile(request: StewardPageRequest):
        return await call(steward.reconcile, request)

    @router.post("/jobs/preview")
    async def jobs(request: StewardJobRequest):
        return await call(steward.plan_jobs, request)

    @router.post("/content/text")
    async def extract_text(request: SelectedTextRequest):
        if steward.local_extraction is None:
            raise HTTPException(409, detail={"code": "steward_executor_disabled"})
        return await call(steward.local_extraction.start, request)

    @router.post("/content/text/prepare")
    async def prepare_text(request: SelectedTextPreparation):
        if steward.local_extraction is None:
            raise HTTPException(409, detail={"code": "steward_executor_disabled"})
        return await call(steward.local_extraction.prepare, request)

    @router.post("/suggest")
    async def suggest(request: StewardSuggestionRequest):
        return await call(steward.suggest, request)

    @router.post("/compare")
    async def compare(request: CompareRequest):
        return await call(steward.compare, request.expected_context, *request.item_ids)

    @router.post("/relocations/preview")
    async def preview(request: StewardPreviewRequest):
        return await call(steward.preview, request)

    @router.post("/relocations/review")
    async def review(request: StewardBatchApproval):
        return await call(steward.approve, request)

    @router.get("/relocations")
    async def plans():
        return steward.plans()

    return router
