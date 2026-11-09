"""从合成 NAS 的完整原件到真实像素导出，覆盖撤销与来源变化。"""

import asyncio
import hashlib
import io
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image

from homeplane.family_media.api import create_router
from homeplane.family_media.catalog import FamilyMediaCatalog, FamilyMediaError
from homeplane.family_media.contracts import (
    FamilyMediaQuery, FamilyMediaRecipeRequest, FamilyMediaRecipeReview, FamilyMediaScanRequest,
)
from homeplane.family_media.fixture import SyntheticNASReader
from homeplane.family_media.photo_requests import FamilyMediaPhotoOpen, FamilyMediaPhotoRender
from homeplane.family_media.photo_runner import PhotoRunner
from homeplane.family_media.photos import FamilyPhotos
from tests.test_photo_codec import colors, image_bytes, launch_worker


@pytest_asyncio.fixture
async def photos(tmp_path):
    payload = image_bytes(colors(), exif=Image.Exif())
    reader = SyntheticNASReader()
    reader.put("SIMULATED-photo", payload)
    clock = [datetime(2026, 10, 1, tzinfo=UTC)]
    mono = [100.0]
    connection = sqlite3.connect(tmp_path / "SIMULATED-photo.sqlite")
    catalog = FamilyMediaCatalog(connection, reader, simulated=True,
        clock=lambda: clock[0], monotonic=lambda: mono[0])
    for _ in range(2):
        await catalog.scan(FamilyMediaScanRequest(expected_context=(await catalog.context()).fingerprint))
        clock[0] += timedelta(seconds=3)
    item = (await catalog.list_items(FamilyMediaQuery(expected_context=(await catalog.context()).fingerprint))).items[0]
    assert item.status == "ready"
    runner = PhotoRunner(launcher=launch_worker)
    catalog.photos = FamilyPhotos(catalog, runner=runner)
    fixture = SimpleNamespace(catalog=catalog, service=catalog.photos, reader=reader, payload=payload,
        clock=clock, mono=mono, item=item, connection=connection)
    yield fixture
    await catalog.close()
    assert runner.active is None and runner.process is None and not catalog.leases
    connection.close()


async def opening(env, nonce="1"):
    return FamilyMediaPhotoOpen(expected_context=(await env.catalog.context()).fingerprint,
        item_id=env.item.item_id, expected_original_hash=env.item.original_hash, request_id=nonce*64)


async def rendered(env, *, purpose="export", nonce="2", review=True, operations=None):
    recipe = await env.catalog.preview_recipe(FamilyMediaRecipeRequest(
        expected_context=(await env.catalog.context()).fingerprint, item_id=env.item.item_id,
        expected_original_hash=env.item.original_hash,
        operations=operations or [{"operation": "crop", "x": .5, "y": 0, "width": .5, "height": 1},
                                  {"operation": "rotate", "degrees_clockwise": 90}]))
    if review:
        recipe = await env.catalog.review_recipe(FamilyMediaRecipeReview(expected_context=recipe.context_fingerprint,
            recipe_id=recipe.recipe_id, decision="reviewed"))
    return FamilyMediaPhotoRender(expected_context=recipe.context_fingerprint, recipe_id=recipe.recipe_id,
        purpose=purpose, output_format="PNG", request_id=nonce*64)


async def finished(env, job):
    lease = env.service.jobs[job.job_id]
    await asyncio.wait_for(asyncio.shield(lease.task), 5)
    return await env.service.status(job.job_id)


@pytest.mark.asyncio
async def test_complete_http_preview_review_render_and_download_has_actual_checked_pixels(photos):
    env = photos
    app = FastAPI();app.include_router(create_router(env.catalog))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://SIMULATED") as client:
        request = await opening(env)
        response = await client.post("/api/family-media/photos/open", json=request.model_dump(mode="json"))
        assert response.status_code == 202 and response.json()["status"] == "processing"
        job = await finished(env, env.service.jobs[response.json()["job_id"]].state)
        assert job.output.purpose == "preview" and not job.output.saved_to_file
        preview = await client.get(job.output.content_url)
        assert preview.headers["content-type"] == "image/png" and "inline" in preview.headers["content-disposition"]
        with Image.open(io.BytesIO(preview.content)) as image:
            assert image.tobytes() == colors().tobytes()
        draft = await client.post("/api/family-media/recipes/preview", json={
            "expected_context": request.expected_context, "item_id": request.item_id,
            "expected_original_hash": request.expected_original_hash,
            "operations": [{"operation": "crop", "x": .5, "y": 0, "width": .5, "height": 1},
                           {"operation": "rotate", "degrees_clockwise": 90}],
        })
        assert draft.status_code == 200 and not draft.json()["rendered_derivative"]
        reviewed = await client.post("/api/family-media/recipes/review", json={
            "expected_context": request.expected_context, "recipe_id": draft.json()["recipe_id"], "decision": "reviewed",
        })
        assert reviewed.status_code == 200 and reviewed.json()["status"] == "reviewed"
        body = {"expected_context": request.expected_context, "request_id": "2"*64,
                "recipe_id": draft.json()["recipe_id"], "purpose": "export", "output_format": "PNG"}
        response = await client.post("/api/family-media/photos/render", json=body)
        assert response.status_code == 202
        export = await finished(env, env.service.jobs[response.json()["job_id"]].state)
        actual = await client.get(export.output.content_url)
        assert actual.status_code == 200 and "attachment" in actual.headers["content-disposition"]
        assert actual.headers["cache-control"] == "no-store" and actual.headers["x-content-type-options"] == "nosniff"
        assert hashlib.sha256(actual.content).hexdigest() == export.output.result.output_sha256
        assert actual.headers["x-friday-photo-sha256"] == export.output.result.output_sha256
        with Image.open(io.BytesIO(actual.content)) as image:
            expected = colors().crop((2, 0, 4, 2)).transpose(Image.Transpose.ROTATE_270)
            assert image.size == expected.size and image.tobytes() == expected.tobytes()
        assert export.output.rendered_derivative and not export.output.original_mutated
        assert not export.output.saved_to_file and env.reader.objects["SIMULATED-photo"]["bytes"] == env.payload
        assert not env.catalog.leases and env.service.runner.process is None
        revoked = await client.delete("/api/family-media/photos/jobs/"+export.job_id)
        assert revoked.json()["status"] == "cancelled"
        assert (await client.get(export.output.content_url)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,review,expected", [("export",False,"failed"),("preview",False,"ready"),("export",True,"ready")])
async def test_export_requires_exact_review_but_non_destructive_preview_does_not(photos, purpose, review, expected):
    request = await rendered(photos, purpose=purpose, review=review)
    job = await finished(photos, await photos.service.start(request, kind="render"))
    assert job.status == expected
    if expected == "failed":
        assert job.failure == "photo_recipe_review_required" and not job.output
        assert not any(call[0] == "range" for call in photos.reader.calls[-1:])


@pytest.mark.asyncio
async def test_same_metadata_changed_bytes_are_detected_by_complete_source_hash(photos):
    photos.reader.objects["SIMULATED-photo"]["bytes"] = photos.payload[:-1] + bytes([photos.payload[-1]^1])
    job = await finished(photos, await photos.service.start(await opening(photos), kind="open"))
    assert job.status == "failed" and job.failure == "photo_original_changed"
    assert not job.output and not photos.catalog.leases and photos.service.runner.active is None


@pytest.mark.asyncio
async def test_idempotent_lost_reply_conflict_and_cancel_before_task_starts(photos):
    request = await opening(photos)
    first = await photos.service.start(request, kind="open")
    again = await photos.service.start(request, kind="open")
    assert first.job_id == again.job_id and len(photos.service.jobs) == 1
    changed = request.model_copy(update={"expected_original_hash": "0"*64})
    with pytest.raises(FamilyMediaError, match="photo_request_conflict"):
        await photos.service.start(changed, kind="open")
    cancelled = await photos.service.cancel(first.job_id)
    assert cancelled.status == "cancelled" and photos.service.active is None
    assert (await photos.service.start(request, kind="open")).status == "cancelled"
    job = await finished(photos, await photos.service.start(await opening(photos,"3"), kind="open"))
    assert job.status == "ready"


@pytest.mark.asyncio
async def test_source_epoch_change_during_real_decoder_never_publishes_the_output(photos):
    decoded, release = asyncio.Event(), asyncio.Event()
    runner = photos.service.runner
    original = runner.render
    async def held(*args):
        result = await original(*args)
        decoded.set();await release.wait();return result
    runner.render = held
    job = await photos.service.start(await opening(photos), kind="open")
    await asyncio.wait_for(decoded.wait(), 3)
    photos.reader.epoch = "SIMULATED-epoch-2"
    release.set()
    lease = photos.service.jobs[job.job_id]
    await asyncio.wait_for(lease.task, 3)
    assert lease.state.status == "failed" and lease.payload is None
    assert not lease.state.output and not photos.catalog.leases
    with pytest.raises(FamilyMediaError):
        await photos.service.content(job.job_id)


@pytest.mark.asyncio
async def test_result_expiry_and_source_revocation_deny_content_without_reprocessing(photos):
    job = await finished(photos, await photos.service.start(await opening(photos), kind="open"))
    before = list(photos.reader.calls)
    photos.reader.state = "disabled"
    with pytest.raises(FamilyMediaError):
        await photos.service.content(job.job_id)
    photos.reader.state = "online"
    photos.mono[0] += 601
    with pytest.raises(FamilyMediaError, match="photo_job_not_found"):
        await photos.service.content(job.job_id)
    assert not photos.service.jobs and not photos.service.active
    assert not any(call[0] == "range" for call in photos.reader.calls[len(before):])


@pytest.mark.asyncio
async def test_renderer_busy_real_child_cancellation_and_close_reap_before_capacity_release(photos):
    from tests.test_photo_runner import idle_child
    started = asyncio.Event();processes = []
    async def launch():
        child = await idle_child();processes.append(child)
        assert await child.stderr.readline() == b"SIMULATED-PHOTO-READY\n"
        started.set();return child
    photos.service.runner.launcher = launch
    job = await photos.service.start(await opening(photos), kind="open")
    await asyncio.wait_for(started.wait(), 3)
    with pytest.raises(FamilyMediaError, match="photo_busy"):
        await photos.service.start(await opening(photos,"3"), kind="open")
    cancel = asyncio.create_task(photos.service.cancel(job.job_id))
    cancel.cancel();cancel.cancel()
    # 撤销调用本身尚未开始时，由目录关闭继续承担子进程回收。
    await asyncio.gather(cancel, return_exceptions=True)
    await photos.catalog.close()
    assert processes[0].returncode is not None and photos.service.runner.active is None
    assert not photos.service.jobs and not photos.catalog.leases


@pytest.mark.asyncio
async def test_real_desktop_composition_only_allows_typed_same_origin_photo_operations(photos, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    origin = "http://127.0.0.1:8740"
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "desktop"))
    monkeypatch.setenv("FRIDAY_EMBODIED_DATA", str(tmp_path / "embodied"))
    monkeypatch.setenv("FRIDAY_SETTINGS", str(tmp_path / "settings.json"))
    spec = importlib.util.spec_from_file_location("photo_desktop_guard",
        Path(__file__).resolve().parents[1] / "native/FridayDesktop/backend/desktop_backend.py")
    backend = importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
    inner = FastAPI();inner.include_router(create_router(photos.catalog))
    inner.state.family_media = photos.catalog
    inner.state.core = SimpleNamespace(actions=SimpleNamespace(policy_report=lambda: {"enabled":False,"configured":False}))
    wrapper = backend.DesktopService();wrapper.inner = inner
    body = (await opening(photos)).model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper), base_url=origin) as client:
        for headers in ({}, {"Origin":"https://invalid.example"}, {"Origin":origin,"Host":"invalid.example:8740"}):
            assert (await client.post("/api/family-media/photos/open", json=body, headers=headers)).status_code == 403
        assert not photos.service.jobs
        assert (await client.post("/api/family-media/photos/open", json={**body,"path":"SIMULATED/private"},
            headers={"Origin":origin})).status_code == 422
        assert (await client.post("/api/family-media/photos/open", content=b"x"*8193,
            headers={"Origin":origin})).status_code == 413
        accepted = await client.post("/api/family-media/photos/open", json=body, headers={"Origin":origin})
        assert accepted.status_code == 202
        job = await finished(photos, photos.service.jobs[accepted.json()["job_id"]].state)
        for headers in ({}, {"Sec-Fetch-Site":"cross-site"}, {"Sec-Fetch-Site":"same-site"}):
            assert (await client.get(job.output.content_url, headers=headers)).status_code == 409
        for headers in ({"Origin":origin}, {"Sec-Fetch-Site":"same-origin"}):
            response = await client.get(job.output.content_url, headers=headers)
            assert response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == job.output.result.output_sha256
        assert (await client.get("/api/family-media/originals/"+"a"*64, headers={"Origin":origin})).status_code == 409
        assert (await client.delete("/api/family-media/photos/jobs/"+job.job_id)).status_code == 403
        assert (await client.delete("/api/family-media/photos/jobs/"+job.job_id, headers={"Origin":origin})).status_code == 200
        assert (await client.get(job.output.content_url, headers={"Origin":origin})).status_code == 409
        for path in ("/api/family-media/photos/write", "/api/family-media/originals/"+"a"*64):
            assert (await client.post(path, json={}, headers={"Origin":origin})).status_code == 409
    async with httpx.AsyncClient(transport=httpx.ASGITransport(wrapper, client=("192.0.2.11",1234)), base_url=origin) as client:
        assert (await client.post("/api/family-media/photos/open", json=body, headers={"Origin":origin})).status_code == 403


@pytest.mark.asyncio
async def test_rejecting_a_recipe_while_preview_is_decoding_prevents_delivery(photos):
    request = await rendered(photos, purpose="preview", review=False)
    started, release = asyncio.Event(), asyncio.Event()
    original = photos.service.runner.render
    async def held(*args):
        result = await original(*args)
        started.set();await release.wait();return result
    photos.service.runner.render = held
    job = await photos.service.start(request, kind="render")
    await asyncio.wait_for(started.wait(), 3)
    await photos.catalog.review_recipe(FamilyMediaRecipeReview(expected_context=request.expected_context,
        recipe_id=request.recipe_id, decision="rejected"))
    release.set()
    result = await finished(photos, job)
    assert result.status == "failed" and result.failure == "photo_recipe_review_required"
    assert result.output is None and photos.service.jobs[job.job_id].payload is None


@pytest.mark.asyncio
async def test_repeated_cancel_and_late_launch_keep_capacity_until_actual_child_is_reaped(photos):
    from tests.test_photo_runner import idle_child
    started, release = asyncio.Event(), asyncio.Event()
    processes = []
    async def launch():
        child = await idle_child();processes.append(child)
        assert await child.stderr.readline() == b"SIMULATED-PHOTO-READY\n"
        started.set();await release.wait();return child
    photos.service.runner.launcher = launch
    job = await photos.service.start(await opening(photos), kind="open")
    await asyncio.wait_for(started.wait(), 3)
    entered = asyncio.Event()
    async def cancelling():
        entered.set();return await photos.service.cancel(job.job_id)
    cancel = asyncio.create_task(cancelling())
    try:
        await entered.wait()
        cancel.cancel();cancel.cancel()
        assert photos.service.active is not None and processes[0].returncode is None
        with pytest.raises(FamilyMediaError, match="photo_busy"):
            await photos.service.start(await opening(photos,"3"), kind="open")
        assert not cancel.done()
    finally:
        release.set()
        result = await asyncio.wait_for(cancel, 3)
    assert result.status == "cancelled" and processes[0].returncode is not None
    assert photos.service.active is None and photos.service.runner.active is None


@pytest.mark.asyncio
async def test_closed_source_convention_is_not_bypassed_and_invalid_images_do_not_become_results(photos):
    for closed, payload, failure in [(None, photos.payload, "photo_original_changed_or_not_ready"),
                                      (True, b"SIMULATED not an image", "photo_decode_failed")]:
        photos.reader.put("SIMULATED-photo", payload, revision="SIMULATED-new-"+str(closed), closed=closed)
        for _ in range(2):
            await photos.catalog.scan(FamilyMediaScanRequest(expected_context=(await photos.catalog.context()).fingerprint))
            photos.clock[0] += timedelta(seconds=3)
        photos.item = (await photos.catalog.list_items(FamilyMediaQuery(expected_context=(await photos.catalog.context()).fingerprint))).items[0]
        # 未完成来源没有已验证 hash；请求仍使用原会话的摘要，不赋予关闭证明。
        request = FamilyMediaPhotoOpen(expected_context=(await photos.catalog.context()).fingerprint,
            item_id=photos.item.item_id, expected_original_hash=hashlib.sha256(payload).hexdigest(),
            request_id=("4" if closed is None else "5")*64)
        result = await finished(photos, await photos.service.start(request, kind="open"))
        assert result.status == "failed" and result.failure == failure
        assert result.output is None and not photos.catalog.leases


@pytest.mark.asyncio
async def test_capacity_is_reserved_before_launch_and_expiry_releases_held_bytes(photos, monkeypatch):
    import homeplane.family_media.photos as limits
    job = await finished(photos, await photos.service.start(await opening(photos), kind="open"))
    held = len(photos.service.jobs[job.job_id].payload)
    monkeypatch.setattr(limits, "MAX_HELD_BYTES", limits.MAX_RENDER_BYTES + held - 1)
    before = list(photos.reader.calls)
    with pytest.raises(FamilyMediaError, match="photo_capacity"):
        await photos.service.start(await opening(photos,"3"), kind="open")
    assert photos.service.active is None and photos.reader.calls == before
    photos.mono[0] += 601
    assert not photos.service.owns_result(job.job_id) and not photos.service.jobs
    result = await finished(photos, await photos.service.start(await opening(photos,"3"), kind="open"))
    assert result.status == "ready"


@pytest.mark.asyncio
async def test_whole_job_deadline_reaps_real_child_and_records_fixed_failure(photos, monkeypatch):
    import homeplane.family_media.photos as limits
    from tests.test_photo_runner import idle_child
    processes = []
    async def launch():
        process = await idle_child();processes.append(process);return process
    photos.service.runner.launcher = launch
    monkeypatch.setattr(limits, "JOB_SECONDS", .2)
    job = await finished(photos, await photos.service.start(await opening(photos), kind="open"))
    assert job.status == "failed" and job.failure == "photo_timeout"
    assert processes and processes[0].returncode is not None
    assert photos.service.active is None and photos.service.runner.active is None and not photos.catalog.leases


@pytest.mark.asyncio
async def test_successful_export_lease_cannot_outlive_its_review_or_be_claimed_as_saved(photos):
    job = await finished(photos, await photos.service.start(await rendered(photos), kind="render"))
    assert job.output.rendered_derivative and not job.output.saved_to_file
    photos.clock[0] += timedelta(seconds=301)
    with pytest.raises(FamilyMediaError, match="family_media_recipe_expired_or_context_changed"):
        await photos.service.content(job.job_id)
    with pytest.raises(FamilyMediaError, match="family_media_recipe_expired_or_context_changed"):
        await photos.service.status(job.job_id)


@pytest.mark.asyncio
async def test_idle_result_is_released_on_timer_without_another_request(photos, monkeypatch):
    import homeplane.family_media.photos as limits
    from homeplane.family_media.photo_codec import render_photo
    expired = asyncio.Event()
    original = photos.service._expire
    def observed(job_id):
        original(job_id);expired.set()
    async def quick(payload, request):
        return render_photo(payload, request)
    photos.service._expire = observed
    photos.service.runner.render = quick
    monkeypatch.setattr(limits, "LEASE_SECONDS", 1)
    job = await finished(photos, await photos.service.start(await opening(photos), kind="open"))
    lease = photos.service.jobs[job.job_id]
    assert lease.payload is not None
    await asyncio.wait_for(expired.wait(), 3)
    assert lease.payload is None and not photos.service.jobs and not photos.service.active
    assert lease.state.status == "expired"
