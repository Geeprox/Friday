"""Opt-in router: integration owner mounts it under existing Friday auth/origin guards.

Creating the router has no source side effects. There is no hidden scheduler,
credential endpoint or unreviewed filesystem mutation. Media bytes are served as
octet-stream until a separate bounded format/decoder qualification exists.
"""

from fastapi import APIRouter, Header, HTTPException, Response

from homeplane.archive_playback import byte_range

from .catalog import FamilyMediaError
from .contracts import (
    FamilyMediaAssetEvidence,
    FamilyMediaAssetRequest,
    FamilyMediaContext,
    FamilyMediaOriginalPlan,
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
from .reader import RANGE_BYTES


def create_router(catalog):
    router = APIRouter(prefix="/api/family-media", tags=["family-media"])

    async def call(method, *args):
        try:
            return await method(*args)
        except FamilyMediaError as error:
            code = error.code
            status = 404 if code.endswith("not_found") else 409
            raise HTTPException(status_code=status, detail={"code": code}) from None

    @router.get("/context", response_model=FamilyMediaContext)
    async def context():
        return await call(catalog.context)

    @router.post("/scan", response_model=FamilyMediaScanResult)
    async def scan(request: FamilyMediaScanRequest):
        return await call(catalog.scan, request)

    @router.post("/source-review", response_model=FamilyMediaContext)
    async def source_review(request: FamilyMediaSourceReview):
        return await call(catalog.review_source, request)

    @router.post("/query", response_model=FamilyMediaPage)
    async def query(request: FamilyMediaQuery):
        return await call(catalog.list_items, request)

    @router.post("/asset", response_model=FamilyMediaAssetEvidence)
    async def asset(request: FamilyMediaAssetRequest):
        return await call(catalog.asset_evidence, request)

    @router.post("/resolve", response_model=FamilyMediaResolution)
    async def resolve(request: FamilyMediaResolveRequest):
        return await call(catalog.resolve, request)

    @router.post("/original-plans", response_model=FamilyMediaOriginalPlan)
    async def prepare(request: FamilyMediaResolveRequest):
        return await call(catalog.prepare_original, request)

    @router.delete("/original-plans/{plan_id}")
    async def revoke(plan_id: str):
        return catalog.revoke_original(plan_id)

    @router.get("/originals/{plan_id}")
    async def original(plan_id: str, range_header: str | None = Header(default=None, alias="Range")):
        catalog._prune_leases()
        lease = catalog.leases.get(plan_id)
        if lease is None:
            raise HTTPException(404, detail={"code": "family_media_original_expired"})
        size = lease.plan.size_bytes
        if range_header is None and size > RANGE_BYTES:
            raise HTTPException(416, detail={"code": "family_media_range_required"})
        try:
            start, length, partial = byte_range(range_header, size)
        except ValueError:
            raise HTTPException(416, detail={"code": "family_media_invalid_range"}) from None
        length = min(length, RANGE_BYTES)
        content = await call(catalog.read_original, plan_id, start, length)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if partial:
            headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
        return Response(
            content,
            status_code=206 if partial else 200,
            headers=headers,
            media_type="application/octet-stream",
        )

    @router.post("/recipes/preview", response_model=FamilyMediaRecipe)
    async def preview(request: FamilyMediaRecipeRequest):
        return await call(catalog.preview_recipe, request)

    @router.post("/recipes/review", response_model=FamilyMediaRecipe)
    async def review(request: FamilyMediaRecipeReview):
        return await call(catalog.review_recipe, request)

    return router
