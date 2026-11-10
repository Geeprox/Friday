"""Check the owning ARL SDK contract without constructing an app/session.

Run in a fresh selected Python process, with the exact packaged import paths.
The owning adapter defines compatibility; this gate checks that its imported
SDK came from the frozen vendor input and could not start runtime effects.
"""

import _thread
import hashlib
import importlib.abc
import json
import os
import socket
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if len(sys.argv) != 3:
    raise SystemExit("Usage: selected-python -I -B check_packaged_application_sdk.py APP OUTPUT")
APP = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[2]).resolve()
CACHE = (
    Path.home()
    / "Library/Caches/CodexBuilds.noindex"
    / (REPO.name + "-" + hashlib.sha256(str(REPO.resolve()).encode()).hexdigest()[:12])
)
ROOTS = (REPO / ".artifacts", CACHE)
assert APP.name == "Friday.app" and any(APP.is_relative_to(p.resolve()) for p in ROOTS)
assert any(OUTPUT.is_relative_to(p.resolve()) for p in ROOTS) and not OUTPUT.exists()
PRODUCT = APP / "Contents/Resources/product"
sys.path[:0] = [str(PRODUCT / "vendor"), str(PRODUCT / "app")]
blocked_effect_attempts = 0


def forbidden(*args, **kwargs):
    global blocked_effect_attempts
    blocked_effect_attempts += 1
    raise AssertionError("sdk_probe_effect_forbidden")


def audit(event, args):
    if event.startswith(("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostby")):
        forbidden()
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn"}:
        forbidden()
    if event == "ctypes.dlopen" and args[0] not in {None, "/usr/lib/libSystem.B.dylib"}:
        forbidden()
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        if any(
            part in str(path)
            for part in (
                "/Application Support/Friday/",
                "/Application Support/ARL/",
                "/data/credentials/",
                "/Repository/Friday/data/",
            )
        ):
            forbidden()
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 and type(args[2]) is int else 0
        write = bool(
            isinstance(mode, str)
            and any(c in mode for c in "wax+")
            or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if write and path != OUTPUT:
            forbidden()
        if path.suffix.lower() in {".onnx", ".gguf", ".safetensors", ".pt", ".npz"}:
            forbidden()


class NoModelSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {
            "miloco",
            "mlx",
            "torch",
            "transformers",
            "onnxruntime",
            "cv2",
            "sherpa_onnx",
            "miot",
            "whisper",
            "faster_whisper",
            "pyatv",
        }:
            forbidden()
        return None


sys.addaudithook(audit)
sys.meta_path.insert(0, NoModelSDK())
threading.Thread.start = _thread.start_new_thread = forbidden
for name in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, name, forbidden)
socket.getaddrinfo = socket.create_connection = forbidden
result = dict(
    phase="LOCAL_DEVELOPMENT_APP_APPLICATION_SDK_PASS",
    passed=False,
    code="application_sdk_probe_unavailable",
    services_started=0,
    inference_attempted=False,
    registration_attempted=False,
    modules={},
)
try:
    from homeplane import arl_application

    if Path(arl_application.__file__).resolve() != (PRODUCT / "app/homeplane/arl_application.py").resolve():
        raise ValueError("sdk_probe_source_fallback")
    probe = getattr(arl_application, "sdk_compatibility", None)
    if not callable(probe):
        raise ValueError("application_sdk_probe_unavailable")
    compatibility = probe()
    if blocked_effect_attempts:
        raise ValueError("application_sdk_effect_attempted")
    if not isinstance(compatibility, dict) or compatibility.get("compatible") is not True:
        raise ValueError("application_sdk_incompatible")
    modules = {}
    for name, module in list(sys.modules.items()):
        if name == "arl" or name.startswith("arl."):
            source = getattr(module, "__file__", None)
            if not source:
                raise ValueError("application_sdk_source_unknown")
            source = Path(source).resolve()
            if not source.is_relative_to(PRODUCT / "vendor"):
                raise ValueError("application_sdk_not_frozen")
            modules[name] = dict(
                path=str(source.relative_to(PRODUCT)), sha256=hashlib.sha256(source.read_bytes()).hexdigest()
            )
    if not {"arl", "arl.client", "arl.application_session"}.issubset(modules):
        raise ValueError("application_sdk_imports_unverified")
    result.update(
        passed=True,
        code="application_sdk_compatible",
        modules=modules,
        owning_contract="homeplane.arl_application.sdk_compatibility",
    )
except BaseException as error:
    codes = {
        "sdk_probe_source_fallback",
        "application_sdk_probe_unavailable",
        "application_sdk_incompatible",
        "application_sdk_source_unknown",
        "application_sdk_not_frozen",
        "application_sdk_imports_unverified",
        "application_sdk_effect_attempted",
    }
    result.update(
        code=str(error)
        if type(error) is ValueError and str(error) in codes
        else "application_sdk_probe_failed",
        error_type=type(error).__name__,
    )
result["blocked_effect_attempts"] = blocked_effect_attempts
OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
print(
    json.dumps(
        {
            key: result[key]
            for key in ("passed", "code", "services_started", "inference_attempted", "registration_attempted")
        }
    )
)
raise SystemExit(0 if result["passed"] else 1)
