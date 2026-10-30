"""Production recording routes over generated media; no household NAS or footage."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app

ORIGIN = "http://127.0.0.1:8740"


@pytest.fixture
def session(tmp_path,request):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("Installed ffmpeg/ffprobe required for synthetic codecs")
    root = tmp_path.resolve() / "SIMULATED-recordings"
    folder = root / "XiaomiCamera_00_001122334455"
    folder.mkdir(parents=True)
    source = folder / "00_20260101000000_20260101000002.mp4"
    options = getattr(request, "param", None)
    duration = options["duration"] if isinstance(options, dict) else 2
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"] if isinstance(options, dict) else []
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=8",
            *audio,
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    if isinstance(options, int):
        with source.open("r+b") as padded:padded.truncate(request.param)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    app = create_app(db_path=tmp_path / "SIMULATED.sqlite", settings_path=tmp_path / "settings.json")

    async def source_root():
        return str(root)

    app.state.archive._root = source_root
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client, app, source
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    owner = app.state.home_experience.recordings
    assert owner.closed and not owner.leases and not owner.readers and not owner.privacy.files
    if owner.privacy.directory is not None:
        assert not owner.privacy.directory.exists()


def listed(client):
    report = client.get("/api/home/recordings").json()
    assert report["connected"] and len(report["files"]) == 1
    return report["files"][0]["id"]


def test_formal_routes_only_serve_protected_preview_and_stop_revokes_it(session):
    client, app, source = session
    response = client.post("/api/home/recordings/play", json={"id": listed(client)})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["privacy_protected"] and plan["privacy_mode"] == "local_full_frame_mosaic"
    assert not plan["original_modified"] and not plan["original_copied"]
    owner = app.state.home_experience.recordings
    lease = owner.leases[plan["token"]]
    preview = Path(lease.binding["root"]) / lease.binding["path"]
    protected = preview.read_bytes()
    assert protected != source.read_bytes()
    response = client.get(plan["url"], headers={"Range": "bytes=64-1023", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 206 and response.content == protected[64:1024]
    assert response.headers["content-range"] == f"bytes 64-1023/{len(protected)}"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert client.get(plan["url"], headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/home/recordings/stop", json={"token": plan["token"]}).status_code == 200
    assert not preview.exists() and not owner.leases
    assert client.get(plan["url"]).status_code == 416
    assert client.get("/api/snapshot").status_code == 200


def test_changed_original_or_invalid_request_cannot_fall_back_to_raw(session):
    client, app, source = session
    ref = listed(client)
    assert client.post("/api/home/recordings/play", json={"id": ref, "path": str(source)}).status_code == 422
    plan = client.post("/api/home/recordings/play", json={"id": ref}).json()
    source.touch()
    response = client.get(plan["url"], headers={"Range": "bytes=0-100"})
    assert response.status_code == 416 and b"ftyp" not in response.content
    assert not app.state.home_experience.recordings.readers


def test_shutdown_removes_preview_and_owned_readers(session):
    client, app, source = session
    plan = client.post("/api/home/recordings/play", json={"id": listed(client)}).json()
    owner = app.state.home_experience.recordings
    assert owner.protected_preview(plan["token"])
    assert owner.leases and owner.privacy.files
    # Fixture closes the actual app lifespan; post-close state is checked below
    # by a finalizer registered before this test returns.
    directory = owner.privacy.directory
    assert directory.exists()


@pytest.mark.parametrize('session',[128*1024*1024],indirect=True)
def test_standard_128_mib_camera_file_prepares_bounded_protected_output(session):
    client,app,source=session
    assert source.stat().st_size==128*1024*1024
    response=client.post('/api/home/recordings/play',json={'id':listed(client)})
    assert response.status_code==200,response.text
    plan=response.json();assert plan['privacy_protected'] and not plan['original_modified']
    lease=app.state.home_experience.recordings.leases[plan['token']]
    assert lease.binding['size_bytes']<=64*1024*1024


@pytest.mark.parametrize("session", [{"duration": 45}], indirect=True)
def test_segmented_recording_seek_preserves_timeline_audio_and_protection(session):
    client, app, source = session
    ref = listed(client)
    first = client.post("/api/home/recordings/play", json={"id": ref}).json()
    assert first["source_duration_seconds"] == pytest.approx(45, abs=.1)
    assert first["duration_seconds"] == pytest.approx(30, abs=.15)
    assert first["media_start_seconds"] == 0
    second = client.post("/api/home/recordings/play", json={"id": ref, "offset_seconds": 30}).json()
    assert second["duration_seconds"] == pytest.approx(15, abs=.15)
    assert second["media_start_seconds"] == 30
    for plan in (first, second):
        assert plan["privacy_protected"] and plan["privacy_preview_generated"]
        assert client.get(plan["url"], headers={"Range": "bytes=0-1023"}).status_code == 206
        lease = app.state.home_experience.recordings.leases[plan["token"]]
        import av
        with av.open(str(Path(lease.binding["root"]) / lease.binding["path"])) as media:
            assert len(media.streams.audio) == 1
            frame = next(media.decode(video=0))
            assert 0 <= float(frame.pts * frame.time_base) < .15
    assert client.post("/api/home/recordings/stop", json={"token": first["token"]}).status_code == 200
    sought = client.post("/api/home/recordings/play", json={"id": ref, "offset_seconds": 15.25})
    assert sought.status_code == 200, sought.text
    assert sought.json()["media_start_seconds"] == 15.25
    assert sought.json()["duration_seconds"] == pytest.approx(29.75, abs=.15)
    assert client.post("/api/home/recordings/play", json={"id": ref, "offset_seconds": 45}).status_code == 409


@pytest.mark.parametrize("offset", [True, -1, 600, "30"])
def test_recording_offset_rejects_ambiguous_or_unbounded_input(session, offset):
    client, app, source = session
    response = client.post("/api/home/recordings/play", json={"id": listed(client), "offset_seconds": offset})
    assert response.status_code == 422
    assert not app.state.home_experience.recordings.leases


def test_installed_gui_path_can_prepare_existing_recording(session,monkeypatch):
    client,app,source=session
    monkeypatch.setenv("PATH","/usr/bin:/bin:/usr/sbin:/sbin")
    response=client.post("/api/home/recordings/play",json={"id":listed(client)})
    assert response.status_code==200,response.text
    assert response.json()["privacy_protected"]
