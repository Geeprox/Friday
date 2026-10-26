"""Loopback-only Friday UI with synthetic tempfile content; never constructs a NAS manager."""

import asyncio
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from homeplane.core import HomeCore
from homeplane.family_media import FamilyMediaCatalog
from homeplane.family_media.catalog import digest
from homeplane.family_media.contracts import FamilyMediaQuery, FamilyMediaScanRequest
from homeplane.family_media.fixture import SyntheticNASReader
from homeplane.file_steward import FileSteward
from homeplane.file_steward.api import create_router
from homeplane.file_steward.contracts import (
    StewardJobRequest,
    StewardPageOutcome,
    StewardPreviewRequest,
    StewardSourceAnchor,
    StewardUnit,
)
from homeplane.file_steward.policy import StewardPolicyEdit, StewardPolicyRule
from homeplane.file_steward.providers import ExtractionStep, ProviderSpec
from homeplane.simulator import Simulator
from homeplane.store import TraceStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "fixture-article": ("other", "宇宙的尺度与时间", "downloads/hash-article.pdf", "downloaded"),
    "fixture-screenshot": ("image", "一张网页收藏截图", "downloads/903bcfd.png", "downloaded"),
    "fixture-clip": ("video", "科学短片：为什么天空是蓝色", "clips/32e719.mp4", "downloaded"),
    "fixture-camera": ("image", "旅行照片 · 原名保留", "Photos/IMG_20200915.jpg", "camera_phone"),
    "fixture-id": ("other", "证件资料 · 保留原位置", "Documents/ID.pdf", "sensitive"),
    "fixture-course": ("other", "大学课程资料包", "Courses/Lecture_01.ppt", "bundle"),
}


class FixtureProvider:
    spec = ProviderSpec("fixture", "1", "合成内容", "fixture", "fixture_only")

    async def step(self, job, budget):
        kind, title, _, _ = FIXTURES[job.original_ref.object_ref]
        if job.original_ref.object_ref == "fixture-article":
            page = job.completed_pages + 1
            anchor = StewardSourceAnchor(
                content_id=job.content_id, original_ref=job.original_ref, kind="page", page=page
            )
            units = (
                [
                    StewardUnit(
                        unit_id=digest([job.job_id, job.completed_units]),
                        ordinal=job.completed_units,
                        title=title,
                        text="合成示例：科学知识的来源与内容片段。此文字由 fixture 提供。",
                        anchor=anchor,
                        provider=job.provider,
                        provider_version=job.provider_version,
                    )
                ]
                if page == 1
                else []
            )
            outcome = StewardPageOutcome(
                anchor=anchor,
                status="text" if units else "needs_ocr",
                provider=job.provider,
                provider_version=job.provider_version,
                unit_id=units[0].unit_id if units else None,
                reason="embedded_text" if units else "no_embedded_text",
            )
            return ExtractionStep(
                units, "fixture-article-page-2" if page == 1 else None, page == 2, [outcome], 2
            )
        anchor = (
            {"kind": "video_time", "start_ms": 14000, "end_ms": 21000}
            if kind == "video"
            else {"kind": "image_region", "region": (0, 0, 1, 1)}
            if kind == "image"
            else {"kind": "page", "page": 2}
        )
        return ExtractionStep(
            [
                StewardUnit(
                    unit_id=digest([job.job_id, 0]),
                    ordinal=0,
                    title=title,
                    text="合成示例：科学知识的来源与内容片段。此文字由 fixture 提供。",
                    anchor=StewardSourceAnchor(
                        content_id=job.content_id, original_ref=job.original_ref, **anchor
                    ),
                    provider=job.provider,
                    provider_version=job.provider_version,
                )
            ],
            None,
            True,
        )


async def make_app(directory):
    reader = SyntheticNASReader()
    for key, (kind, _, _, _) in FIXTURES.items():
        path = directory / (key + ".txt")
        path.write_text("SYNTHETIC fixture content " + key)
        reader.put(key, path.read_bytes(), kind=kind)
    now = [datetime(2026, 10, 2, 12, tzinfo=UTC)]
    connection = sqlite3.connect(directory / "fixture.sqlite")
    catalog = FamilyMediaCatalog(
        connection,
        reader,
        simulated=True,
        clock=lambda: now[0],
        indexed_kinds=("image", "video", "audio", "other"),
    )
    directories = sorted({fields[2].split("/")[0] for fields in FIXTURES.values()})
    for folder in directories:
        reader.listing = [key for key, fields in FIXTURES.items() if fields[2].split("/")[0] == folder]
        for _ in range(3):
            await catalog.scan(
                FamilyMediaScanRequest(
                    expected_context=(await catalog.context()).fingerprint,
                    directory_ref="fixture-dir-" + folder,
                )
            )
            now[0] += timedelta(seconds=3)
    ctx = await catalog.context()
    items = (await catalog.list_items(FamilyMediaQuery(expected_context=ctx.fingerprint))).items
    steward = FileSteward(
        connection,
        catalog,
        clock=lambda: now[0],
        fixture_provider=FixtureProvider(),
        fixture_paths={i.item_id: FIXTURES[i.original_ref.object_ref][2] for i in items},
    )
    for item in items:
        _, title, _, origin = FIXTURES[item.original_ref.object_ref]
        downloaded = origin == "downloaded"
        scope = (await catalog.cached_scopes(ctx.fingerprint, [item.item_id]))[0]["directory_ref"]
        await steward.set_policy(
            StewardPolicyEdit(
                expected_context=ctx.fingerprint,
                rule=StewardPolicyRule(
                    rule_id=scope if downloaded else item.original_ref.object_ref,
                    source_ref=ctx.source_ref,
                    config_generation=ctx.config_generation,
                    connection_epoch=ctx.connection_epoch,
                    directory_ref=scope,
                    item_id=None if downloaded else item.item_id,
                    status="user_pinned",
                    label="网页与文章收藏"
                    if downloaded and "downloads" in scope
                    else "收藏片段"
                    if downloaded
                    else title,
                    origin=origin,
                    rename_preview=downloaded,
                    move_preview=downloaded,
                    structure_preview=downloaded,
                    dedup_review=origin != "sensitive",
                    unit="dependent_bundle" if origin == "bundle" else "file",
                ),
            )
        )
        jobs = await steward.plan_jobs(
            StewardJobRequest(expected_context=ctx.fingerprint, item_ids=[item.item_id], provider="fixture")
        )
        if item.original_ref.object_ref != "fixture-screenshot":
            await steward.queue.activate_fixture(jobs[0].job_id, ctx.fingerprint)
            entry = await steward.queue.step_fixture(ctx.fingerprint)
            if entry.status == "paused":
                await steward.queue.activate_fixture(jobs[0].job_id, ctx.fingerprint)
                await steward.queue.step_fixture(ctx.fingerprint)
    protected = next(i for i in items if i.original_ref.object_ref == "fixture-camera")
    preview_request = StewardPreviewRequest.model_validate(
        {
            "expected_context": ctx.fingerprint,
            "selections": [
                {
                    "item_id": protected.item_id,
                    "expected_content_id": protected.original_hash,
                    "destination": "archive/travel.jpg",
                    "operation": "rename_and_move",
                }
            ],
            "inventory": {
                "root_ref": "fixture-output",
                "namespace_revision": "1",
                "checked_at": now[0],
                "complete": True,
                "paths": [],
                "basis": "synthetic_fixture",
            },
        }
    )
    steward.register_fixture_inventory(preview_request.inventory)
    await steward.preview(preview_request)
    core = HomeCore(Simulator(), TraceStore(directory / "house.sqlite"))
    app = FastAPI()
    app.include_router(create_router(steward))

    @app.get("/")
    async def index():
        html = (
            (ROOT / "homeplane/static/index.html")
            .read_text()
            .replace("<body>", '<body data-file-steward-enabled="true">', 1)
        )
        return HTMLResponse(html)

    @app.get("/api/snapshot")
    async def snapshot():
        return await core.snapshot()

    app.mount("/static", StaticFiles(directory=ROOT / "homeplane/static"))
    return app, catalog, connection, core.store


async def main():
    with tempfile.TemporaryDirectory(prefix="friday-steward-fixture-") as work:
        app, catalog, connection, store = await make_app(Path(work))
        try:
            await uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=8769, log_level="warning")
            ).serve()
        finally:
            await catalog.close()
            connection.close()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
