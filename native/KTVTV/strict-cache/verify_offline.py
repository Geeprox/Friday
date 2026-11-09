"""Compile/run synthetic tests and cross-compile the bridge; never loads vendor DSOs."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main():
    root = Path(__file__).resolve().parents[3]
    source = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / ".artifacts/ktv-cache-only-review")
    parser.add_argument("--ndk", type=Path, default=root / ".artifacts/embodied-toolchain/ndk")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(root / ".artifacts"):
        parser.error("test_outputs_must_be_under_artifacts")
    output.mkdir(parents=True, exist_ok=True)
    log = []

    def run(command):
        result = subprocess.run([str(p) for p in command], check=True, text=True, capture_output=True, timeout=120)
        log.append(result.stdout + result.stderr)
        return result.stdout

    host_clang = shutil.which("clang++")
    if not host_clang:
        raise SystemExit("existing_clang_required")
    test = output / "strict-cache-test-asan"
    run([host_clang, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pthread", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer", source / "StrictCacheTest.cpp", "-o", test])
    run([test])
    ndk = args.ndk.resolve()
    if "27.3.13750724" not in (ndk / "source.properties").read_text():
        raise SystemExit("pinned_ndk_r27d_required")
    tools = ndk / "toolchains/llvm/prebuilt/darwin-x86_64/bin"
    library = output / "libfriday_strict_cache.so"
    run([tools / "clang++", "--target=aarch64-linux-android34", "-std=c++17", "-shared", "-fPIC", "-O2",
         "-fno-exceptions", "-fno-rtti", "-fvisibility=hidden", "-static-libstdc++", "-ffunction-sections", "-fdata-sections",
         "-Wall", "-Wextra", "-Werror", "-Wl,--gc-sections,--exclude-libs,ALL",
         "-Wl,--version-script=" + str(source / "exports.map"), "-Wl,-z,relro,-z,now,-z,noexecstack,-z,max-page-size=16384",
         source / "StrictCacheHost.cpp", "-ldl", "-o", library])
    probe = output / "client-header.c"
    probe.write_text('#include "StrictCacheClient.h"\nint header_probe(void){FridayCacheClient c=friday_cache_client_init();return c.fd;}\n')
    for target in ("aarch64-linux-android34", "armv7a-linux-androideabi34"):
        run([tools / "clang", "--target=" + target, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", "-I", source, probe])
    symbols = run([tools / "llvm-readelf", "--dyn-syms", "--wide", library])
    exports = [line.split()[-1].split("@")[0] for line in symbols.splitlines()
               if "GLOBAL" in line and "DEFAULT" in line and " UND " not in line and " FUNC " in line]
    if sorted(exports) != ["friday_strict_cache_start", "friday_strict_cache_stop"]:
        raise SystemExit("unexpected_bridge_exports")
    if any(name in symbols for name in ("AAudio", "AudioRecord", "proxy_open", "proxy_read", "pcmDevOpen", "syncReadDryPcm", "pcm_open")):
        raise SystemExit("forbidden_capture_import")
    report = {
        "scope": "offline synthetic memory/socket tests and Android cross compilation only",
        "asan_ubsan": "passed", "synthetic_reads": 100000,
        "android_host_abi": "arm64-v8a API34", "client_c_headers": ["arm64-v8a", "armeabi-v7a"],
        "exports": sorted(exports), "device_nas_audio_model_actions": False,
        "vendor_dso_executed": False, "deployed_or_system_permissions_verified": False,
        "expected_mapped_rx_sha256": "618a2f2d4a60b17d0d3e52167d564f8064a2129314a97efffbce5273f30c4ef8",
        "instance_cookie": "HELLO sequence -> independent client instance_cookie; version-slot CAS/restore and borrowed lifetime synthetic tests passed",
        "framework_audio_manager_route_match_executed": False,
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.iterdir()) if p.is_file()},
        "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        "limitations": ["macOS socket-packet tests use AF_UNIX DGRAM when SEQPACKET is unavailable; deadline backpressure uses a stream pair",
                        "real Android SO_PEERCRED/abstract socket, audioserver loading, SELinux, secure config and mapped vendor binding are not executed",
                        "vendor object/trylock layouts are checked with synthetic memory/lock substitutes, not real hardware",
                        "cache freshness checks do not establish physical capture age, acoustic latency or reverb quality"],
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "verification.log").write_text("\n".join(log))
    print(json.dumps({"status": "passed", "receipt": str(output / "verification.json"), "library_sha256": report["library_sha256"]}))


if __name__ == "__main__":
    main()
