"""背景媒体仍绑定原歌曲与票据；图片不会改变音频版本或控制权限。"""

import asyncio
import base64
import copy
import hashlib
import io
import json
import os
import random
import select
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from homeplane.intelligence import backgrounds as module
from homeplane.intelligence.backgrounds import (
    LEGACY_CANVAS, LEGACY_REQUEST_SIZE, MAX_IMAGE_BYTES, PROMPT_VERSION, QHD_CANVAS, UHD_CANVAS,
    SongBackgrounds, prepare_backdrop, song_identity,
)
from homeplane.intelligence.budget import EnhancementUnavailable
from homeplane.intelligence.settings import IntelligenceConfig
from homeplane.intelligence.settings import atomic_json
from homeplane.ktv_service import create_app
from homeplane.ktv_worker import BACKGROUND_PREFETCH_COUNT, MediaTickets, manifest_version
from scripts.ktv_build_workspace import build_workspace
from tests.test_intelligence_journeys import picture
from tests.test_intelligence_providers import runtime
from tests.test_ktv_coordinator import manifest
from tests.test_ktv_coordinator import store as store
from tests.test_ktv_worker import KEY, PreparedQueue, make_worker


async def test_4k_jpeg_reaches_tv_route_unchanged_with_original_job_ticket(tmp_path):
    requests = []
    source = solid_jpeg(UHD_CANVAS, "#23384c")

    def handler(request):
        requests.append(json.loads(request.content))
        return image_response(source)

    owner = runtime(tmp_path / "ai", handler)
    owner.settings.update({"cloud_images": True})
    images = SongBackgrounds(owner)
    queue = PreparedQueue()
    before = manifest_version(queue.job)
    images.request(queue.job)
    await asyncio.gather(*list(images.tasks.values()))
    token, _ = MediaTickets(KEY).issue(queue.job)
    app = create_app(queue, KEY, song_backgrounds=images)
    with TestClient(app, base_url="http://192.168.40.20:8769") as client:
        url = f"/v1/jobs/{queue.job_id}/media/background"
        assert client.get(url).status_code == 401
        response = client.get(url, headers={"X-KTV-Media": token})
        assert response.status_code == 200 and response.content == source
        assert response.headers["content-type"] == "image/jpeg"
        with Image.open(io.BytesIO(response.content)) as delivered:
            assert delivered.format == "JPEG" and delivered.size == UHD_CANVAS
        assert client.get(url, headers={"X-KTV-Media": token + "bad"}).status_code == 401
    assert manifest_version(queue.job) == before
    assert len(requests) == 1 and requests[0]["size"] == "3840x2160" and requests[0]["output_format"] == "jpeg"
    cached = images.cached_for(queue.job)
    assert (cached["width"], cached["height"]) == UHD_CANVAS


@pytest.mark.parametrize("debug", [False, True])
def test_owner_can_change_daily_image_limit_in_place_and_debug_never_waits(tmp_path, debug):
    calls = []

    def handler(request):
        calls.append(request)
        return image_response(solid_jpeg(UHD_CANVAS, "#23384c"))

    owner = runtime(tmp_path / "ai", handler)
    config = owner.settings.update({"cloud_images": True, "daily_images": 0, "daily_requests": 0})
    assert IntelligenceConfig().daily_images == 100
    images = SongBackgrounds(owner, debug=debug)
    queue = PreparedQueue()
    queue.job["simulated"] = False
    queue.jobs = {queue.job_id: queue.job}
    queue.lock = threading.Lock()
    origin = "http://127.0.0.1:8769"
    app = create_app(queue, KEY, console_origin=origin, song_backgrounds=images)
    prefix = "/v1/manage/backgrounds"
    with TestClient(app, base_url=origin, client=("127.0.0.1", 48123)) as client:
        assert client.get("/console/").status_code == 200
        value = client.get(prefix).json()
        assert value["daily_limit"] == 0 and value["daily_remaining"] == (None if debug else 0)
        endpoint = prefix + "/" + value["items"][0]["id"] + "/regenerate"
        headers = {"Origin": origin}
        if not debug:
            assert client.post(endpoint, json={}, headers=headers).json()["detail"] == "daily_images_reached"
            assert not calls
            assert client.post(prefix + "/policy", json={"daily_images": 100}).status_code == 403
            assert client.post(prefix + "/policy", json={"daily_images": True}, headers=headers).status_code == 400
            report = client.post(prefix + "/policy", json={"daily_images": 100}, headers=headers)
            assert report.json() == {"daily_limit": 100, "daily_remaining": 100}
        assert client.post(endpoint, json={}, headers=headers).status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (item := client.get(prefix).json()["items"][0])["ready"]:
            time.sleep(.03)
        assert item["ready"] and len(calls) == 1
        assert owner.status()["usage"]["used"]["images"] == 1
        assert owner.settings.read().daily_images == (0 if debug else 100)
        assert owner.settings.read().daily_requests == config.daily_requests


def test_generated_background_is_widescreen_and_preserves_theme_colors():
    source = io.BytesIO()
    frame = Image.new("RGB", LEGACY_REQUEST_SIZE, (32, 160, 80))
    frame.paste((180, 130, 30), (768, 0, 1536, 1024))
    frame.save(source, format="PNG")
    with Image.open(io.BytesIO(prepare_backdrop(source.getvalue()))) as delivered:
        assert delivered.format == "JPEG" and delivered.size == LEGACY_CANVAS
        assert delivered.getpixel((600, 432)) == pytest.approx((32, 160, 80), abs=2)
        assert delivered.getpixel((1150, 432)) == pytest.approx((180, 130, 30), abs=2)


def solid_png(color, size=LEGACY_REQUEST_SIZE):
    content = io.BytesIO()
    Image.new("RGB", size, color).save(content, "PNG")
    return content.getvalue()


def solid_jpeg(size, color):
    content = io.BytesIO()
    Image.new("RGB", size, color).save(content, "JPEG", quality=96)
    return content.getvalue()


def image_response(content):
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(content).decode()}]})


@pytest.mark.parametrize("reject_qhd", [False, True])
async def test_only_verified_size_rejection_falls_back_and_uses_one_image_slot(tmp_path, reject_qhd):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append((body["size"], body["output_format"], body["model"]))
        if body["size"] == "3840x2160" or reject_qhd and body["size"] == "2560x1440":
            status = 400 if body["size"] == "3840x2160" else 422
            return httpx.Response(status, json={"error": {
                "type": "invalid_request_error", "param": "size", "code": "invalid_value",
            }})
        return image_response(solid_png("#24344c") if reject_qhd else solid_jpeg(QHD_CANVAS, "#24344c"))

    owner = runtime(tmp_path / "ai", handler)
    model = "gpt-image-2.5-flare-2026-09-08"
    owner.settings.update({"cloud_images": True, "daily_images": 1, "image_model": model})
    images = SongBackgrounds(owner)
    song = {"song_id": "808", "title": "合成回退歌曲"}
    first = images.request(song)
    await asyncio.gather(*list(images.tasks.values()))
    expected = LEGACY_CANVAS if reject_qhd else QHD_CANVAS
    ready = images.cached(first["id"])
    assert ready and (ready["width"], ready["height"]) == expected
    with Image.open(io.BytesIO(images.read_image(first["id"]))) as delivered:
        assert delivered.format == "JPEG" and delivered.size == expected
    assert requests == [("3840x2160", "jpeg", model), ("2560x1440", "jpeg", model)] + (
        [("1536x1024", "png", model)] if reject_qhd else []
    )
    used = owner.status()["usage"]["used"]
    assert used["images"] == 1 and used["requests"] == 0
    assert owner.status()["usage"]["blocks"] == []


@pytest.mark.parametrize("outcome", ["ambiguous_400", "rate_limit", "timeout", "undersized_200"])
async def test_unverified_failure_or_undersized_response_never_retries_as_smaller_image(tmp_path, outcome):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content)["size"])
        if outcome == "undersized_200":
            return image_response(solid_jpeg(QHD_CANVAS, "#35524a"))
        if outcome == "timeout":
            raise httpx.ReadTimeout("等待合成响应", request=request)
        if outcome == "rate_limit":
            return httpx.Response(429, json={"error": {"param": "size", "code": "invalid_value"}})
        return httpx.Response(400, json={"error": {"message": "size error", "code": "invalid_value"}})

    owner = runtime(tmp_path / "ai", handler)
    config = owner.settings.update({"cloud_images": True, "daily_images": 2})
    images = SongBackgrounds(owner)
    song = {"song_id": "617", "title": "已有老背景的合成曲目"}
    cached_id = song_identity(song, config.image_model)
    content = solid_png("#293e51", LEGACY_CANVAS)
    images.directory.mkdir(mode=0o700)
    path = images.directory / (cached_id + ".png")
    path.write_bytes(content)
    path.chmod(0o600)
    atomic_json(images.directory / (cached_id + ".json"), {
        "prompt_version": PROMPT_VERSION, "render_revision": 2, "sha256": hashlib.sha256(content).hexdigest(),
    })
    assert images.request(song)["width"] == LEGACY_CANVAS[0] and not requests
    assert images.request(song, force=True)["status"] == "preparing"
    await asyncio.gather(*list(images.tasks.values()))
    assert requests == ["3840x2160"] and images.read_image(cached_id) == content
    assert images.cached(cached_id)["height"] == LEGACY_CANVAS[1]
    used = owner.status()["usage"]["used"]
    assert used["images"] == 1 and used["requests"] == 0


def test_full_resolution_detailed_jpeg_is_never_remapped_or_recompressed():
    width, height = UHD_CANVAS
    pixels = random.Random(259).randbytes(width * height * 3)
    source = io.BytesIO()
    Image.frombytes("RGB", UHD_CANVAS, pixels).save(source, "JPEG", quality=88)
    assert len(source.getvalue()) < MAX_IMAGE_BYTES
    data = prepare_backdrop(source.getvalue(), requested_size=UHD_CANVAS, encoding="jpeg")
    assert data == source.getvalue()
    with Image.open(io.BytesIO(data)) as result:
        assert result.format == "JPEG" and result.size == UHD_CANVAS


async def test_cloud_regeneration_keeps_legacy_on_failure_and_uses_verified_public_metadata(tmp_path, monkeypatch):
    prompts = []
    color = "#6d1322"

    def generated(request):
        prompts.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(solid_png(color)).decode()}]})

    owner = runtime(tmp_path / "ai", generated)
    config = owner.settings.update({"cloud_images": True, "cached_background_limit": 1, "image_model": "gpt-image-1.5"})
    images = SongBackgrounds(owner)
    song = {"song_id": "420", "title": "PRIVATE 云盘标记", "artist": "", "audio_origin": "cloud"}
    public = {"title": "公开曲名", "artist": "公开歌手"}
    identifier = song_identity(song, config.image_model)
    old = solid_png("#213745", LEGACY_CANVAS)
    before = hashlib.sha256(old).hexdigest()
    images.directory.mkdir()
    old_path = images.directory / f"{identifier}.png"
    old_path.write_bytes(old)
    old_path.chmod(0o600)
    record = images.directory / f"{identifier}.json"
    atomic_json(record, {"prompt_version": PROMPT_VERSION, "sha256": before, "public_metadata": public})

    def fail_at_commit(path, value):
        assert path == record and value["sha256"] != before
        assert images.read_image(identifier) == old
        raise OSError("synthetic failure before pointer switch")

    try:
        assert images.read_image(identifier) == old and images.public_metadata_for(identifier) == public
        assert images.song_ref_for(identifier) is None
        with monkeypatch.context() as changes:
            changes.setattr(module, "atomic_json", fail_at_commit)
            assert images.request(song, force=True)["status"] == "preparing"
            await asyncio.gather(*list(images.tasks.values()))
        assert images.cached(identifier)["sha256"] == before and old_path.read_bytes() == old
        assert not list(images.versions.glob("*.jpg"))
        assert images.request(song, force=True)["status"] == "preparing"
        await asyncio.gather(*list(images.tasks.values()))
        after = images.cached(identifier)["sha256"]
        assert after != before and images.read_image(identifier) == prepare_backdrop(solid_png(color))
        assert images.read_image(identifier, expected_sha=before) == old
        assert images.read_image(identifier, expected_sha="invalid") is None
        assert images.public_metadata_for(identifier) == public and images.song_ref_for(identifier) == song
        assert "public_metadata" not in images.cached(identifier) and "song_ref" not in images.cached(identifier)
        color = "#255345"
        assert images.request(song, force=True)["status"] == "preparing"
        await asyncio.gather(*list(images.tasks.values()))
        assert images.read_image(identifier, expected_sha=after) == prepare_backdrop(solid_png("#6d1322"))
        assert images.read_image(identifier, expected_sha=before) is None and not old_path.exists()
        assert len(list(images.versions.glob("*.jpg"))) == 2
        assert len(prompts) == 3 and all("公开曲名" in text and "PRIVATE" not in text for text in prompts)
    finally:
        await images.close()


@pytest.mark.parametrize("parallel", [1, 3])
async def test_generation_respects_configured_parallelism_and_deduplicates_pending_song(tmp_path, parallel):
    calls = 0
    active = 0
    peak = 0
    entered, release = asyncio.Event(), asyncio.Event()
    encoded = base64.b64encode(solid_png("#18344c")).decode()

    async def generated(request):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        if active == parallel:
            entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        finally:
            active -= 1

    owner = runtime(tmp_path / "ai", generated)
    owner.settings.update({"cloud_images": True, "image_model": "gpt-image-1.5"})
    generator = SongBackgrounds(owner, max_parallel=parallel)
    songs = [{"song_id": str(n), "title": "歌曲 " + str(n)} for n in range(1, 5)]
    try:
        for song in songs:
            assert generator.request(song)["status"] == "preparing"
        await asyncio.wait_for(entered.wait(), 5)
        assert generator.request(songs[0], force=True)["status"] == "preparing"
        assert calls == parallel
        release.set()
        await asyncio.gather(*list(generator.tasks.values()))
        assert peak == parallel and calls == 4
        assert all(generator.cached_for(song)["status"] == "ready" for song in songs)
    finally:
        release.set()
        await generator.close()


@pytest.mark.parametrize("invalid", [0, 25, True, 2.5, "3"])
def test_background_parallelism_is_a_positive_bounded_integer(tmp_path, invalid):
    owner = runtime(tmp_path / "ai", lambda request: pytest.fail("合成测试不会请求模型"))
    with pytest.raises(ValueError, match="invalid_background_parallelism"):
        SongBackgrounds(owner, max_parallel=invalid)


def test_context_timeout_is_configurable_with_a_bound(tmp_path):
    owner = runtime(tmp_path / "ai", lambda request: pytest.fail("合成测试不会请求模型"))
    assert SongBackgrounds(owner).context_timeout == 4
    assert SongBackgrounds(owner, context_timeout=22).context_timeout == 22
    for value in (.9, 30.1, True, float("nan")):
        with pytest.raises(ValueError, match="invalid_background_context_timeout"):
            SongBackgrounds(owner, context_timeout=value)


async def test_public_release_brief_is_vetted_and_persists_across_regeneration(tmp_path):
    public = {"title": "公开曲名", "artist": "公开歌手"}
    brief = "公开发行于摇滚专辑。低调的苔绿和雾中的巨石拱门，歌词区保留深色。"
    received, prompts = [], []
    encoded = base64.b64encode(solid_png("#18344c")).decode()

    async def resolve(fields):
        received.append(fields)
        return brief

    def generated(request):
        prompts.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    owner = runtime(tmp_path / "ai", generated)
    owner.settings.update({"cloud_images": True, "image_model": "gpt-image-1.5"})
    generator = SongBackgrounds(owner, context_resolver=resolve)
    song = {"song_id": "808", "title": "私人云盘名", "artist": "", "audio_origin": "cloud"}
    try:
        assert generator.request(song)["reason"] == "private_song_metadata"
        with pytest.raises(ValueError, match="invalid_public_background_metadata"):
            generator.request(song, force=True, public_metadata={**public, "context": "字" * 401})
        pending = generator.request(song, public_metadata=public)
        await asyncio.gather(*list(generator.tasks.values()))
        assert generator.request(song, force=True)["status"] == "preparing"
        await asyncio.gather(*list(generator.tasks.values()))
        assert received == [public]
        assert generator.public_metadata_for(pending["id"]) == {**public, "context": brief}
        assert len(prompts) == 2 and all(brief in text and "私人云盘名" not in text for text in prompts)
    finally:
        await generator.close()


async def test_background_uses_base_public_metadata_when_brief_lookup_times_out(tmp_path):
    attempted, cancelled, prompts = [], [], []
    public = {"title": "公开曲名", "artist": "公开歌手"}
    encoded = base64.b64encode(solid_png("#18344c")).decode()

    async def slow(fields):
        attempted.append(fields)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    def generated(request):
        prompts.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    owner = runtime(tmp_path / "ai", generated)
    owner.settings.update({"cloud_images": True, "image_model": "gpt-image-1.5"})
    generator = SongBackgrounds(owner, context_resolver=slow, context_timeout=1)
    song = {"song_id": "111", "title": "合成曲目"}
    try:
        pending = generator.request(song, public_metadata=public)
        await asyncio.wait_for(asyncio.gather(*list(generator.tasks.values())), 3)
        assert attempted == [public] and cancelled == [True]
        assert len(prompts) == 1 and all(value in prompts[0] for value in public.values())
        assert generator.public_metadata_for(pending["id"]) == public
    finally:
        await generator.close()


def test_process_leases_reserve_insert_capacity_and_recover_uncommitted_image(tmp_path):
    root = tmp_path / "shared"
    runtime_stub = SimpleNamespace(settings=SimpleNamespace(directory=tmp_path))
    generator = SongBackgrounds(runtime_stub, root, max_parallel=4)
    first, second, third = "a" * 64, "b" * 64, "c" * 64
    program = """\
from pathlib import Path
from types import SimpleNamespace
from homeplane.intelligence.backgrounds import SongBackgrounds
import sys
root=Path(sys.argv[1])
app=SongBackgrounds(SimpleNamespace(settings=SimpleNamespace(directory=root)), root, max_parallel=4)
lease=app._claim(sys.argv[2], 2)
print("held", flush=True)
sys.stdin.buffer.read(1)
app._release(lease)
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", program, str(root), first],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    other = None
    try:
        assert select.select([process.stdout], [], [], 5)[0] and process.stdout.readline().strip() == b"held"
        with pytest.raises(EnhancementUnavailable) as duplicate:
            generator._claim(first, 2)
        assert duplicate.value.code == "background_in_progress"
        other = generator._claim(second, 2)
        with pytest.raises(EnhancementUnavailable) as full:
            generator._claim(third, 2)
        assert full.value.code == "background_storage_limit_reached"
        generator.versions.mkdir(mode=0o700)
        data = prepare_backdrop(solid_png("#486278"))
        stale = generator.versions / (first + "." + hashlib.sha256(data).hexdigest() + ".png")
        stale.write_bytes(data)
        stale.chmod(0o600)
        unknown = generator.versions / "unknown-source.png"
        unknown.write_bytes(b"keep")
        process.kill()
        process.communicate(timeout=5)
        recovered = generator._claim(third, 2)
        try:
            assert not stale.exists() and unknown.read_bytes() == b"keep"
            assert not (root / ".reservations" / (first + ".lease")).exists()
        finally:
            generator._release(recovered)
    finally:
        if other is not None:
            generator._release(other)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_tv_background_generation_fence_and_media_credential_scope():
    configured = os.environ.get("FRIDAY_TEST_JAVA_HOME") or os.environ.get("JAVA_HOME")
    java = Path(configured) / "bin/java" if configured else None
    javac = Path(configured) / "bin/javac" if configured else None
    if java is None or not java.is_file() or javac is None or not javac.is_file():
        pytest.skip("需要显式 JDK；不触发 macOS Java 安装器")
    root = Path(__file__).resolve().parents[1]
    source = root / "native/KTVTV/android"
    with build_workspace("jdk-macos-background", "song-background-test", configuration="test") as (
        area,
        record,
    ):
        classes = area / "classes"
        classes.mkdir()
        files = [
            source / "src/local/friday/ktv" / name
            for name in ("SongBackdrop.java", "BackdropImagePolicy.java", "MediaRoute.java", "KtvEndpoint.java")
        ]
        files.append(source / "tests/SongBackdropTest.java")
        command = [str(javac), "--release", "8", "-d", str(classes), *[str(p) for p in files]]
        record["reproduce_command"] = command
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert compiled.returncode == 0, compiled.stderr
        checked = subprocess.run(
            [str(java), "-cp", str(classes), "local.friday.ktv.SongBackdropTest"], capture_output=True, text=True, timeout=10
        )
        assert checked.returncode == 0, checked.stderr
        assert "passed" in checked.stdout


def test_optional_background_failure_does_not_hold_audio_publication(tmp_path):
    worker, queue, remote, report, calls = make_worker(tmp_path)

    def unavailable(song):
        raise ValueError("synthetic_background_settings_unavailable")

    worker.backgrounds = SimpleNamespace(eligible_for_generation=lambda song: True, request_from_thread=unavailable)
    try:
        worker.tick()
        published = next(body for path, body in calls if path == "/agent/manifests")
        assert published["job"]["state"] == "ready" and "background" not in published["job"]
        assert worker.tickets.permits(published["media_ticket"], queue.job)
    finally:
        worker.close()


def test_late_background_changes_only_optional_metadata_and_keeps_audio_ticket(tmp_path):
    worker, queue, remote, report, calls = make_worker(tmp_path)
    value = {"status": "local"}
    worker.backgrounds = SimpleNamespace(eligible_for_generation=lambda song: True, request_from_thread=lambda song: dict(value))
    try:
        worker.tick()
        previous = next(body for path, body in calls if path == "/agent/manifests")
        data = solid_jpeg(UHD_CANVAS, "#23384c")
        value.update(status="ready", sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), encoding="jpeg")
        worker.tick()
        published = [body for path, body in calls if path == "/agent/manifests"]
        assert len(published) == 2 and published[-1]["version"] != previous["version"]
        assert published[-1]["job"]["media"] == previous["job"]["media"]
        assert worker.tickets.permits(previous["media_ticket"], queue.job)
        assert published[-1]["job"]["background"]["encoding"] == "jpeg"
        assert published[-1]["job"]["background"]["url"] == f"/v1/jobs/{queue.job_id}/media/background"
    finally:
        worker.close()


def test_background_lookahead_counts_only_eligible_ready_songs(tmp_path, monkeypatch):
    worker, queue, _, report, calls = make_worker(tmp_path)
    owner = runtime(tmp_path / "ai", lambda request: pytest.fail("synthetic test must not request generation"))
    owner.settings.update({"cloud_images": True})
    images = SongBackgrounds(owner, recording=lambda _: pytest.fail("synthetic test must not read the public catalog"))
    worker.backgrounds = images
    generated, cached = [], []
    monkeypatch.setattr(images, "request_from_thread", lambda song: generated.append(song["song_id"]))
    monkeypatch.setattr(images, "cached_for", lambda song: cached.append(song["song_id"]))
    fixtures = [
        *({"audio_origin": "cloud"} for _ in range(4)),
        {"video": {"status": "ready"}},
        {"simulated": True},
        *({} for _ in range(BACKGROUND_PREFETCH_COUNT + 1)),
    ]
    jobs, mapping = {}, {}
    song_ids = []
    for number, fields in enumerate(fixtures, 100):
        ready = copy.deepcopy(queue.job)
        ready.update(id=str(uuid.uuid4()), song_id=str(number), simulated=False)
        ready.update(fields)
        song_ids.append(ready["song_id"])
        mapping[str(uuid.uuid4())] = ready["id"]
        jobs[ready["id"]] = ready
    worker.mapping = mapping
    report["rooms"][0]["order"] = list(mapping)
    monkeypatch.setattr(queue, "get", lambda job_id: copy.deepcopy(jobs[job_id]))
    try:
        worker.tick()
        assert generated == song_ids[6:-1]
        assert cached == song_ids[:6] + song_ids[-1:]
        assert len([path for path, _ in calls if path == "/agent/manifests"]) == len(fixtures)
    finally:
        worker.close()


@pytest.mark.parametrize("encoding", ["png", "jpeg"])
def test_nas_background_is_scoped_media_and_invalid_optional_image_keeps_audio(store, encoding):
    value, data, instance, party, room = manifest(store)
    original = value["job"]["media"]["original"]["url"]
    picture_data = picture() if encoding == "png" else solid_jpeg(UHD_CANVAS, "#23384c")
    value["job"]["background"] = {
        "status": "ready",
        "encoding": encoding,
        "sha256": hashlib.sha256(picture_data).hexdigest(),
        "bytes": len(picture_data),
        "url": original.replace("/original", "/background"),
    }
    store.coordinator.manifest(value)
    published = store.coordinator.jobs()["jobs"][0]
    assert set(published["media"]) == {"original", "instrumental"} and published["state"] == "ready"
    assert published["background"]["encoding"] == encoding
    assert published["background"]["sources"][0]["header"] == "X-KTV-Media"
    value["version"] = hashlib.sha256(b"background-invalid-test-version").hexdigest()
    value["job"]["background"]["url"] = "https://untrusted.invalid/private"
    store.coordinator.manifest(value)
    published = store.coordinator.jobs()["jobs"][0]
    assert published["state"] == "ready" and "background" not in published
