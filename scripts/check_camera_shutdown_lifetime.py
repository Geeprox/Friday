"""Pinned destructor + synthetic blocked executor; no SDK import/native call.

The shutdown_gc case preserves the independent review's cancellation/GC order.
Worker cases exercise the real worker_probe exit path in this test-owned process.
"""
import argparse
import ast
import asyncio
import atexit
import gc
import hashlib
import io
import json
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from test_homeplane_native_media import SimulatedCamera, SimulatedSDK, immediate  # noqa: E402

from homeplane import native_media_worker as worker  # noqa: E402


class MIoTCameraInstance(SimulatedCamera):
    """Module-level method shape from the exact independent reproduction."""
    def __init__(self, blocking_call, native_tasks, block_start):
        super().__init__()
        self.blocking_call, self.native_tasks, self.block_start = blocking_call, native_tasks, block_start

    async def start_async(self, **options):
        asyncio.get_running_loop().call_later(0,
            lambda: self.native_tasks.append(asyncio.create_task(self.__try_start_async())))

    async def __try_start_async(self):
        did = "SIMULATED-camera"
        await self.callbacks["raw_audio"](did, b"SIMULATED", 1, 1, 0, SimpleNamespace(name="AUDIO_OPUS"))
        await self.callbacks["audio"](did, SimpleNamespace(shape=(320,), dtype="int16"), 1, 0, 1, 1)
        await self.callbacks["raw_video"](did, b"SIMULATED", 1, 1, 0)
        await self.callbacks["video"](did, SimpleNamespace(shape=(120, 160, 3), dtype="uint8"), 1, 0, 1, 1)
        if self.block_start:
            await asyncio.get_running_loop().run_in_executor(None, self.blocking_call)


def pinned_manager_type(source):
    manifest = json.loads((PROJECT / "homeplane/native_media_sdk.json").read_text())
    digest = hashlib.sha256(source).hexdigest()
    if digest != manifest["files"]["camera.py"]:
        raise ValueError("vendor_source_pin_mismatch")
    tree = ast.parse(source)
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MIoTCamera")
    destructor = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "__del__")
    node = ast.ClassDef(name="SyntheticManager", bases=[], keywords=[], body=[destructor], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    scope = {}
    exec(compile(module, "PINNED-DESTRUCTOR-SYNTHETIC-FUNCTIONS", "exec"), scope)
    return scope["SyntheticManager"], digest


async def wait_started(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(.005)


async def run(manager_type, digest, case, marker):
    release, started = threading.Event(), threading.Event()
    observations, weak = [], {}
    native_tasks = []
    explicit_close = []
    block_cleanup = case in {"cleanup_timeout", "cleanup_cancel", "worker_cleanup_timeout"}
    block_start = case != "worker_normal" and not block_cleanup
    began = time.monotonic()

    def blocking_call():
        started.set()
        if case.startswith("worker_"):
            print("SIMULATED-started", file=sys.stderr, flush=True)
        # A fallback makes a broken harness recoverable; worker tests require
        # exit well before this deadline and never release this synthetic call.
        release.wait(10)
        return 0

    def synthetic_deinit():
        item = {"native_call_still_blocked": started.is_set() and not release.is_set()}
        observations.append(item)
        if marker:
            marker.write_text(json.dumps(item))

    async def never(_):
        await asyncio.Event().wait()

    async def probe_scope(config=None):
        client = SimulatedSDK()
        client.instance = MIoTCameraInstance(blocking_call, native_tasks, block_start)
        manager = manager_type()
        manager._lib_miot_camera = SimpleNamespace(miot_camera_set_log_handler=lambda _: None,
            miot_camera_deinit=synthetic_deinit)
        manager._init_done = True
        manager._camera_map = {"SIMULATED-camera": client.instance}
        client.camera = manager
        client.instance._manager = manager
        weak["manager"], weak["instance"] = weakref.ref(manager), weakref.ref(client.instance)

        async def close():
            explicit_close.append(True)
            if block_cleanup:
                # Model destroy_camera_async popping its instance before stop.
                manager._camera_map.clear()
                client.instance = None
                await asyncio.get_running_loop().run_in_executor(None, blocking_call)
            synthetic_deinit()
            manager._init_done = False

        client.deinit_async = close
        sleep = never if case in {"cancel", "repeat_cancel", "native_task_cancel", "worker_cancel"} else immediate
        return await worker.read_camera(worker.NativeMediaConnection(camera_id="SIMULATED-camera"), client,
            codec_quality="SIMULATED-LOW", sleep=sleep)

    if case.startswith("worker_"):
        if marker:
            atexit.register(lambda: marker.with_suffix(".atexit").write_text("SIMULATED-finalized"))
        worker.probe = probe_scope
        original_publish = worker.publish_worker_result

        def simulated_publish(result, output):
            result.simulated = True
            original_publish(result, output)

        worker.publish_worker_result = simulated_publish
        if case == "worker_main_timeout":
            # Finish this setup loop before invoking the actual entry point,
            # which creates its own loop and reserves/silences protocol stdout.
            return True
        output = os.dup(sys.stdout.fileno())
        if case == "worker_broken_pipe":
            os.close(output)
        result = await worker.worker_probe(worker.NativeMediaConnection(camera_id="SIMULATED-camera"), output)
        worker.publish_worker_result(result, output)
        assert not worker._NATIVE_SESSIONS
        return

    try:
        if case in {"cancel", "repeat_cancel", "native_task_cancel", "cleanup_cancel"}:
            probe_task = asyncio.create_task(probe_scope())
            await wait_started(started)
            if case == "native_task_cancel":
                native_tasks[0].cancel()
                report = await asyncio.wait_for(probe_task, 4)
                assert report.status == "failed" and report.native_start_status == "pending"
            else:
                probe_task.cancel()
                if case == "repeat_cancel":
                    await asyncio.sleep(.03)
                    probe_task.cancel()
                try:
                    await asyncio.wait_for(probe_task, 4)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancelled caller unexpectedly returned")
                report = None
            del probe_task
        else:
            report = await asyncio.wait_for(probe_scope(), 4)
            assert report.failure == "worker_timeout"
            if case == "shutdown_gc":
                assert report.native_start_status == "pending"
        assert not observations
        # Exact cancellation order of asyncio.run's _cancel_all_tasks shutdown.
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            del task
        del pending
        native_tasks.clear()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        result = {"simulated": True, "case": case, "sdk_module_imported": False,
            "native_library_loaded": False, "vendor_source_sha256": digest,
            "manager_alive_after_shutdown_cancel_and_gc": weak["manager"]() is not None,
            "instance_alive_after_shutdown_cancel_and_gc": weak["instance"]() is not None,
            "destructor_deinit_calls": observations, "explicit_close_started": bool(explicit_close),
            "native_call_still_blocked": started.is_set() and not release.is_set(),
            "elapsed_seconds": time.monotonic() - began}
        print(json.dumps(result), flush=True)
        assert result["native_call_still_blocked"]
        assert result["manager_alive_after_shutdown_cancel_and_gc"]
        assert result["instance_alive_after_shutdown_cancel_and_gc"]
        assert not observations, "SDK deinitialized while synthetic native call remained blocked"
        assert result["elapsed_seconds"] < 4.5
    finally:
        release.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-source", type=Path, required=True)
    parser.add_argument("--case", choices=["shutdown_gc", "cancel", "repeat_cancel", "native_task_cancel",
        "cleanup_timeout", "cleanup_cancel", "worker_timeout", "worker_main_timeout", "worker_cancel",
        "worker_cleanup_timeout", "worker_broken_pipe", "worker_normal"],
        default="shutdown_gc")
    parser.add_argument("--marker", type=Path)
    args = parser.parse_args()
    manager_type, digest = pinned_manager_type(args.vendor_source.read_bytes())
    run_main = asyncio.run(run(manager_type, digest, args.case, args.marker))
    if run_main:
        request = worker.NativeMediaConnection(camera_id="SIMULATED-camera").model_dump_json().encode()
        sys.stdin = io.TextIOWrapper(io.BytesIO(request))
        worker.main()


if __name__ == "__main__":
    main()
