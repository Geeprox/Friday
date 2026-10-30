"""Actual protected player/child/range lifecycle over a generated test video."""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeplane.archive_contracts import ArchivePlaybackRequest, ArchiveSegmentRef, ArchiveTimeRange
from homeplane.archive_playback import ArchivePlayer
from homeplane.archive_privacy_previews import PrivacyPreviews
from homeplane.archive_worker import run, signature


class Archive:
    def __init__(self, root, path, settings, info):
        self.root, self.path, self.settings, self.info = root, path, settings, info
        self.token = "a" * 64
        self.when = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def clock(self):
        return self.when

    def context(self):
        return {"fingerprint": self.token}

    def _check(self, token):
        if token != self.token:
            raise ValueError("archive_context_changed")

    async def runner(self, root, operation, request):
        return await asyncio.to_thread(run, root, operation, request)

    async def playback_binding(self, request):
        self._check(request.expected_context)
        span = ArchiveTimeRange(start=self.when, end=self.when + timedelta(seconds=2))
        segment = ArchiveSegmentRef(
            media_ref="b" * 64,
            camera_id="SIMULATED-camera",
            time_range=span,
            status="ready",
            reason="SIMULATED-generated",
            time_basis="simulated",
            completion_basis="simulated",
            clock_uncertainty_seconds=0,
            simulated=True,
        )
        return {
            "root": str(self.root),
            "path": self.path.name,
            "signature": signature(self.path.stat()),
            "size_bytes": self.path.stat().st_size,
            "info": self.info,
            "segment": segment,
            "start": 0,
            "end": 2,
        }


@pytest.fixture
def archive(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("Installed ffmpeg/ffprobe required for synthetic codecs")
    root = tmp_path.resolve() / "original"
    root.mkdir()
    path = root / "synthetic.mp4"
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
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    info = run(
        str(root),
        "playback-info",
        {"path": path.name, "ffprobe": shutil.which("ffprobe"), "offset_seconds": 0},
    )
    return Archive(root, path, SimpleNamespace(path=tmp_path / "settings.json"), info)


def request(archive):
    return ArchivePlaybackRequest(
        expected_context=archive.token,
        camera_id="SIMULATED-camera",
        media_ref="b" * 64,
        time_range=ArchiveTimeRange(start=archive.when, end=archive.when + timedelta(seconds=2)),
    )


def renderer(archive, tmp_path):
    return PrivacyPreviews(
        tmp_path / "cache", model=tmp_path / "MISSING-model.onnx", python=Path(sys.executable)
    )


@pytest.mark.asyncio
async def test_default_pipeline_never_serves_original_and_seek_ranges_are_protected(archive, tmp_path):
    privacy = renderer(archive, tmp_path)
    player = ArchivePlayer(archive, privacy=privacy)
    original = archive.path.read_bytes()
    try:
        plan = await player.prepare(request(archive))
        assert plan.privacy_protected and plan.privacy_mode == "local_full_frame_mosaic"
        assert plan.original_modified is False and plan.original_copied is False
        cache = privacy.directory / player.leases[plan.plan_id].binding["path"]
        assert cache.exists() and cache.read_bytes() != original
        reader = await player.read(plan.plan_id, "bytes=0-1023")
        first = b"".join([x async for x in reader.chunks()])
        reader = await player.read(plan.plan_id, "bytes=1024-2047")
        second = b"".join([x async for x in reader.chunks()])
        assert first + second == cache.read_bytes()[:2048]
        assert not player.readers
        player.revoke(plan.plan_id)
        assert not cache.exists()
        assert archive.path.read_bytes() == original
    finally:
        await player.close()
    assert not privacy.files and not privacy.directory.exists()


@pytest.mark.asyncio
async def test_source_changed_after_prepare_cannot_send_a_header_or_raw_fallback(archive, tmp_path):
    player = ArchivePlayer(archive, privacy=renderer(archive, tmp_path))
    try:
        plan = await player.prepare(request(archive))
        archive.path.touch()
        with pytest.raises(ValueError):
            await player.read(plan.plan_id, "bytes=0-100")
        assert not player.readers
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_context_changes_during_render_revoke_result_and_delete_preview(archive, tmp_path):
    privacy = renderer(archive, tmp_path)
    real_prepare = privacy.prepare

    async def changed(binding):
        result = await real_prepare(binding)
        archive.token = "c" * 64
        return result

    privacy.prepare = changed
    player = ArchivePlayer(archive, privacy=privacy)
    try:
        with pytest.raises(ValueError, match="context_changed"):
            await player.prepare(request(archive))
        assert not player.leases and not privacy.files
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_expiry_discards_preview_and_close_during_late_launch_leaves_no_child(archive, tmp_path):
    tick = [0]
    privacy = renderer(archive, tmp_path)
    player = ArchivePlayer(archive, privacy=privacy, monotonic=lambda: tick[0])
    plan = await player.prepare(request(archive))
    cache = privacy.directory / player.leases[plan.plan_id].binding["path"]
    tick[0] = 301
    player.prune()
    assert not cache.exists()
    await player.close()
    appeared = asyncio.Event()
    children = []

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0.04)
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        children.append(process)
        appeared.set()
        return process

    privacy = PrivacyPreviews(
        tmp_path / "late", model=tmp_path / "MISSING", python=Path(sys.executable), launcher=delayed
    )
    player = ArchivePlayer(archive, privacy=privacy)
    task = asyncio.create_task(player.prepare(request(archive)))
    while privacy.launch_task is None:
        await asyncio.sleep(0)
    await player.close()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], BaseException)
    assert not privacy.files and all(p.returncode is not None for p in children)
    assert not player.leases


@pytest.mark.asyncio
async def test_cancel_range_during_late_launch_reaps_owned_child(archive, tmp_path):
    children = []

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0.04)
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        children.append(process)
        return process

    player = ArchivePlayer(archive, privacy=renderer(archive, tmp_path), range_launcher=delayed)
    try:
        plan = await player.prepare(request(archive))
        task = asyncio.create_task(player.read(plan.plan_id, "bytes=0-100"))
        while not player.readers or next(iter(player.readers)).launch_task is None:
            await asyncio.sleep(0)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert children and all(p.returncode is not None for p in children)
        assert not player.readers
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_privacy_log_success_failure_cancel_and_disk_failure_are_redacted(archive, tmp_path):
    from homeplane.runtime_log import RuntimeLog, binding

    journal = RuntimeLog(tmp_path / "logs")

    def failed_disk(_):
        raise OSError("SIMULATED-private-path-and-token")

    journal._append = failed_disk
    privacy = renderer(archive, tmp_path)
    player = ArchivePlayer(archive, privacy=privacy)
    with binding(journal):
        try:
            plan = await player.prepare(request(archive))
            reader = await player.read(plan.plan_id, "bytes=0-100")
            assert len(b"".join([x async for x in reader.chunks()])) == 101
            player.revoke(plan.plan_id)
            bad = request(archive).model_copy(update={"expected_context": "f" * 64})
            with pytest.raises(ValueError, match="context_changed"):
                await player.prepare(bad)
            real = privacy.launcher

            async def delayed(*args, **kwargs):
                await asyncio.sleep(0.04)
                return await real(*args, **kwargs)

            privacy.launcher = delayed
            task = asyncio.create_task(player.prepare(request(archive)))
            while privacy.launch_task is None:
                await asyncio.sleep(0)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
        finally:
            await player.close()
    report = journal.export()
    assert report["disk_available"] is False and report["disk_failures"] > 0
    rows = report["events"]
    assert {row["stage"] for row in rows} >= {
        "archive.privacy",
        "archive.playback",
        "archive.read",
        "archive.stream",
        "archive.cancel",
        "archive.close",
    }
    assert any(row["result"] == "cancelled" for row in rows)
    assert any(row["code"] == "archive_context_changed" for row in rows)
    assert len({row["correlation"] for row in rows}) == 1
    serialized = json.dumps(report)
    for private in (str(archive.path), str(tmp_path), "SIMULATED-private", plan.plan_id, archive.token):
        assert private not in serialized


@pytest.mark.asyncio
async def test_desktop_admission_requires_existing_owned_protected_lease(archive, tmp_path):
    privacy = renderer(archive, tmp_path)
    player = ArchivePlayer(archive, privacy=privacy)
    assert not player.protected_preview("f" * 64)
    try:
        plan = await player.prepare(request(archive))
        lease = player.leases[plan.plan_id]
        assert player.protected_preview(plan.plan_id)
        for missing_flag in ("privacy_protected", "privacy_preview_generated"):
            lease.plan = plan.model_copy(update={missing_flag: False})
            assert not player.protected_preview(plan.plan_id)
        lease.plan = plan
        privacy.release(lease.binding["path"])
        assert not player.protected_preview(plan.plan_id)
        with pytest.raises(ValueError):
            await player.read(plan.plan_id, "bytes=0-100")
        player.revoke(plan.plan_id)
        assert not player.protected_preview(plan.plan_id)
    finally:
        await player.close()


def test_local_model_configuration_is_owner_only_and_never_loads_weights(tmp_path):
    from homeplane.archive_privacy import MODEL_SHA256

    config = tmp_path / "archive-privacy.json"
    model = tmp_path / "intentionally-missing-320n.onnx"
    config.write_text(
        json.dumps(
            {"version": 1, "mode": "local_model", "model_path": str(model), "model_sha256": MODEL_SHA256}
        )
    )
    config.chmod(0o600)
    owner = PrivacyPreviews(tmp_path / "previews", config=config)
    assert owner.model == model and owner.configuration_report()["detector_configured"]
    assert not model.exists() and owner.directory is None and owner.process is None
    assert str(tmp_path) not in json.dumps(owner.configuration_report())
    config.chmod(0o644)
    unsafe = PrivacyPreviews(tmp_path / "unsafe", config=config)
    assert unsafe.model is None and unsafe.configuration == "invalid_configuration"
    config.chmod(0o600)
    config.write_text(json.dumps({"version": 1, "mode": "full_frame"}))
    assert PrivacyPreviews(tmp_path / "full", config=config).model is None
    config.write_text(
        json.dumps({"version": 1, "mode": "local_model", "model_path": str(model), "model_sha256": "0" * 64})
    )
    assert PrivacyPreviews(tmp_path / "bad-hash", config=config).configuration == "invalid_configuration"
    link = tmp_path / "linked.json"
    link.symlink_to(config)
    assert PrivacyPreviews(tmp_path / "linked", config=link).configuration == "invalid_configuration"


@pytest.mark.asyncio
async def test_restart_prunes_abandoned_previews_but_preserves_live_owner(archive, tmp_path):
    parent = tmp_path / "shared-cache"
    live = PrivacyPreviews(parent)
    binding = await archive.playback_binding(request(archive))
    try:
        current = await live.prepare(binding)
        abandoned = parent / ("preview-" + "d" * 32)
        abandoned.mkdir(mode=0o700)
        lock = abandoned / ".owner.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        leftover = abandoned / ("e" * 32 + ".mp4")
        leftover.write_bytes(b"PROTECTED synthetic incomplete derivative")
        leftover.chmod(0o600)
        restarted = PrivacyPreviews(parent)
        try:
            prepared = await restarted.prepare(binding)
            assert not abandoned.exists()
            assert (Path(current["root"]) / current["path"]).exists()
            assert live.owns(current) and restarted.owns(prepared)
            assert not live.owns(prepared)
        finally:
            await restarted.close()
    finally:
        await live.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_still_reaps_renderer_before_partial_cleanup(archive, tmp_path):
    reaping, release = asyncio.Event(), asyncio.Event()

    class Input:
        def write(self, _):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class Child:
        returncode = None
        stdin = Input()
        stdout = asyncio.StreamReader()

        def terminate(self):
            pass

        async def wait(self):
            reaping.set()
            await release.wait()
            self.returncode = -15

    child = Child()

    async def launcher(*_, **kwargs):
        return child

    privacy = PrivacyPreviews(tmp_path / "double-cancel", launcher=launcher)
    player = ArchivePlayer(archive, privacy=privacy)
    task = asyncio.create_task(player.prepare(request(archive)))
    while privacy.process is None:
        await asyncio.sleep(0)
    partial = privacy.directory / privacy.pending_output
    partial.write_bytes(b"SIMULATED protected partial derivative")
    task.cancel()
    await reaping.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and partial.exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None and not partial.exists() and not privacy.files
    await player.close()
