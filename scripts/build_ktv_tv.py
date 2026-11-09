"""Build the independent KTV APK with an existing Android SDK/JDK; never installs or starts it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
# Set this before local imports so direct invocation creates no source-tree pyc.
from build_ktv_audio_guard import source_snapshot  # noqa: E402
from ktv_build_tools import (  # noqa: E402
    build_environment,
    isolated_command,
    reuse_oboe_object,
    toolchain_identity,
    workspace_receipt,
)
from ktv_build_workspace import build_workspace  # noqa: E402
from prepare_ktv_coordinator import (  # noqa: E402
    private_ipv4,
    private_path,
    profile_fingerprint,
    read_nas_connection,
    read_regular,
)

ROOT = Path(__file__).resolve().parents[1]


def connection_profile(connection_config=None, nas_connection_config=None, host=None, *, root=ROOT):
    if connection_config and nas_connection_config:
        raise ValueError("one_private_connection_role_required")
    if nas_connection_config:
        if host:
            raise ValueError("nas_origin_comes_from_private_profile")
        return read_nas_connection(nas_connection_config, root)
    if connection_config:
        raw = read_regular(private_path(connection_config, root))
        address = private_ipv4(host or "")
        try:
            key = json.loads(raw)["token"]
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ValueError("invalid_private_connection") from None
        origin = "http://" + address + ":8769"
        return {"role": "mac", "origin": origin, "token": key,
                "fingerprint": profile_fingerprint("mac", origin, key),
                "input_sha256": hashlib.sha256(raw).hexdigest()}
    if host:
        raise ValueError("private_connection_config_required_for_host")
    return None


def generated_setup_defaults(profile):
    if profile is None:
        raise ValueError("private_profile_required")
    values = {"ORIGIN": profile["origin"], "KEY": profile["token"],
              "PROFILE": profile["fingerprint"], "ROLE": profile["role"]}
    return ('package local.friday.ktv; public final class SetupDefaults { '
            + " ".join("public static final String " + key + "=" + json.dumps(value) + ";" for key, value in values.items())
            + ' private SetupDefaults(){} }\n')


def existing_nas_signing(output, profile):
    if profile is None or profile["role"] != "nas":
        return
    for name in ("development-signing.p12", "development-signing.password"):
        path = output / name
        if (path.resolve() != path or not path.is_file() or path.stat().st_mode & 0o077
                or path.stat().st_size == 0):
            raise ValueError("existing_apk_signing_identity_required_for_nas")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/android")
    parser.add_argument("--java", type=Path, default=ROOT / ".artifacts/embodied-toolchain/jdk")
    connections = parser.add_mutually_exclusive_group()
    connections.add_argument("--connection-config", type=Path, help="Optional private Mac service config; never included in portable builds")
    connections.add_argument("--nas-connection-config", type=Path, help="Private 0600 NAS TV profile; applies this explicit profile once")
    parser.add_argument("--host", help="Explicit private Mac IPv4 for the optional household build")
    parser.add_argument("--native-audio", "--native-probe", dest="native_probe", action="store_true", default=True,
                        help="Include callback-backed AAudio I/O (default) and bounded diagnostics")
    parser.add_argument("--no-native-audio", dest="native_probe", action="store_false", help="Build the Java compatibility backend only")
    parser.add_argument("--ndk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/ndk")
    parser.add_argument("--oboe", type=Path, default=ROOT / ".artifacts/embodied-toolchain/oboe-1.11.0")
    args = parser.parse_args()
    profile = connection_profile(args.connection_config, args.nas_connection_config, args.host, root=ROOT)
    source = ROOT / "native/KTVTV/android"
    before = source_snapshot(source)
    bridge_source = ROOT / "native/KTVTV/strict-cache"
    bridge_before = source_snapshot(bridge_source)
    manifest = ET.parse(source / "AndroidManifest.xml").getroot()
    version = manifest.attrib["{http://schemas.android.com/apk/res/android}versionName"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise ValueError("invalid_version")
    output = ROOT / ".artifacts/ktv-tv"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing_nas_signing(output, profile)
    signing_reused = (output / "development-signing.p12").is_file()
    with build_workspace(toolchain_identity(args.java, ndk=args.ndk if args.native_probe else None), "ktv-tv-apk", configuration="ktv-system-audio") as (build, workspace):
        environment = build_environment({**os.environ, "JAVA_HOME": str(args.java.absolute())}, build)
        environment["PATH"] = str(args.java.absolute() / "bin") + os.pathsep + os.environ.get("PATH", "")

        def run(*command):
            result = subprocess.run(isolated_command(environment, command), env=environment, capture_output=True, text=True,
                                    timeout=180)
            if result.returncode:
                (build / "compiler-error.txt").write_text(result.stderr[-16000:])
                workspace["failed_tool"] = Path(str(command[0])).name
                workspace["failed_returncode"] = result.returncode
                raise RuntimeError("build_failed_" + Path(str(command[0])).name)
            return result.stdout

        def tool(name):
            matches = list(args.sdk.rglob(name))
            if len(matches) != 1:
                raise ValueError("existing_sdk_tool_required_" + name)
            return matches[0]

        android, aapt, d8, align, signer = [tool(n) for n in ("android.jar", "aapt2", "d8", "zipalign", "apksigner")]
        classes, dex = build / "classes", build / "dex"
        classes.mkdir()
        dex.mkdir()
        sources = sorted((source / "src").rglob("*.java"))
        if profile is not None:
            generated = build / "SetupDefaults.java"
            generated.write_text(generated_setup_defaults(profile))
            generated.chmod(0o600)
            sources = [generated if p.name == "SetupDefaults.java" else p for p in sources]
        run(args.java / "bin/javac", "--release", "8", "-classpath", android, "-d", classes, *sources)
        run(d8, "--min-api", "26", "--lib", android, "--output", dex, *sorted(classes.rglob("*.class")))
        resource_args = []
        resources = source / "res"
        if resources.is_dir():
            for resource in resources.rglob("*"):
                if resource.is_symlink() or (resource.is_file() and resource.stat().st_size > 8 * 1024 * 1024):
                    raise ValueError("unsafe_resource")
            compiled = build / "compiled-resources.zip"
            run(aapt, "compile", "--dir", resources, "-o", compiled)
            resource_args = ["-R", compiled]
        run(aapt, "link", "-o", build / "resources.apk", "-I", android,
            "--manifest", source / "AndroidManifest.xml", *resource_args)
        libraries = []
        native_source = source / "cpp/audio_probe.cpp"
        if args.native_probe:
            properties = args.ndk / "source.properties"
            if not properties.is_file() or "27.3.13750724" not in properties.read_text():
                raise ValueError("pinned_ndk_r27d_required")
            clang = args.ndk / "toolchains/llvm/prebuilt/darwin-x86_64/bin/clang++"
            dependency = json.loads((ROOT / "native/KTVTV/dependencies/oboe.json").read_text())
            for name, expected in dependency["files"].items():
                file = args.oboe / name
                if (not file.is_file() or file.is_symlink() or file.resolve() != file.absolute()
                        or hashlib.sha256(file.read_bytes()).hexdigest() != expected):
                    raise ValueError("pinned_oboe_required_run_setup_ktv_audio")
            for abi, target in (("arm64-v8a", "aarch64-linux-android29"), ("armeabi-v7a", "armv7a-linux-androideabi29")):
                library = build / (abi + "-libktv_probe.so")
                oboe_flags = ["--target=" + target, "-O2", "-DNDEBUG", "-fPIC", "-std=c++17", "-fno-exceptions", "-fno-rtti",
                              "-DOBOE_ENABLE_LOGGING=0", "-DDO_NOT_DEFINE_OPENSL_ES_CONSTANTS=1"]
                cache = args.oboe.parent / "oboe-build" / (abi + "-" + hashlib.sha256(
                    json.dumps([dependency, oboe_flags, "ndk-r27d"], sort_keys=True).encode()).hexdigest()[:16])
                # Existing shared objects are read-only inputs. Never create,
                # repair, or overwrite the old shared cache from this build.
                objects = build / "oboe-objects" / abi
                objects.mkdir(parents=True)
                def compile_oboe(name):
                    filename = name.replace("/", "_") + ".o"
                    obj = objects / filename
                    reused = reuse_oboe_object(cache / filename, obj)
                    if not reused:
                        run(clang, *oboe_flags,
                            "-I" + str(args.oboe / "include"), "-I" + str(args.oboe / "src"),
                            "-c", args.oboe / name, "-o", obj)
                    return obj, reused
                with ThreadPoolExecutor(max_workers=4) as workers:
                    compiled_objects = list(workers.map(compile_oboe, dependency["sources"]))
                oboe_objects = [obj for obj, _ in compiled_objects]
                workspace.setdefault("oboe_objects", {})[abi] = {
                    "reused": sum(reused for _, reused in compiled_objects),
                    "compiled": sum(not reused for _, reused in compiled_objects), "shared_cache_written": False}
                run(clang, "--target=" + target, "-shared", "-O2", "-fPIC", "-std=c++17", "-fno-exceptions", "-fno-rtti",
                    "-static-libstdc++", "-Wl,-z,max-page-size=16384", "-I" + str(args.oboe / "include"),
                    native_source, source / "cpp/audio_io.cpp", source / "cpp/OboeAudioMonitor.cpp",
                    source / "cpp/NativeDuplexIO.cpp", *oboe_objects,
                    "-laaudio", "-lOpenSLES", "-llog", "-ldl", "-o", library)
                libraries.append((abi, "libktv_probe.so", library))
                # Optional wet effects run in their own process. Keeping this library
                # separate prevents its JNI/DSP lifetime from owning the dry monitor.
                wet_library = build / (abi + "-libfriday_wet.so")
                wet_target = "aarch64-linux-android34" if abi == "arm64-v8a" else "armv7a-linux-androideabi34"
                run(clang, "--target=" + wet_target, "-shared", "-O2", "-fPIC", "-std=c++17",
                    "-fno-exceptions", "-fno-rtti", "-static-libstdc++", "-Wl,-z,max-page-size=16384",
                    source / "cpp/wet/NativeWet.cpp", "-lOpenSLES", "-llog", "-o", wet_library)
                libraries.append((abi, "libfriday_wet.so", wet_library))
        with zipfile.ZipFile(build / "resources.apk") as resources, zipfile.ZipFile(build / "unsigned.apk", "w",
                                                                                      zipfile.ZIP_DEFLATED) as apk:
            for item in resources.infolist():
                apk.writestr(item, resources.read(item.filename))
            apk.write(dex / "classes.dex", "classes.dex")
            for abi, filename, library in libraries:
                apk.write(library, "lib/" + abi + "/" + filename)
            for asset in sorted((source / "assets").rglob("*")):
                if asset.is_file():
                    if asset.resolve() != asset or asset.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("unsafe_asset")
                    apk.write(asset, "assets/" + str(asset.relative_to(source / "assets")))
        run(align, "-f", "4", build / "unsigned.apk", build / "aligned.apk")
        store, password = output / "development-signing.p12", output / "development-signing.password"
        if not store.exists():
            if password.exists():
                raise ValueError("existing_signing_material_requires_review")
            password.write_text(secrets.token_urlsafe(32))
            password.chmod(0o600)
            run(args.java / "bin/keytool", "-genkeypair", "-keystore", store, "-storetype", "PKCS12",
                "-storepass:file", password, "-keypass:file", password, "-alias", "ktv-development", "-keyalg", "RSA",
                "-keysize", "3072", "-validity", "3650", "-dname", "CN=Friday KTV Development", "-noprompt")
            store.chmod(0o600)
        if not password.is_file() or password.stat().st_mode & 0o077:
            raise ValueError("private_signing_material_required")
        apk_path = output / ("friday-ktv-" + version + ".apk")
        run(signer, "sign", "--ks", store, "--ks-pass", "file:" + str(password),
            "--out", apk_path, build / "aligned.apk")
        run(signer, "verify", "--verbose", apk_path)
        permissions = run(aapt, "dump", "permissions", apk_path)
        badging = run(aapt, "dump", "badging", apk_path)
        assert "name='local.friday.ktv'" in badging
        actual_permissions = set(re.findall(r"uses-permission: name='([^']+)'", permissions))
        assert actual_permissions == {"android.permission.INTERNET", "android.permission.RECORD_AUDIO", "android.permission.MODIFY_AUDIO_SETTINGS", "local.friday.ktv.audio.CONTROL"}
        if source_snapshot(source) != before or source_snapshot(bridge_source) != bridge_before:
            raise ValueError("source_changed_during_build")
        private_input = args.nas_connection_config or args.connection_config
        if private_input and hashlib.sha256(read_regular(private_input)).hexdigest() != profile["input_sha256"]:
            raise ValueError("private_profile_changed_during_build")
        apk_path.chmod(0o600)
        receipt = {"package": "local.friday.ktv", "version": version, "min_api": 26, "target_api": 35,
                   "household_provisioned": profile is not None, "role": profile["role"] if profile else "portable",
                   "signing_identity_reused": signing_reused,
                   "sha256": hashlib.sha256(apk_path.read_bytes()).hexdigest(), "bytes": apk_path.stat().st_size,
                   "signature_verified": True, "permissions_verified": True, "installed": False, "native_audio": args.native_probe,
                   "audio_started": False, "microphone_started": False, "device_latency_verified": False,
                   "wet_effects_built": args.native_probe, "wet_effects_native_min_api": 34,
                   "wet_effects_device_verified": False,
                   "source_sha256": {**before, **bridge_before}, "source_stable_verified": True,
                   "strict_platform_bridge_required_for_vendor_dry": True,
                   "compat_vocal_presets": 7, "effects_add_dry_fifo_frames": 0}
        if profile is not None:
            receipt["connection_profile"] = {"role": profile["role"], "fingerprint": profile["fingerprint"],
                                             "input_sha256": profile["input_sha256"],
                                             "generated_source_sha256": hashlib.sha256(generated.read_bytes()).hexdigest(),
                                             "contains_private_connection": True,
                                             "apply_once": profile["role"] == "nas"}
        if args.native_probe:
            receipt["dependency_sha256"] = {"native/KTVTV/dependencies/oboe.json": hashlib.sha256(
                (ROOT / "native/KTVTV/dependencies/oboe.json").read_bytes()).hexdigest()}
    receipt["build_workspace"] = workspace_receipt(workspace)
    receipt_path = output / "build-result.json"
    receipt_path.write_text(json.dumps(receipt, indent=2))
    receipt_path.chmod(0o600)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
