"""Native reads of generated SIMULATED media, never household NAS or devices."""

import asyncio
import io
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeplane.api import create_app
from homeplane.archive_contracts import ArchivePlaybackRequest, ArchiveTimeRange
from homeplane.archive_playback import RANGE_BUDGET, ArchiveReader, byte_range
from homeplane.archive_worker import run_range, signature

ORIGIN = "http://127.0.0.1:8740"
FIXTURE = Path(__file__).parents[1] / "fixtures/homeplane/archive-simulated.mp4"


def app_at(tmp_path):
    return create_app(
        db_path=tmp_path / "SIMULATED.sqlite",
        settings_path=tmp_path / "settings.json",
        authority_path=tmp_path / "authority.json",
    )


def request_for(archive, kind="ready", start=30, end=40):
    context = archive.context()
    camera = context["cameras"][0]
    span = ArchiveTimeRange(**camera["example_ranges"][kind])
    query = ArchivePlaybackRequest(
        expected_context=context["fingerprint"],
        camera_id=camera["id"],
        media_ref="a" * 64,
        time_range=ArchiveTimeRange(
            start=span.start + timedelta(seconds=start), end=span.start + timedelta(seconds=end)
        ),
    )
    return query


async def actual_request(archive):
    request = request_for(archive)
    report = await archive.resolve(request)
    return request.model_copy(update={"media_ref": report.segments[0].media_ref})


def test_single_ranges_suffix_and_transport_budget():
    assert byte_range("bytes=5-9", 20) == (5, 5, True)
    assert byte_range("bytes=15-", 20) == (15, 5, True)
    assert byte_range("bytes=-4", 20) == (16, 4, True)
    assert byte_range("bytes=0-999999", 20) == (0, 20, True)
    assert byte_range(None, 20) == (0, 20, False)
    assert byte_range("bytes=0-", RANGE_BUDGET + 10) == (0, RANGE_BUDGET, True)
    for bad in (
        "bytes=0-1,3-4",
        "bytes=-0",
        "bytes=20-",
        "bytes=10-5",
        "bytes=0-NaN",
        "bytes=999999999999-",
        "bytes=-",
        "other=0-1",
    ):
        with pytest.raises(ValueError):
            byte_range(bad, 20)
    with pytest.raises(ValueError):
        byte_range(None, RANGE_BUDGET + 1)


def test_worker_range_byte_identity_changed_source_and_no_follow(tmp_path):
    root = tmp_path.resolve()
    p = root / "SIMULATED.mp4"
    shutil.copyfile(FIXTURE, p)
    original = p.read_bytes()
    expected = signature(p.stat())
    out = io.BytesIO()
    assert run_range(str(root), {"path": p.name, "signature": expected, "start": 8, "length": 32}, out)
    header, binary = out.getvalue().split(b"\n", 1)
    assert b'"readable"' in header and binary == original[8:40]
    p.write_bytes(original + b"SIMULATED change")
    out = io.BytesIO()
    assert not run_range(str(root), {"path": p.name, "signature": expected, "start": 0, "length": 32}, out)
    assert out.getvalue() == b'{"status":"unavailable"}\n'
    p.unlink()
    p.symlink_to(FIXTURE)
    out = io.BytesIO()
    assert not run_range(str(root), {"path": p.name, "signature": expected, "start": 0, "length": 32}, out)
    assert out.getvalue() == b'{"status":"unavailable"}\n'
    assert FIXTURE.read_bytes() == original


def test_worker_discards_chunk_when_source_changes_during_read(tmp_path, monkeypatch):
    from homeplane import archive_worker

    root = tmp_path.resolve()
    p = root / "SIMULATED.mp4"
    shutil.copyfile(FIXTURE, p)
    expected = signature(p.stat())
    original_read = archive_worker.os.read

    def changed_read(fd, length):
        chunk = original_read(fd, length)
        with p.open("ab") as out:
            out.write(b"SIMULATED change")
        return chunk

    monkeypatch.setattr(archive_worker.os, "read", changed_read)
    out = io.BytesIO()
    assert not run_range(str(root), {"path": p.name, "signature": expected, "start": 0, "length": 32}, out)
    header, binary = out.getvalue().split(b"\n", 1)
    assert b"readable" in header and binary == b""


def test_disconnected_cleanup_is_shielded_until_owned_child_is_reaped():
    """SIMULATED disconnect during reaping must not leave a reader slot occupied."""

    async def check():
        waiting, release = asyncio.Event(), asyncio.Event()

        class Child:
            returncode = None
            killed = 0

            def kill(self):
                self.killed += 1

            async def wait(self):
                waiting.set()
                await release.wait()
                self.returncode = -9

        class Owner:
            readers = set()

        owner = Owner()
        reader = ArchiveReader(owner, None, 0, 1, True)
        reader.process = Child()
        owner.readers.add(reader)
        disconnect = asyncio.create_task(reader.close())
        await waiting.wait()
        disconnect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await disconnect
        assert reader in owner.readers and not reader.closed
        release.set()
        await reader.close()  # Background cleanup joins the same child reaping task.
        assert reader.process.killed == 1 and reader.closed and not owner.readers

    asyncio.run(check())


def test_close_while_reading_discards_late_bytes_and_drains_before_wait():
    """SIMULATED pipe backpressure and concurrent cancellation, no media reads."""

    async def check():
        class Child:
            returncode = None
            killed = 0
            stdout = asyncio.StreamReader()

            def kill(self):
                self.killed += 1
                self.stdout.feed_data(b"SIMULATED late bytes")
                self.stdout.feed_eof()

            async def wait(self):
                assert self.stdout.at_eof()  # Represents asyncio's paused PIPE transport.
                self.returncode = -9

        class Owner:
            readers = set()

            def valid(self, lease):
                pass

        owner = Owner()
        reader = ArchiveReader(owner, None, 0, 19, True)
        reader.process = Child()
        owner.readers.add(reader)
        chunk = asyncio.create_task(anext(reader.chunks()))
        await asyncio.sleep(0)  # Begin the pending read before the close.
        await asyncio.wait_for(reader.close(), 2)
        with pytest.raises(ValueError, match="archive_playback_expired"):
            await chunk
        assert reader.closed and not owner.readers and reader.process.killed == 1

    asyncio.run(check())


@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="Installed ffprobe required for generated media probe"
)
def test_native_plan_ranges_expiry_revocation_context_and_no_effects(tmp_path):
    app = app_at(tmp_path)
    archive, player = app.state.archive, app.state.archive_player
    tick = [0]
    player.monotonic = lambda: tick[0]

    async def check():
        request = await actual_request(archive)
        plan = await player.prepare(request)
        assert plan.simulated and plan.start_offset_seconds == 30 and plan.end_offset_seconds == 40
        assert plan.previous_keyframe_seconds == 30 and 30 in plan.indexed_keyframes_seconds
        assert plan.codecs == ["h264"] and not plan.audio_track and not plan.decoder_verified_by_server
        assert "root" not in plan.model_dump() and "path" not in plan.model_dump() and not plan.media_copied
        original = FIXTURE.read_bytes()
        reader = await player.read(plan.plan_id, "bytes=8-31")
        assert b"".join([chunk async for chunk in reader.chunks()]) == original[8:32]
        assert not player.readers
        lease = player.leases[plan.plan_id]
        lease.remaining = 1
        with pytest.raises(ValueError, match="byte_budget"):
            await player.read(plan.plan_id, "bytes=0-15")
        lease.remaining = 100
        tick[0] = 301
        with pytest.raises(ValueError, match="expired"):
            await player.read(plan.plan_id, "bytes=0-15")
        player.prune()
        assert not player.leases
        tick[0] = 0
        plan = await player.prepare(request)
        player.revoke(plan.plan_id)
        with pytest.raises(ValueError, match="not_found"):
            await player.read(plan.plan_id, "bytes=0-15")
        plan = await player.prepare(request)
        archive.simulate(archive.context()["fingerprint"], "offline")
        with pytest.raises(ValueError, match="context_changed"):
            await player.read(plan.plan_id, "bytes=0-15")
        assert (
            app.state.core.store.connection.execute("SELECT count(*) FROM light_actions").fetchone()[0] == 0
        )
        assert FIXTURE.read_bytes() == original
        await player.close()
        await archive.close()

    asyncio.run(check())
    app.state.core.actions.close()
    app.state.rules.close()
    app.state.core.store.close()


@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="Installed ffprobe required for generated media probe"
)
def test_abandoned_reader_is_killed_and_source_context_cannot_resume(tmp_path):
    app = app_at(tmp_path)
    archive, player = app.state.archive, app.state.archive_player

    async def check():
        plan = await player.prepare(await actual_request(archive))
        reader = await player.read(plan.plan_id, "bytes=0-")
        pid = reader.process.pid
        assert reader in player.readers and pid > 0
        # A full pipe must not make wait() hang after the owned child exits.
        await asyncio.wait_for(reader.close(), 2)
        assert reader.process.returncode is not None and not player.readers
        reader = await player.read(plan.plan_id, "bytes=0-")
        player.revoke(plan.plan_id)
        with pytest.raises(ValueError, match="expired"):
            await anext(reader.chunks())
        assert reader.process.returncode is not None and not player.readers
        await player.close()
        await archive.close()

    asyncio.run(check())
    app.state.core.actions.close()
    app.state.rules.close()
    app.state.core.store.close()


@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="Installed ffprobe required for generated media probe"
)
def test_api_media_origin_range_metadata_and_cancellation(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        a = app.state.archive
        q = request_for(a)
        report = client.post(
            "/api/archive/resolve",
            json=q.model_dump(mode="json", exclude={"media_ref"}),
            headers={"Origin": ORIGIN},
        ).json()
        q = q.model_copy(update={"media_ref": report["segments"][0]["media_ref"]})
        assert client.post("/api/archive/playback", json=q.model_dump(mode="json")).status_code == 403
        assert (
            client.post(
                "/api/archive/playback",
                json={**q.model_dump(mode="json"), "path": "/etc/passwd"},
                headers={"Origin": ORIGIN},
            ).status_code
            == 422
        )
        plan = client.post(
            "/api/archive/playback", json=q.model_dump(mode="json"), headers={"Origin": ORIGIN}
        ).json()
        url = plan["stream_url"]
        assert client.get(url).status_code == 403
        assert (
            client.get(
                url, headers={"Origin": "https://foreign.example", "Sec-Fetch-Site": "same-origin"}
            ).status_code
            == 403
        )
        response = client.get(url, headers={"Sec-Fetch-Site": "same-origin", "Range": "bytes=8-31"})
        assert response.status_code == 206 and response.content == FIXTURE.read_bytes()[8:32]
        assert response.headers["content-range"] == f"bytes 8-31/{FIXTURE.stat().st_size}"
        assert (
            response.headers["cross-origin-resource-policy"] == "same-origin"
            and response.headers["cache-control"] == "no-store"
        )
        assert client.get(url, headers={"Origin": ORIGIN, "Range": "bytes=0-1,5-6"}).status_code == 416
        assert (
            client.post(
                "/api/archive/playback/cancel", json={"plan_id": plan["plan_id"]}, headers={"Origin": ORIGIN}
            ).status_code
            == 200
        )
        assert client.get(url, headers={"Origin": ORIGIN}).status_code == 404
        assert client.get("/api/snapshot").status_code == 200


def test_unready_simulated_references_do_not_open_media(tmp_path):
    app = app_at(tmp_path)

    async def check():
        for kind in ("pending", "expired"):
            request = request_for(app.state.archive, kind, start=0, end=10)
            with pytest.raises(ValueError, match="not_ready"):
                await app.state.archive_player.prepare(request)
        assert not app.state.archive_player.leases and not app.state.archive_player.readers
        await app.state.archive_player.close()
        await app.state.archive.close()

    asyncio.run(check())
    app.state.core.actions.close()
    app.state.rules.close()
    app.state.core.store.close()
