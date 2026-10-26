"""Explicitly SIMULATED files/probe metadata; no household NAS or media used."""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from homeplane.archive_contracts import ArchiveCameraEdit, ArchiveResolveRequest, ArchiveTimeRange
from homeplane.archive_index import ArchiveIndex, filename_range
from homeplane.archive_worker import bounded_output, root_handle, run
from homeplane.settings import SettingsService
from homeplane.store import TraceStore

NOW = datetime(2026, 10, 1, 1, tzinfo=UTC)
NAME = "00_20261001080000_20261001080100.mp4"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "SIMULATED-archive"
    folder = root / "SIMULATED-camera"
    folder.mkdir(parents=True)
    (folder / NAME).write_bytes(b"SIMULATED non-media bytes")
    store = TraceStore(tmp_path / "SIMULATED.sqlite")
    settings = SettingsService(tmp_path / "settings.json")
    settings.update({"nas_path": str(root)})
    current = [NOW]
    from homeplane import archive_worker

    monkeypatch.setattr(
        archive_worker,
        "probe",
        lambda *_: {
            "media_metadata_valid": True,
            "audio_track": True,
            "codecs": ["SIMULATED"],
            "media_time_bases": ["1/90000"],
            "duration_seconds": 60,
        },
    )
    calls = []

    async def runner(path, operation, payload=None):
        calls.append(operation)
        return run(path, operation, payload)

    index = ArchiveIndex(store, settings, lambda: False, runner=runner, clock=lambda: current[0])
    yield index, root, folder, store, settings, current, calls
    asyncio.run(index.close())
    store.close()


async def enroll(index, *, timing=True, completion=True, uncertainty=0, delay=None):
    context = await index.scan(index.context()["fingerprint"])
    return index.save_camera(
        ArchiveCameraEdit(
            expected_context=context["fingerprint"],
            folder_ref=context["folders"][0]["folder_ref"],
            name="SIMULATED camera",
            room="SIMULATED room",
            timing_reviewed=timing,
            clock_offset_seconds=0,
            clock_uncertainty_seconds=uncertainty,
            completion_convention="closed_files_operator_reviewed" if completion else "unreviewed",
            arrival_delay_seconds=delay,
        )
    )


def query(index, *, start=0, end=60):
    camera = index.context()["cameras"][0]["id"]
    return ArchiveResolveRequest(
        expected_context=index.context()["fingerprint"],
        camera_id=camera,
        time_range=ArchiveTimeRange(
            start=NOW - timedelta(hours=1) + timedelta(seconds=start),
            end=NOW - timedelta(hours=1) + timedelta(seconds=end),
        ),
    )


def test_filename_basis_signed_offset_and_dst():
    profile = {"timezone": "Asia/Shanghai", "clock_offset_seconds": 120}
    span = filename_range(NAME, profile)
    assert span.start == datetime(2026, 10, 1, tzinfo=UTC) - timedelta(seconds=120)
    assert filename_range("mtime-is-not-recording-time.mp4", profile) is None
    assert filename_range("00_20260230080000_20260230080100.mp4", profile) is None
    assert (
        filename_range(
            "00_20261101013000_20261101013100.mp4",
            {"timezone": "America/New_York", "clock_offset_seconds": 0},
        )
        is None
    )
    assert (
        filename_range(
            "00_20260308023000_20260308023100.mp4",
            {"timezone": "America/New_York", "clock_offset_seconds": 0},
        )
        is None
    )
    assert filename_range("00_20261001080100_20261001080000.mp4", profile) is None


def test_contract_budgets_and_clock_unknown():
    with pytest.raises(ValueError):
        ArchiveCameraEdit(
            expected_context="a" * 64, folder_ref="b" * 64, name="SIMULATED", timing_reviewed=True
        )
    with pytest.raises(ValueError):
        ArchiveCameraEdit(
            expected_context="a" * 64,
            folder_ref="b" * 64,
            name="SIMULATED",
            clock_offset_seconds=float("nan"),
        )
    with pytest.raises(ValueError):
        ArchiveResolveRequest(
            expected_context="a" * 64,
            camera_id="SIMULATED",
            time_range={"start": NOW, "end": NOW + timedelta(hours=1)},
        )
    assert ArchiveTimeRange(start=NOW, end=NOW + timedelta(hours=1)).end > NOW


@pytest.mark.parametrize(
    "timing,completion,expected",
    [(False, False, "unavailable"), (True, False, "pending"), (True, True, "ready")],
)
def test_reviewed_mapping_and_completion_are_separate(setup, timing, completion, expected):
    index, _, folder, _, _, current, _ = setup
    before = (folder / NAME).read_bytes()

    async def check():
        await enroll(index, timing=timing, completion=completion)
        current[0] += timedelta(seconds=3)
        result = await index.resolve(query(index))
        assert result.status == expected
        assert (
            not result.media_copied
            and not result.device_write_attempted
            and not result.model_inference_attempted
        )
        assert result.causal_relation == "unknown"
        assert all(not s.playable_verified for s in result.segments)

    asyncio.run(check())
    assert (folder / NAME).read_bytes() == before


def test_mutation_and_later_absence_never_keep_ready(setup):
    index, _, folder, _, _, current, _ = setup

    async def check():
        await enroll(index)
        current[0] += timedelta(seconds=3)
        first = await index.resolve(query(index))
        assert first.status == "ready"
        (folder / NAME).write_bytes(b"SIMULATED changed")
        assert (await index.resolve(query(index))).status == "pending"
        (folder / NAME).unlink()
        removed = await index.resolve(query(index))
        assert removed.status == "expired"
        assert removed.segments[0].media_ref == first.segments[0].media_ref

    asyncio.run(check())


def test_file_replacement_during_probe_is_detected(setup, monkeypatch):
    index, _, folder, _, _, current, _ = setup
    from homeplane import archive_worker

    def mutate(*_):
        p = folder / NAME
        p.unlink()
        p.write_bytes(b"SIMULATED replacement")
        return {"media_metadata_valid": True, "duration_seconds": 60}

    async def check():
        await enroll(index)
        current[0] += timedelta(seconds=3)
        monkeypatch.setattr(archive_worker, "probe", mutate)
        assert (await index.resolve(query(index))).status == "pending"

    asyncio.run(check())


def test_clock_uncertainty_visible_and_cross_segment_coverage(setup):
    index, _, folder, _, _, current, _ = setup
    second = "00_20261001080100_20261001080200.mp4"
    (folder / second).write_bytes(b"SIMULATED second")

    async def check():
        await enroll(index)
        current[0] += timedelta(seconds=3)
        covered = await index.resolve(query(index, end=120))
        assert covered.status == "ready" and len(covered.segments) == 2 and not covered.gaps
        context = index.context()
        index.save_camera(
            ArchiveCameraEdit(
                expected_context=context["fingerprint"],
                folder_ref=context["folders"][0]["folder_ref"],
                name="SIMULATED",
                timing_reviewed=True,
                clock_offset_seconds=0,
                clock_uncertainty_seconds=2,
                completion_convention="closed_files_operator_reviewed",
            )
        )
        bounded = await index.resolve(query(index, end=120))
        assert bounded.status == "unavailable" and bounded.gaps
        assert all(s.clock_uncertainty_seconds == 2 for s in bounded.segments)
        assert (await index.resolve(query(index, start=5, end=55))).status == "ready"

    asyncio.run(check())


def test_overlap_unrecognized_name_and_stale_scan_do_not_prove_coverage(setup):
    index, _, folder, _, _, current, _ = setup

    async def check():
        await enroll(index)
        current[0] += timedelta(seconds=3)
        (folder / ("01" + NAME[2:])).write_bytes(b"SIMULATED overlapping")
        await index.scan(index.context()["fingerprint"])
        current[0] += timedelta(seconds=3)
        assert (await index.resolve(query(index))).reason == "archive_overlapping_segments_ambiguous"
        (folder / "unknown.mp4").write_bytes(b"SIMULATED unknown time")
        await index.scan(index.context()["fingerprint"])
        assert (await index.resolve(query(index, start=300, end=360))).status == "unavailable"
        current[0] += timedelta(seconds=61)
        assert index.context()["scan"] is None
        assert (await index.resolve(query(index, start=300, end=360))).status == "unavailable"

    asyncio.run(check())


def test_source_change_after_io_rejects_without_indexing(setup):
    index, _, _, store, settings, _, _ = setup

    async def runner(*_):
        settings.update({"nas_path": None})
        return {"status": "readable", "files": [], "folders": [], "scan_complete": True}

    index.runner = runner
    with pytest.raises(ValueError, match="archive_context_changed"):
        asyncio.run(index.scan(index.context()["fingerprint"]))
    assert store.connection.execute("SELECT count(*) FROM archive_files").fetchone()[0] == 0


def test_restart_private_storage_and_expired_context(setup):
    index, _, _, store, settings, current, _ = setup

    async def check():
        await enroll(index)
        request = query(index)
        restarted = ArchiveIndex(
            store, settings, lambda: False, runner=index.runner, clock=lambda: current[0]
        )
        assert restarted.context()["scan"] is None
        assert restarted.context()["cameras"][0]["id"] == request.camera_id
        with pytest.raises(ValueError, match="archive_context_changed"):
            await restarted.resolve(request)
        assert "relative_directory" not in json.dumps(restarted.context())
        await restarted.close()

    asyncio.run(check())


def test_worker_no_follow_and_nested_layout_coverage(tmp_path):
    root = tmp_path / "SIMULATED-root"
    folder = root / "SIMULATED-camera"
    folder.mkdir(parents=True)
    p = folder / NAME
    p.write_bytes(b"SIMULATED")
    (folder / "hidden").symlink_to("/etc")
    (folder / "nested").mkdir()
    report = run(str(root), "scan")
    assert not report["scan_complete"] and len(report["files"]) == 1
    with pytest.raises(OSError):
        root_handle(str(folder / "hidden"))
    result = run(str(root), "inspect", {"paths": ["SIMULATED-camera/hidden/passwd"], "ffprobe": None})
    assert result["files"][0]["present"] is None
    result = run(str(root), "inspect", {"paths": ["../not-allowed"], "ffprobe": None})
    assert result["files"][0]["present"] is None
    assert p.read_bytes() == b"SIMULATED"


def test_worker_output_and_wall_time_bounds():
    with pytest.raises(ValueError, match="output_budget"):
        bounded_output([sys.executable, "-c", 'print("SIMULATED"*10000)'], limit=1024)
    with pytest.raises(TimeoutError):
        bounded_output([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)


def test_simulated_pending_ready_expired_and_offline_do_not_use_worker(tmp_path):
    store = TraceStore(tmp_path / "SIMULATED.sqlite")
    settings = SettingsService(tmp_path / "settings.json")

    async def forbidden(*_):
        raise AssertionError("Simulator must not touch NAS")

    index = ArchiveIndex(store, settings, lambda: True, runner=forbidden, clock=lambda: NOW)

    async def check():
        camera = index.context()["cameras"][0]

        def request(which):
            return ArchiveResolveRequest(
                expected_context=index.context()["fingerprint"],
                camera_id=camera["id"],
                time_range=camera["example_ranges"][which],
            )

        ready = await index.resolve(request("ready"))
        assert ready.status == "ready" and ready.simulated and len(ready.segments) == 2
        assert (await index.resolve(request("pending"))).status == "pending"
        index.simulate(index.context()["fingerprint"], "late-arrives")
        assert (await index.resolve(request("pending"))).status == "ready"
        assert (await index.resolve(request("expired"))).status == "expired"
        index.simulate(index.context()["fingerprint"], "offline")
        assert (await index.resolve(request("ready"))).status == "unavailable"
        await index.close()

    asyncio.run(check())
    store.close()


def test_index_metadata_retention_does_not_delete_source(setup):
    index, _, folder, store, _, current, _ = setup

    async def check():
        await enroll(index)
        current[0] += timedelta(days=8)
        index._prune()

    asyncio.run(check())
    assert store.connection.execute("SELECT count(*) FROM archive_files").fetchone()[0] == 0
    assert store.connection.execute("SELECT count(*) FROM archive_profiles").fetchone()[0] == 1
    assert (folder / NAME).is_file()


def test_camera_time_identity_survives_explicit_directory_rebinding(setup):
    index, root, folder, _, _, current, _ = setup

    async def check():
        await enroll(index)
        current[0] += timedelta(seconds=3)
        first = await index.resolve(query(index))
        new = root / "SIMULATED-moved-camera"
        folder.rename(new)
        context = await index.scan(index.context()["fingerprint"])
        index.save_camera(
            ArchiveCameraEdit(
                expected_context=context["fingerprint"],
                folder_ref=context["folders"][0]["folder_ref"],
                camera_id=first.camera_id,
                name="SIMULATED moved",
                timing_reviewed=True,
                clock_offset_seconds=0,
                clock_uncertainty_seconds=0,
                completion_convention="closed_files_operator_reviewed",
            )
        )
        current[0] += timedelta(seconds=3)
        # Old paths remain diagnostic history and are not part of the new binding.
        result = await index.resolve(query(index))
        assert result.status == "ready" and result.segments[0].media_ref == first.segments[0].media_ref

    asyncio.run(check())


def test_cancelled_worker_is_killed_and_credentials_not_inherited(setup, monkeypatch):
    index, _, _, _, _, _, _ = setup
    original = asyncio.create_subprocess_exec
    captured = []

    async def stalled(*args, **kwargs):
        captured.append(kwargs["env"])
        return await original(sys.executable, "-c", "import time; time.sleep(10)", **kwargs)

    monkeypatch.setenv("HOME_HA_TOKEN", "SIMULATED-token")
    monkeypatch.setenv("OPENAI_API_KEY", "SIMULATED-key")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled)
    index.runner = index._worker

    async def check():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(index.scan(index.context()["fingerprint"]), 0.1)
        assert index.process is None and not index.lock.locked()

    asyncio.run(check())
    assert captured and "HOME_HA_TOKEN" not in captured[0] and "OPENAI_API_KEY" not in captured[0]


def test_actual_installed_probe_on_generated_silent_media_only(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("native media tools absent; hardware/NAS inference not implied")
    root = tmp_path / "SIMULATED-generated-media"
    root.mkdir()
    p = root / "SIMULATED-black-silent.mp4"
    process = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=1:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-movflags",
            "+faststart",
            str(p),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    assert process.returncode == 0
    before = p.read_bytes()
    result = run(str(root), "inspect", {"paths": [p.name], "ffprobe": ffprobe})["files"][0]
    assert result["media_metadata_valid"] and not result["audio_track"]
    assert result["codecs"] == ["h264"] and result["unchanged_during_probe"]
    assert abs(result["duration_seconds"] - 2) < 0.01
    assert p.read_bytes() == before
