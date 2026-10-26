"""Execute one pinned vendor source method with SIMULATED native primitives.

No vendor module/library is imported. No device, login, retained media or network
is read. Run with the existing dev interpreter and an explicit camera.py source.
"""
import argparse
import ast
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from test_homeplane_native_media import SimulatedCamera, SimulatedSDK  # noqa: E402
from test_homeplane_native_media_lifecycle import MIoTCameraError  # noqa: E402

from homeplane.media_contracts import NativeMediaConnection  # noqa: E402
from homeplane.native_media_worker import read_camera  # noqa: E402


class NullLogger:
    def info(self, *args):
        pass

    def error(self, *args):
        pass


class UInt8Stub:
    def __mul__(self, _):
        return lambda *args: args


def simulated_camera_type(source):
    digest = hashlib.sha256(source).hexdigest()
    manifest = json.loads((PROJECT / "homeplane/native_media_sdk.json").read_text())
    if digest != manifest["files"]["camera.py"]:
        raise ValueError("vendor_source_pin_mismatch")
    tree = ast.parse(source)
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MIoTCameraInstance")
    method = next(n for n in klass.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "__try_start_async")
    scope = {"SimulatedCamera": SimulatedCamera, "_LOGGER": NullLogger(), "MIoTCameraError": MIoTCameraError,
        "c_uint8": UInt8Stub(), "_MIoTCameraConfigC": lambda *args: args, "byref": lambda arg: arg}
    node = ast.ClassDef(name="MIoTCameraInstance", bases=[ast.Name(id="SimulatedCamera", ctx=ast.Load())],
        keywords=[], body=[method], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, "OFFICIAL-SOURCE-METHOD-SIMULATED", "exec"), scope)
    return scope["MIoTCameraInstance"], digest


async def run(camera_type, digest):
    client, loop = SimulatedSDK(), asyncio.get_running_loop()
    camera = camera_type()
    client.instance = camera
    camera._main_loop = loop
    camera._did = "SIMULATED-camera"
    camera.camera_info = SimpleNamespace(did=camera._did, channel_count=1)
    camera._video_qualities = [1, 0]
    camera._enable_audio, camera._enable_reconnect, camera._pin_code = True, False, None
    camera._c_instance, camera.tasks = object(), []

    def start_native(*args):
        async def emit():
            await camera.callbacks["raw_audio"](camera._did, b"SIMULATED", 1, 1, 0,
                SimpleNamespace(name="AUDIO_OPUS"))
            await camera.callbacks["audio"](camera._did, SimpleNamespace(shape=(320,), dtype="int16"),
                1, 0, 1, 1)
        asyncio.run_coroutine_threadsafe(emit(), loop).result(timeout=2)
        return -14  # Synthetic C return, never a hardware reading.

    camera._lib_miot_camera = SimpleNamespace(miot_camera_start=start_native)
    camera._MIoTCameraInstance__reset_try_start_timeout = lambda: None

    async def start(**options):
        camera.starts.append(options)
        camera._reconnect_timer = loop.call_later(0,
            lambda: camera.tasks.append(loop.create_task(camera._MIoTCameraInstance__try_start_async())))

    camera.start_async = start

    async def sampling(_):
        await asyncio.sleep(.05)

    report = await read_camera(NativeMediaConnection(camera_id=camera._did), client,
        codec_quality="SIMULATED-LOW", sleep=sampling)
    report.simulated = True
    assert report.failure == "stream_error" and report.failure_phase == "connect"
    assert report.native_start_error_code == -14
    assert report.audio.decoded_frames == 1 and report.video.decoded_frames == 0
    assert not any(name.startswith("miot.") for name in sys.modules)
    return {"simulated": True, "sdk_module_imported": False, "native_library_loaded": False,
        "official_method_source_sha256": digest, "method": "MIoTCameraInstance.__try_start_async",
        "fake_native_return": -14, "report": report.model_dump()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    camera_type, digest = simulated_camera_type(args.vendor_source.read_bytes())
    result = asyncio.run(run(camera_type, digest))
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
