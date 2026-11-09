"""Build an exact private firmware-input candidate. Never installs or opens audio."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from ktv_build_workspace import ROOT, build_workspace
from prepare_ktv_vendor_audio import PAYLOAD_NAME, PAYLOAD_SHA256, derive_payload


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ndk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/ndk")
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.absolute()
    if (output.exists() or output.is_symlink() or output.parent.resolve() != output.parent
            or not any(output.is_relative_to(ROOT / name) for name in (".artifacts", "data"))):
        parser.error("new_private_delivery_directory_required")
    original = args.vendor_source.absolute()
    if original.resolve() != original or not original.is_file() or original.stat().st_size > 2 * 1024**2:
        parser.error("canonical_exact_vendor_input_required")
    body, derivation = derive_payload(original.read_bytes())
    source = ROOT / "native/KTVTV/strict-cache"
    before = {str(p.relative_to(ROOT)): sha(p) for p in source.iterdir() if p.is_file()}
    for name in ("InlineVoiceKernel.h", "WetDsp.h", "VoiceMixBudget.h"):
        path = ROOT / "native/KTVTV/android/cpp/wet" / name
        before[str(path.relative_to(ROOT))] = sha(path)
    before.update({str(Path(__file__).relative_to(ROOT)): sha(Path(__file__)),
                   "scripts/prepare_ktv_vendor_audio.py": sha(ROOT / "scripts/prepare_ktv_vendor_audio.py"),
                   "scripts/ktv_build_workspace.py": sha(ROOT / "scripts/ktv_build_workspace.py")})
    ndk = args.ndk.resolve()
    if "27.3.13750724" not in (ndk / "source.properties").read_text():
        parser.error("existing_pinned_ndk_r27d_required")
    tools = ndk / "toolchains/llvm/prebuilt/darwin-x86_64/bin"
    compiler = shutil.which("clang++")
    if compiler is None:
        parser.error("existing_host_clang_required")
    compiler_version = subprocess.run([compiler, "--version"], capture_output=True, text=True, check=True).stdout
    host_identity = "clang-macos-" + platform.machine() + "-" + hashlib.sha256(compiler_version.encode()).hexdigest()[:8]
    logs, workspaces = [], []

    def run(command, area):
        env = {**os.environ, "TMPDIR": str(area / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
        (area / "tmp").mkdir(exist_ok=True)
        result = subprocess.run([str(v) for v in command], capture_output=True, text=True,
                                timeout=180, cwd=ROOT, env=env)
        logs.append({"command": [str(v) for v in command], "returncode": result.returncode,
                     "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:]})
        if result.returncode:
            # Keep the bounded failed command only; the build context removes
            # its disposable compiler outputs after checking child handles.
            failure = output.parent / (output.name + "-failure.json")
            failure.write_text(json.dumps({"source_sha256": before, "last_command": logs[-1]}, indent=2) + "\n")
            raise RuntimeError("offline_tool_failed_" + Path(str(command[0])).name)
        return result.stdout

    with build_workspace(host_identity, "ktv-system-module-tests", configuration="asan-ubsan") as (area, record):
        workspaces.append(record)
        record["compiler_version"] = compiler_version.strip()
        payload = area / PAYLOAD_NAME
        payload.write_bytes(body)
        cases = ("SystemModuleTest", "PayloadImageTest", "StrictCacheTest", "InlineProtocolTest", "InlineEffectHookTest", "VendorInlineIntegration")
        for name in cases:
            binary = area / name
            is_hook = name in {"InlineEffectHookTest", "VendorInlineIntegration"}
            extra = ["-DFRIDAY_INLINEFX_TEST", source / "InlineEffectHook.cpp"] if is_hook else []
            if name == "VendorInlineIntegration":
                extra += ["-DFRIDAY_SYSTEM_MODULE_SHIM", "-DFRIDAY_VENDOR_INLINE_INTEGRATION_TEST"]
            test_source = "InlineEffectHookTest" if name == "VendorInlineIntegration" else name
            run([compiler, "-std=c++17", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-pthread",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer", source / (test_source + ".cpp"), *extra, "-o", binary], area)
            run([binary, payload] if name == "PayloadImageTest" else [binary], area)

    with build_workspace("android-ndk27d-arm64-api34", "ktv-system-module", configuration="release") as (area, record):
        workspaces.append(record)
        module = area / "libtlkg.hal.globalmic.so"
        run([tools / "clang++", "--target=aarch64-linux-android34", "-std=c++17", "-shared", "-fPIC", "-O2",
             "-DFRIDAY_SYSTEM_MODULE_SHIM", "-fno-exceptions", "-fno-rtti", "-fvisibility=hidden", "-static-libstdc++",
             "-ffunction-sections", "-fdata-sections", "-Wall", "-Wextra", "-Werror",
             "-Wl,--gc-sections,--exclude-libs,ALL", "-Wl,--version-script=" + str(source / "module-exports.map"),
             "-Wl,-soname,libtlkg.hal.globalmic.so", "-Wl,-z,relro,-z,now,-z,noexecstack,-z,max-page-size=16384",
             source / "SystemModule.cpp", source / "StrictCacheHost.cpp", source / "InlineEffectHook.cpp", "-ldl", "-o", module], area)
        symbols = run([tools / "llvm-readelf", "--dyn-syms", "--wide", module], area)
        exports = [line.split()[-1].split("@")[0] for line in symbols.splitlines()
                   if "GLOBAL" in line and "DEFAULT" in line and " UND " not in line
                   and (" FUNC " in line or " OBJECT " in line)]
        if sorted(exports) != ["TMIS", "_Z20tlkg_moudle_open_jniP12_HalPatchOps"]:
            raise RuntimeError("unexpected_system_module_exports")
        descriptor = [line.split() for line in symbols.splitlines() if line.split() and line.split()[-1].split("@")[0] == "TMIS"]
        if len(descriptor) != 1 or descriptor[0][2] != "24":
            raise RuntimeError("module_descriptor_size_changed")
        if any(name in symbols for name in ("AAudio", "AudioRecord", "pcm_open", "proxy_open", "syncReadDryPcm")):
            raise RuntimeError("module_must_not_open_pcm")
        if any(sha(ROOT / name) != digest for name, digest in before.items()):
            raise RuntimeError("source_changed_during_build")
        output.mkdir(mode=0o700)
        shutil.copyfile(module, output / module.name)
        (output / PAYLOAD_NAME).write_bytes(body)
        (output / "derivation.json").write_text(json.dumps(derivation, indent=2) + "\n")
        result = {"source_sha256": before, "input_sha256": sha(original),
                  "module_sha256": sha(output / module.name), "payload_sha256": PAYLOAD_SHA256,
                  "exports": sorted(exports), "descriptor_bytes": 24, "ops_slots": 11,
                  "full_table_published_after_bridge_ready": True, "initial_failure_is_terminal": True,
                  "host_tests": "ASan+UBSan module lifecycle, real ELF data-only relocation proof, strict-cache, inline protocol and real Source/Core/Hook/kernel integration",
                  "host_test_suites": list(cases),
                  "vendor_fixture_lock_scope": "macOS lifecycle-safe pthread sidecars preserve logical vendor lock ordering; Android mutex layout/concurrency is not emulated",
                  "tool_logs": logs, "build_areas": workspaces,
                  "installed": False, "device_contacted": False, "vendor_code_executed": False,
                  "audio_or_microphone_started": False, "platform_privileges_verified": False,
                  "acoustic_result_verified": False,
                  "retention": "Current firmware candidate; review after supervised platform integration or replacement. Keep original firmware recovery separately."}
    # Context exit updated the shared workspace records with cleanup readback.
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "passed", "output": str(output), "module_sha256": result["module_sha256"],
                      "payload_sha256": PAYLOAD_SHA256, "temporary_payloads_removed": all(v.get("payload_removed") for v in workspaces)}))


if __name__ == "__main__":
    main()
