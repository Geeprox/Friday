"""背景媒体仍绑定原歌曲与票据；图片不会改变音频版本或控制权限。"""

import asyncio
import base64
import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from homeplane.intelligence.backgrounds import SongBackgrounds
from homeplane.ktv_service import create_app
from homeplane.ktv_worker import MediaTickets, manifest_version
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
        assert response.status_code == 200 and response.content == picture()
        assert response.headers["content-type"] == "image/png"
        assert client.get(url, headers={"X-KTV-Media": token + "bad"}).status_code == 401
    assert manifest_version(queue.job) == before


def test_tv_background_generation_fence_and_media_credential_scope():
    import pytest

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

    worker.backgrounds = SimpleNamespace(request_from_thread=unavailable)
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
    worker.backgrounds = SimpleNamespace(request_from_thread=lambda song: dict(value))
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
