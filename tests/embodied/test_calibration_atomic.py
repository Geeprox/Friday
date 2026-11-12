"""Real private storage/child/endpoint paths with synthetic images only."""

import asyncio
import base64
import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from homeplane.embodied.calibration_save import CalibrationSaver
from homeplane.embodied.calibration_store import NAME, CalibrationStore, encode, manual_config
from homeplane.embodied.room_engine import DEFAULT, calibration_draft, load_room
from tests.embodied.test_camera_audio import actual


@pytest.fixture
def image():
    pixels = np.random.default_rng(42).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    return cv2.imencode(".jpg", pixels)[1].tobytes()


def config(version=0):
    value = calibration_draft(DEFAULT)
    value.update(mode="relative_calibrated", note=f"synthetic-{version}")
    if version:
        for anchor in value["anchors"]:
            anchor["map"] = [v * 0.8 + 0.1 for v in anchor["map"]]
    return manual_config(value)


def legacy(folder, image):
    (folder / "room-calibration.json").write_text(json.dumps(config()))
    (folder / "room-reference.jpg").write_bytes(image)
    return {p.name: p.read_bytes() for p in folder.iterdir()}


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def test_bundle_readback_is_one_generation_and_legacy_files_are_preserved(tmp_path, image):
    before = legacy(tmp_path, image)
    value = config(1)
    reference = image + b"SYNTHETIC REFERENCE"
    store = CalibrationStore(tmp_path)
    revision = store.save(value, reference)
    loaded, jpeg, digest = store.read()
    assert (loaded, jpeg, digest) == (value, reference, revision)
    room = load_room(tmp_path, b"unused")
    assert room.floor is not None and room.reference == reference
    assert all((tmp_path / name).read_bytes() == data for name, data in before.items())
    assert not list(tmp_path.glob(".room-calibration-*.tmp"))


@pytest.mark.parametrize("phase", ["fsync", "replace"])
def test_storage_failure_keeps_the_entire_previous_generation(tmp_path, image, monkeypatch, phase):
    store = CalibrationStore(tmp_path)
    store.save(config(), image)
    before = (tmp_path / NAME).read_bytes()

    def fail(*_, **__):
        raise OSError("PRIVATE FILESYSTEM DETAIL")

    monkeypatch.setattr(os, phase, fail)
    with pytest.raises(OSError):
        store.save(config(1), image + b"NEW")
    assert (tmp_path / NAME).read_bytes() == before
    assert not list(tmp_path.glob(".room-calibration-*.tmp"))


@pytest.mark.parametrize("fault", ["broken", "checksum", "link", "hardlink"])
def test_a_damaged_or_linked_bundle_never_falls_back_to_old_geometry(tmp_path, image, fault):
    legacy(tmp_path, image)
    path = tmp_path / NAME
    if fault in {"link", "hardlink"}:
        external = tmp_path / "protected"
        external.write_bytes(encode(config(), image)[0])
        external.chmod(0o600)
        if fault == "link":
            path.symlink_to(external)
        else:
            os.link(external, path)
        before = external.read_bytes()
    else:
        path.write_bytes(
            b"{" if fault == "broken" else encode(config(), image)[0].replace(b'"v":2', b'"v":3')
        )
        path.chmod(0o600)
    room = load_room(tmp_path, image)
    assert room.floor is None and room.calibration_error == "calibration_unavailable"
    if fault in {"link", "hardlink"}:
        assert external.read_bytes() == before


def test_an_existing_temporary_file_is_not_removed_when_exclusive_creation_fails(tmp_path, image):
    nonce = "a" * 32
    path = tmp_path / (".room-calibration-" + nonce + ".tmp")
    path.write_bytes(b"UNOWNED")
    path.chmod(0o600)
    with pytest.raises(FileExistsError):
        CalibrationStore(tmp_path).save(config(), image, nonce=nonce)
    assert path.read_bytes() == b"UNOWNED"
    with pytest.raises(ValueError):
        CalibrationStore(tmp_path).cleanup(nonce, encode(config(), image)[0], time.time() - 1)
    assert path.read_bytes() == b"UNOWNED"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "replaced"])
async def test_actual_process_death_leaves_either_complete_old_or_complete_new_pair(tmp_path, image, phase):
    store = CalibrationStore(tmp_path)
    store.save(config(), image)
    value, reference, nonce = config(1), image + b"NEW", "a" * 32
    payload, digest = encode(value, reference)
    began = time.time()
    script = """
import json,os,sys
from pathlib import Path
from homeplane.embodied.calibration_store import CalibrationStore,decode
config,reference,_=decode(sys.stdin.buffer.read())
CalibrationStore(Path(sys.argv[1])).save(config,reference,nonce='a'*32,
    checkpoint=lambda phase:os._exit(9) if phase==sys.argv[2] else None)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-c",
        script,
        str(tmp_path),
        phase,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await process.communicate(payload)
    assert process.returncode == 9, error.decode()
    loaded, jpeg, revision = store.read()
    assert (loaded, jpeg) == ((config(), image) if phase == "prepared" else (value, reference))
    if phase == "replaced":
        assert revision == digest
    store.cleanup(nonce, payload, began)
    assert not list(tmp_path.glob(".room-calibration-*.tmp"))


CHILD = r"""
import ctypes,json,os,signal,sys,time
from pathlib import Path
from homeplane.embodied.calibration_worker import serve,validate
from homeplane.embodied.calibration_store import CalibrationStore
mode=sys.argv[2]
def validator(config,reference):
    if mode=='stall':
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        ctypes.PyDLL(None).sleep(60)
    if mode=='slow':ctypes.PyDLL(None).usleep(180000)
    validate(config,reference)
class Store(CalibrationStore):
    def save(self,*args,**kwargs):
        result=super().save(*args,**kwargs)
        if mode=='unknown_commit':os._exit(0)
        return result
serve(sys.stdin.buffer,sys.stdout.buffer,Path(sys.argv[1]),validator=validator,store_factory=Store)
"""


def saver(folder, invalidate, mode="ok", **kwargs):
    return CalibrationSaver(
        folder, invalidate, command=(sys.executable, "-B", "-c", CHILD, str(folder), mode), **kwargs
    )


@pytest.mark.asyncio
async def test_production_validation_and_commit_are_offloop_and_read_back_identically(tmp_path, image):
    invalidate = Mock()
    service = saver(tmp_path, invalidate, "slow")
    job = asyncio.create_task(service.save(config(1), image))
    ticks = 0
    try:
        while not job.done():
            ticks += 1
            await asyncio.sleep(0.01)
        result = await job
        assert result["saved"] and ticks >= 8
        assert service.owner.process is None and not service.owner.slot
        assert CalibrationStore(tmp_path).read() == (config(1), image, result["revision"])
        invalidate.assert_called_once()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_native_validation_stall_is_killed_and_reaped_without_writing(tmp_path, image):
    invalidate = Mock()
    service = saver(tmp_path, invalidate, "stall", timeout=0.15)
    job = asyncio.create_task(service.save(config(), image))
    try:
        await until(lambda: service.owner is not None and service.owner.process is not None)
        process = service.owner.process
        with pytest.raises(ValueError, match="^calibration_unavailable$"):
            await job
        assert process.returncode is not None and not service.owner.slot
        assert not (tmp_path / NAME).exists()
        invalidate.assert_not_called()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_source_revocation_during_preparation_prevents_commit(tmp_path, image):
    current = [True]
    invalidate = Mock()
    service = saver(tmp_path, invalidate, "slow")
    job = asyncio.create_task(service.save(config(), image, current=lambda: current[0]))
    try:
        await until(lambda: service.owner is not None and service.owner.process is not None)
        current[0] = False
        with pytest.raises(ValueError, match="^calibration_source_changed$"):
            await job
        assert not (tmp_path / NAME).exists()
        invalidate.assert_not_called()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repeated_caller_cancellation_keeps_one_owned_save_to_completion(tmp_path, image):
    invalidate = Mock()
    service = saver(tmp_path, invalidate, "slow")
    caller = asyncio.create_task(service.save(config(), image))
    try:
        await until(lambda: service.owner is not None and service.owner.process is not None)
        caller.cancel()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        with pytest.raises(ValueError, match="^calibration_busy$"):
            await service.save(config(1), image)
        result = await asyncio.shield(service.task)
        assert result["saved"] and CalibrationStore(tmp_path).read()[1] == image
        invalidate.assert_called_once()
        assert not service.owner.slot
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unknown_commit_reloads_runtime_but_does_not_report_saved(tmp_path, image):
    invalidate = Mock()
    service = saver(tmp_path, invalidate, "unknown_commit")
    try:
        with pytest.raises(ValueError, match="^calibration_unavailable$"):
            await service.save(config(), image)
        assert CalibrationStore(tmp_path).read()[:2] == (config(), image)
        invalidate.assert_called_once()
        assert not service.owner.slot
    finally:
        await service.close()


def request(data=None, *, chunks=None, peer="127.0.0.1", headers=None):
    packets = iter(chunks if chunks is not None else [json.dumps(data).encode()])

    async def receive():
        try:
            return dict(type="http.request", body=next(packets), more_body=True)
        except StopIteration:
            return dict(type="http.request", body=b"", more_body=False)

    return Request(
        dict(type="http", method="POST", path="/api/calibration", headers=headers or [], client=(peer, 1)),
        receive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["stale", "missing", "bool", "stream_size", "header_size", "peer", "origin"]
)
async def test_original_endpoint_rejects_invalid_source_or_request_before_starting_a_child(image, fault):
    from homeplane.embodied.calibration_frames import CalibrationFrames
    source = dict(worker="synthetic", generation=1, motion_generation=2)
    frames=CalibrationFrames(clock=lambda:10,wall=lambda:1000)
    frames.publish(config(),image,{},source,1000)
    value = dict(config=dict(config(), _source=frames.open(source)['_source']), reference_jpeg=base64.b64encode(image).decode())
    if fault == "stale":
        value["config"]["_source"]["motion_generation"] = 1
    if fault == "missing":
        value["config"].pop("_source")
    if fault == "bool":
        value["config"]["_source"]["generation"] = True
    service = SimpleNamespace(save=AsyncMock())

    def source_check():
        return source

    scope = dict(
        HTTPException=HTTPException,
        Request=Request,
        json=json,
        base64=base64,
        calibration_saver=service,
        calibration_frames=frames,
        calibration_source=source_check,
        state={"status": "live"},
        camera_planner=None,
    )

    # Use the real saver guard for stale sources without spawning a child.
    async def save(*_, current):
        if not current():
            raise ValueError("calibration_source_changed")
        raise AssertionError("request should have been refused")

    service.save.side_effect = save
    req = request(
        value,
        chunks=[b"x" * 3_000_001] if fault == "stream_size" else None,
        peer="192.0.2.1" if fault == "peer" else "127.0.0.1",
        headers=[(b"content-length", b"3000001")]
        if fault == "header_size"
        else ([(b"origin", b"http://untrusted")] if fault == "origin" else None),
    )
    with pytest.raises(HTTPException) as error:
        await actual("homeplane/embodied/worker.py", "save_calibration", scope)(req)
    assert error.value.status_code in {400, 403, 409, 413}
    service.save.assert_not_called()


@pytest.mark.asyncio
async def test_actual_invalidation_during_registration_drops_the_retired_room_before_track_mutation(tmp_path):
    from tests.embodied.test_capture_stream_recovery import frame_continuation, primary_frame_scope

    scope, published = primary_frame_scope(tmp_path)
    scope["tracks"].invalidate = Mock()
    scope["camera_planner"] = SimpleNamespace(views={"old": "SYNTHETIC"})
    frame = scope["wall"].frames[scope["SELECTED"]["id"]]
    epoch = scope["wall"].epochs[scope["SELECTED"]["id"]]
    old_room = scope["room"]
    invalidate = actual("homeplane/embodied/worker.py", "invalidate_calibration", scope)
    async def interrupted_register(*_,**__):
        invalidate()
        return True
    old_room.register = interrupted_register
    await frame_continuation(scope)()
    assert scope["room"] is None and scope["motion_generation"] == 5 and published == []
    scope["tracks"].update.assert_not_called()
    assert scope["state"]["room"] is None and not scope["state"]["tracking_current"]
    assert scope["camera_planner"].views == {}
    assert scope["SELECTED"]["id"] not in scope["wall"].observations
    assert scope["wall"].frames[scope["SELECTED"]["id"]] == frame
    assert scope["wall"].epochs[scope["SELECTED"]["id"]] == epoch


@pytest.mark.asyncio
async def test_repeated_shutdown_cancellation_reaps_validation_before_the_deadline(tmp_path, image):
    invalidate = Mock()
    service = saver(tmp_path, invalidate, "stall", timeout=20)
    caller = asyncio.create_task(service.save(config(), image))
    try:
        await until(lambda: service.owner is not None and service.owner.process is not None)
        process = service.owner.process
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await asyncio.wait_for(service.close(), 2)
        assert process.returncode is not None and not service.owner.slot
        assert not (tmp_path / NAME).exists()
        invalidate.assert_not_called()
    finally:
        await service.close()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_owned_commit_cleanup_finishes_even_if_callers_and_shutdown_are_cancelled(
    tmp_path, image, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = CalibrationStore.cleanup

    def held(store, *args):
        entered.set()
        release.wait(2)
        return original(store, *args)

    monkeypatch.setattr(CalibrationStore, "cleanup", held)
    invalidate = Mock()
    service = saver(tmp_path, invalidate)
    caller = asyncio.create_task(service.save(config(), image))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not service.owner.slot
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert service.finisher is not None and not service.finisher.done()
        release.set()
        await asyncio.wait_for(service.close(), 2)
        assert service.finisher.done() and service.cleanup_pending is None
        assert CalibrationStore(tmp_path).read()[:2] == (config(), image)
        invalidate.assert_called_once()
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_invalid_geometry_is_rejected_by_native_validation_without_commit(tmp_path, image):
    value = config()
    value["anchors"][0]["image"][0] = True
    invalidate = Mock()
    service = saver(tmp_path, invalidate)
    try:
        with pytest.raises(ValueError, match="^calibration_invalid$"):
            await service.save(value, image)
        assert not (tmp_path / NAME).exists() and not service.owner.slot
        invalidate.assert_not_called()
    finally:
        await service.close()
