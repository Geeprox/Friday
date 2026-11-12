"""Build the separately permissioned KTV audio companion; never contacts a device."""
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
from pathlib import Path

sys.dont_write_bytecode = True
# Set this before local imports so direct invocation creates no source-tree pyc.
from ktv_build_tools import (  # noqa: E402
    build_environment,
    isolated_command,
    toolchain_identity,
    workspace_receipt,
)
from ktv_build_workspace import build_workspace  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NS = "{http://schemas.android.com/apk/res/android}"
PACKAGE = "local.friday.ktv.audio"
CONTROL = PACKAGE + ".CONTROL"
PERMISSIONS = frozenset({
    CONTROL, "android.permission.MODIFY_AUDIO_SETTINGS", "android.permission.RECORD_AUDIO",
    "android.permission.FOREGROUND_SERVICE", "android.permission.FOREGROUND_SERVICE_SPECIAL_USE",
    "android.permission.WAKE_LOCK",
})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_snapshot(source: Path, root: Path = ROOT) -> dict[str, str]:
    if not source.is_dir() or source.resolve() != source or not source.is_relative_to(root):
        raise ValueError("canonical_workspace_source_required")
    result = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("source_symlink_not_allowed")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path)
    if not result:
        raise ValueError("source_manifest_required")
    return result


def manifest_identity(source: Path, package: str) -> dict:
    tree = ET.parse(source / "AndroidManifest.xml").getroot()
    version = tree.get(NS + "versionName", "")
    if tree.get("package") != package or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version):
        raise ValueError("reviewed_package_and_version_required")
    version_code = int(tree.get(NS + "versionCode", "0"))
    sdk = tree.find("uses-sdk")
    if version_code < 1 or sdk is None or NS + "sharedUserId" in tree.attrib:
        raise ValueError("separate_package_uid_and_sdk_required")
    permissions = [node.get(NS + "name", "") for node in tree.findall("uses-permission")]
    if len(set(permissions)) != len(permissions) or tree.findall("uses-permission-sdk-23"):
        raise ValueError("explicit_unique_permissions_required")
    return {"package": package, "version": version, "version_code": version_code,
            "min_api": int(sdk.get(NS + "minSdkVersion", "0")),
            "target_api": int(sdk.get(NS + "targetSdkVersion", "0")),
            "permissions": sorted(permissions)}


def validate_companion_manifest(source: Path) -> dict:
    identity = manifest_identity(source, PACKAGE)
    if (identity["min_api"] != 34 or identity["target_api"] != 35
            or set(identity["permissions"]) != PERMISSIONS):
        raise ValueError("reviewed_audio_permission_surface_required")
    tree = ET.parse(source / "AndroidManifest.xml").getroot()
    declarations = tree.findall("permission")
    if (len(declarations) != 1 or declarations[0].get(NS + "name") != CONTROL
            or declarations[0].get(NS + "protectionLevel") != "signature"):
        raise ValueError("signature_control_permission_required")
    app = tree.find("application")
    if app is None or app.get(NS + "debuggable", "false") != "false":
        raise ValueError("non_debuggable_audio_application_required")
    components = [n for n in app if n.tag in {"activity", "activity-alias", "service", "receiver", "provider"}]
    found = set()
    for component in components:
        name = component.get(NS + "name", "")
        name = PACKAGE + name if name.startswith(".") else name
        found.add((component.tag, name))
        if (component.get(NS + "exported") != "true" or component.get(NS + "permission") != CONTROL
                or NS + "process" in component.attrib):
            raise ValueError("signature_gated_audio_components_required")
    if len(components) != 2 or found != {("activity", PACKAGE + ".PermissionActivity"),
                                        ("service", PACKAGE + ".GuardService")}:
        raise ValueError("reviewed_audio_components_required")
    return identity


def sdk_tool(sdk: Path, name: str) -> Path:
    matches = list(sdk.rglob(name))
    if len(matches) != 1 or not matches[0].is_file():
        raise ValueError("existing_sdk_tool_required_" + name)
    return matches[0]


def java_environment(java: Path) -> dict[str, str]:
    return {**os.environ, "JAVA_HOME": str(java.absolute()),
            "PATH": str(java.absolute() / "bin") + os.pathsep + os.environ.get("PATH", "")}


def run_tool(environment: dict, *command, error_dir: Path | None = None) -> str:
    result = subprocess.run(isolated_command(environment, command), env=environment, capture_output=True,
                            text=True, timeout=180)
    if result.returncode:
        if error_dir is not None:
            (error_dir / "compiler-error.txt").write_text(result.stderr[-16000:])
        raise RuntimeError("tool_failed_" + Path(str(command[0])).name)
    return result.stdout


def inspect_apk(apk: Path, identity: dict, sdk: Path, environment: dict) -> str:
    """Verify signed bytes and compare the actual APK identity/permissions to source."""
    certificate = run_tool(environment, sdk_tool(sdk, "apksigner"), "verify", "--verbose", "--print-certs", apk)
    signers = re.findall(r"Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})", certificate)
    if len(signers) != 1:
        raise ValueError("single_verified_apk_signer_required")
    aapt = sdk_tool(sdk, "aapt2")
    badging = run_tool(environment, aapt, "dump", "badging", apk)
    package_line = next((line for line in badging.splitlines() if line.startswith("package: ")), "")
    fields = dict(re.findall(r"(\w+)='([^']*)'", package_line))
    if (fields.get("name") != identity["package"] or fields.get("versionName") != identity["version"]
            or fields.get("versionCode") != str(identity["version_code"])):
        raise ValueError("apk_identity_mismatch")
    for name, expected in (("sdkVersion", identity["min_api"]), ("targetSdkVersion", identity["target_api"])):
        label = r"(?:minSdkVersion|sdkVersion)" if name == "sdkVersion" else name
        if re.findall(r"^" + label + r":'([^']+)'$", badging, re.MULTILINE) != [str(expected)]:
            raise ValueError("apk_sdk_mismatch")
    permissions = run_tool(environment, aapt, "dump", "permissions", apk)
    used = re.findall(r"^uses-permission: name='([^']+)'", permissions, re.MULTILINE)
    if (sorted(used) != identity["permissions"]
            or any(line.startswith("uses-permission-") for line in permissions.splitlines())):
        raise ValueError("apk_permission_mismatch")
    return signers[0].lower()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/android")
    parser.add_argument("--java", type=Path, default=ROOT / ".artifacts/embodied-toolchain/jdk")
    parser.add_argument("--ndk", type=Path, default=ROOT / ".artifacts/embodied-toolchain/ndk")
    args = parser.parse_args()
    os.umask(0o077)
    source = ROOT / "native/KTVTV/audio-guard"
    identity = validate_companion_manifest(source)
    before = source_snapshot(source)
    bridge_source = ROOT / "native/KTVTV/strict-cache"
    bridge_before = source_snapshot(bridge_source)
    builder_hash = digest(Path(__file__))
    sources = sorted((source / "src").rglob("*.java"))
    if not sources or (source / "res").exists() or (source / "assets").exists():
        raise ValueError("reviewed_audio_source_required")
    output = ROOT / ".artifacts/ktv-tv"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    store, password = output / "development-signing.p12", output / "development-signing.password"
    for path in (store, password):
        if not path.is_file() or path.resolve() != path or path.stat().st_mode & 0o077:
            raise ValueError("existing_private_ktv_signing_material_required_build_main_first")
    with build_workspace(toolchain_identity(args.java, ndk=args.ndk), "ktv-audio-guard-apk", configuration="ktv-system-audio") as (work, workspace):
        environment = build_environment(java_environment(args.java), work)

        def run(*command):
            try:
                return run_tool(environment, *command, error_dir=work)
            except RuntimeError:
                workspace["failed_tool"] = Path(str(command[0])).name
                raise

        android, aapt, d8, align, signer = [sdk_tool(args.sdk, n) for n in
                                           ("android.jar", "aapt2", "d8", "zipalign", "apksigner")]
        classes, dex = work / "classes", work / "dex"
        classes.mkdir()
        dex.mkdir()
        run(args.java / "bin/javac", "--release", "8", "-classpath", android, "-d", classes, *sources)
        run(d8, "--min-api", identity["min_api"], "--lib", android, "--output", dex, *sorted(classes.rglob("*.class")))
        run(aapt, "link", "-o", work / "resources.apk", "-I", android, "--manifest", source / "AndroidManifest.xml")
        properties = args.ndk / "source.properties"
        if not properties.is_file() or "27.3.13750724" not in properties.read_text():
            raise ValueError("pinned_ndk_r27d_required")
        clang = args.ndk / "toolchains/llvm/prebuilt/darwin-x86_64/bin/clang++"
        native = []
        for abi, target in (("arm64-v8a", "aarch64-linux-android34"), ("armeabi-v7a", "armv7a-linux-androideabi34")):
            library = work / (abi + "-libfriday_cache_owner.so")
            run(clang, "--target=" + target, "-shared", "-O2", "-fPIC", "-std=c++17",
                "-fno-exceptions", "-fno-rtti", "-static-libstdc++", "-Wl,-z,max-page-size=16384",
                source / "cpp/CacheOwner.cpp", "-o", library)
            native.append((abi, library))
        with zipfile.ZipFile(work / "resources.apk") as resources, zipfile.ZipFile(
                work / "unsigned.apk", "w", zipfile.ZIP_DEFLATED) as apk:
            for item in resources.infolist():
                apk.writestr(item, resources.read(item.filename))
            for path in sorted(dex.glob("classes*.dex")):
                apk.write(path, path.name)
            for abi, library in native:
                apk.write(library, "lib/" + abi + "/libfriday_cache_owner.so")
        run(align, "-f", "4", work / "unsigned.apk", work / "aligned.apk")
        # Signed candidates and the persistent signing identity stay outside
        # the disposable cache. Rename only within the delivery filesystem.
        signed = output / ("audio-candidate-" + secrets.token_hex(6) + ".apk")
        run(signer, "sign", "--ks", store, "--ks-pass", "file:" + str(password), "--out", signed, work / "aligned.apk")
        cert = inspect_apk(signed, identity, args.sdk, environment)
        if (before != source_snapshot(source) or bridge_before != source_snapshot(bridge_source)
                or builder_hash != digest(Path(__file__))):
            raise ValueError("build_source_changed_during_compile")
        receipt = {"schema": 1, **identity, "sha256": digest(signed), "bytes": signed.stat().st_size,
                   "certificate_sha256": cert, "signature_verified": True, "permissions_verified": True,
                   "source_sha256": {**before, **bridge_before}, "builder_sha256": {"scripts/build_ktv_audio_guard.py": builder_hash},
                   "native_cache_owner_built": True, "strict_platform_bridge_required": True,
                   "same_signing_store_as_main": True, "household_provisioned": False,
                   "installed": False, "device_contacted": False, "audio_started": False,
                   "microphone_started": False, "device_latency_verified": False}
        apk_path = output / ("friday-ktv-audio-" + identity["version"] + ".apk")
        signed.chmod(0o600)
        signed.replace(apk_path)
    receipt["build_workspace"] = workspace_receipt(workspace)
    receipt_text = json.dumps(receipt, indent=2) + "\n"
    version_receipt = output / ("audio-build-" + identity["version"] + "-result.json")
    version_receipt.write_text(receipt_text)
    pending = output / (".audio-result-" + secrets.token_hex(6) + ".json")
    pending.write_text(receipt_text)
    pending.replace(output / "audio-build-result.json")
    print(receipt_text, end="")


if __name__ == "__main__":
    main()
