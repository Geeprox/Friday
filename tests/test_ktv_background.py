"""背景媒体仍绑定原歌曲与票据；图片不会改变音频版本或控制权限。"""

import asyncio
import base64
import copy
import hashlib
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from homeplane.intelligence.backgrounds import SongBackgrounds, prepare_backdrop
from homeplane.ktv_service import create_app
from homeplane.ktv_worker import BACKGROUND_PREFETCH_COUNT, MediaTickets, manifest_version
from scripts.ktv_build_workspace import build_workspace
from tests.test_intelligence_journeys import picture
from tests.test_intelligence_providers import runtime
from tests.test_ktv_coordinator import manifest
from tests.test_ktv_coordinator import store as store
from tests.test_ktv_worker import KEY, PreparedQueue, make_worker


async def test_checked_png_is_available_only_with_original_job_ticket_and_does_not_change_audio(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(picture()).decode()}]})

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
        assert response.status_code == 200 and response.content == prepare_backdrop(picture())
        assert response.headers["content-type"] == "image/png"
        assert client.get(url, headers={"X-KTV-Media": token + "bad"}).status_code == 401
    assert manifest_version(queue.job) == before


def test_generated_background_is_widescreen_and_tames_lyric_area_highlights():
    import io

    source = io.BytesIO()
    Image.new("RGB", (768, 512), (240, 240, 240)).save(source, format="PNG")
    with Image.open(io.BytesIO(prepare_backdrop(source.getvalue()))) as delivered:
        assert delivered.size == (1536, 864)
        assert max(delivered.getpixel((768, 432))) <= 80
        assert min(delivered.getpixel((0, 0))) >= 230


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
            for name in ("SongBackdrop.java", "MediaRoute.java", "KtvEndpoint.java")
        ]
        files.append(source / "tests/SongBackdropTest.java")
        command = [str(javac), "--release", "8", "-d", str(classes), *[str(p) for p in files]]
        record["reproduce_command"] = command
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert compiled.returncode == 0, compiled.stderr
        checked = subprocess.run(
            [str(java), "-cp", str(classes), "SongBackdropTest"], capture_output=True, text=True, timeout=10
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
        value.update(status="ready", sha256=hashlib.sha256(picture()).hexdigest(), bytes=len(picture()))
        worker.tick()
        published = [body for path, body in calls if path == "/agent/manifests"]
        assert len(published) == 2 and published[-1]["version"] != previous["version"]
        assert published[-1]["job"]["media"] == previous["job"]["media"]
        assert worker.tickets.permits(previous["media_ticket"], queue.job)
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


def test_nas_background_is_scoped_media_and_invalid_optional_image_keeps_audio(store):
    value, data, instance, party, room = manifest(store)
    original = value["job"]["media"]["original"]["url"]
    value["job"]["background"] = {
        "status": "ready",
        "encoding": "png",
        "sha256": hashlib.sha256(picture()).hexdigest(),
        "bytes": len(picture()),
        "url": original.replace("/original", "/background"),
    }
    store.coordinator.manifest(value)
    published = store.coordinator.jobs()["jobs"][0]
    assert set(published["media"]) == {"original", "instrumental"} and published["state"] == "ready"
    assert published["background"]["sources"][0]["header"] == "X-KTV-Media"
    value["version"] = hashlib.sha256(b"background-invalid-test-version").hexdigest()
    value["job"]["background"]["url"] = "https://untrusted.invalid/private"
    store.coordinator.manifest(value)
    published = store.coordinator.jobs()["jobs"][0]
    assert published["state"] == "ready" and "background" not in published
