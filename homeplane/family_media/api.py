"""Opt-in router: integration owner mounts it under existing Friday auth/origin guards.

Creating the router has no source side effects. There is no hidden scheduler,
credential endpoint or unreviewed filesystem mutation. Originals retain their
bounded octet-stream path; photo derivatives use a separately qualified decoder.
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
from .photo_requests import FamilyMediaPhotoJob, FamilyMediaPhotoOpen, FamilyMediaPhotoRender
from .photos import FamilyPhotos


def create_router(catalog):
    router = APIRouter(prefix="/api/family-media", tags=["family-media"])
    if catalog.photos is None:
        catalog.photos = FamilyPhotos(catalog)

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

    @router.post("/photos/open", response_model=FamilyMediaPhotoJob, status_code=202)
    async def photo_open(request: FamilyMediaPhotoOpen):
        async def start():
            return await catalog.photos.start(request, kind="open")
        return await call(start)

    @router.post("/photos/render", response_model=FamilyMediaPhotoJob, status_code=202)
    async def photo_render(request: FamilyMediaPhotoRender):
        async def start():
            return await catalog.photos.start(request, kind="render")
        return await call(start)

    @router.get("/photos/jobs/{job_id}", response_model=FamilyMediaPhotoJob)
    async def photo_job(job_id: str):
        return await call(catalog.photos.status, job_id)

    @router.delete("/photos/jobs/{job_id}", response_model=FamilyMediaPhotoJob)
    async def photo_cancel(job_id: str):
        return await call(catalog.photos.cancel, job_id)

    @router.get("/photos/content/{job_id}")
    async def photo_content(job_id: str):
        output, payload = await call(catalog.photos.content, job_id)
        extension = "jpg" if output.result.output_format == "JPEG" else "png"
        disposition = "attachment" if output.purpose == "export" else "inline"
        return Response(payload, media_type=output.result.content_type, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{disposition}; filename="Friday-photo-{job_id[:8]}.{extension}"',
            "X-Friday-Photo-SHA256": output.result.output_sha256,
        })

    return router
