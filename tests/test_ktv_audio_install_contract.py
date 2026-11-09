"""Local build/install boundaries only; no subprocess, network, TV or audio actions."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contract(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    builder = importlib.import_module("build_ktv_audio_guard")
    installer = importlib.import_module("install_ktv_tv")
    root = tmp_path.resolve()
    output = root / ".artifacts/ktv-tv"
    output.mkdir(parents=True)
    script = root / "scripts/build_ktv_audio_guard.py"
    script.parent.mkdir()
    script.write_text("# synthetic builder identity\n")
    bridge = root / "native/KTVTV/strict-cache"
    bridge.mkdir(parents=True)
    (bridge / "StrictCacheClient.h").write_text("// synthetic fixed cache transport\n")
    cert = "a" * 64
    for role, dirname, package, prefix, receipt_name in (
        ("audio", "audio-guard", builder.PACKAGE, "friday-ktv-audio-", "audio-build-result.json"),
        ("main", "android", installer.PACKAGE, "friday-ktv-", "build-result.json"),
    ):
        source = root / "native/KTVTV" / dirname
        (source / "src").mkdir(parents=True)
        (source / "AndroidManifest.xml").write_bytes((ROOT / "native/KTVTV" / dirname / "AndroidManifest.xml").read_bytes())
        (source / "src/Fixture.java").write_text("final class Fixture {}\n")
        identity = builder.manifest_identity(source, package)
        apk = output / (prefix + identity["version"] + ".apk")
        apk.write_bytes(("synthetic signed " + role).encode())
        receipt = {**identity, "schema": 1, "signature_verified": True, "permissions_verified": True,
                   "source_sha256": {**builder.source_snapshot(source, root), **builder.source_snapshot(bridge, root)}, "sha256": builder.digest(apk),
                   "bytes": apk.stat().st_size, "native_audio": False, "certificate_sha256": cert,
                   "household_provisioned": False, "same_signing_store_as_main": True,
                   "builder_sha256": {"scripts/build_ktv_audio_guard.py": builder.digest(script)}}
        (output / receipt_name).write_text(json.dumps(receipt))
    monkeypatch.setattr(installer, "inspect_apk", lambda *args: cert)
    return SimpleNamespace(root=root, output=output, builder=builder, installer=installer, cert=cert)


def verify(contract):
    return contract.installer.verify_builds(contract.output, contract.root / "sdk", contract.root / "java", contract.root)


def test_verified_pair_order_and_actual_signer_comparison(contract, monkeypatch):
    builds = verify(contract)
    assert [build.role for build in builds] == ["audio", "main"]
    monkeypatch.setattr(contract.installer, "inspect_apk", lambda apk, identity, *args:
                        contract.cert if identity["package"] == contract.builder.PACKAGE else "b" * 64)
    with pytest.raises(ValueError, match="signatures_must_match"):
        verify(contract)


@pytest.mark.parametrize("mutation", ["change", "add", "symlink", "receipt_omission"])
def test_source_changes_are_rejected_before_apk_tools(contract, monkeypatch, mutation):
    source = contract.root / "native/KTVTV/audio-guard"
    if mutation == "change":
        (source / "src/Fixture.java").write_text("final class Changed {}\n")
    elif mutation == "add":
        (source / "src/New.java").write_text("final class New {}\n")
    elif mutation == "symlink":
        (source / "src/Link.java").symlink_to(source / "src/Fixture.java")
    else:
        path = contract.output / "audio-build-result.json"
        receipt = json.loads(path.read_text())
        receipt["source_sha256"].pop("native/KTVTV/audio-guard/src/Fixture.java")
        path.write_text(json.dumps(receipt))
    monkeypatch.setattr(contract.installer, "inspect_apk", lambda *args: pytest.fail("APK tool should not run"))
    with pytest.raises(ValueError, match="source"):
        verify(contract)


@pytest.mark.parametrize("field,value", [("package", "unreviewed.package"), ("version", "../../other"),
                                         ("signature_verified", False), ("sha256", "0" * 64)])
def test_unknown_receipt_or_changed_apk_fails_closed(contract, field, value):
    path = contract.output / "audio-build-result.json"
    receipt = json.loads(path.read_text())
    receipt[field] = value
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        verify(contract)


def test_companion_cannot_gain_network_or_unprotected_entry(contract):
    source = contract.root / "native/KTVTV/audio-guard"
    manifest = source / "AndroidManifest.xml"
    original = manifest.read_text()
    manifest.write_text(original.replace("</manifest>", '<uses-permission android:name="android.permission.INTERNET" /></manifest>'))
    with pytest.raises(ValueError, match="permission_surface"):
        contract.builder.validate_companion_manifest(source)
    manifest.write_text(original.replace('android:permission="local.friday.ktv.audio.CONTROL"', ""))
    with pytest.raises(ValueError, match="signature_gated"):
        contract.builder.validate_companion_manifest(source)


def test_builder_change_requires_new_audio_receipt(contract):
    (contract.root / "scripts/build_ktv_audio_guard.py").write_text("# changed build process\n")
    with pytest.raises(ValueError, match="builder_drift"):
        verify(contract)


def test_shared_cache_transport_is_part_of_both_apk_source_receipts(contract):
    header = contract.root / "native/KTVTV/strict-cache/StrictCacheClient.h"
    header.write_text("// changed authentication or cache transport\n")
    for role in ("audio", "main"):
        with pytest.raises(ValueError, match="build_source_drift"):
            contract.installer.verify_build(role, contract.output, contract.root / "sdk", {}, contract.root)


def test_companion_failure_prevents_main_install_and_any_launch(contract, monkeypatch):
    calls = []

    def install(build, *args):
        calls.append(build.role)
        return {"role": build.role, "installed_reported": False, "outcome": "denied"}

    monkeypatch.setattr(contract.installer, "install_build", install)
    result = contract.installer.install_pair(verify(contract), "unused", contract.output,
                                             lambda: pytest.fail("No launch after failure"), False, False)
    assert calls == ["audio"]
    assert result["outcome"] == "audio_denied"
    assert not result["installed_reported"] and not result["main_launch_requested"]


def test_no_launch_installs_both_in_order_without_starting_either(contract, monkeypatch):
    calls = []

    def install(build, *args):
        calls.append(build.role)
        return {"role": build.role, "installed_reported": True, "outcome": "installed_reported"}

    monkeypatch.setattr(contract.installer, "install_build", install)
    result = contract.installer.install_pair(verify(contract), "unused", contract.output,
                                             lambda: pytest.fail("--no-launch must not create launch session"), True, True)
    assert calls == ["audio", "main"]
    assert result["installed_reported"] and not result["main_launch_requested"]
    assert not result["runtime_verified"] and not result["physical_latency_verified"]


def test_dry_run_accepts_all_flags_without_private_config_or_network(contract, monkeypatch, capsys):
    monkeypatch.setattr(contract.installer, "ROOT", contract.root)
    monkeypatch.setattr(contract.installer.sys, "argv", ["install_ktv_tv.py", "--dry-run", "--review-prompt", "--no-launch",
                                                        "--config", str(contract.root / "does-not-exist.json")])
    monkeypatch.setattr(contract.installer.socket, "socket", lambda *args: pytest.fail("dry-run contacted a device"))
    monkeypatch.setattr(contract.installer, "install_pair", lambda *args: pytest.fail("dry-run installed an APK"))
    assert contract.installer.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["same_signer_verified"] and not result["device_contacted"]
    assert result["install_order"] == [contract.builder.PACKAGE, contract.installer.PACKAGE]
    assert not result["main_launch_planned"]


@pytest.mark.parametrize("changed", ["package", "permissions", "certificate"])
def test_sdk_readback_must_match_manifest_and_single_signer(contract, monkeypatch, changed):
    sdk = contract.root / "sdk"
    sdk.mkdir()
    for name in ("apksigner", "aapt2"):
        (sdk / name).touch()
    identity = contract.builder.validate_companion_manifest(contract.root / "native/KTVTV/audio-guard")
    package = "unknown.package" if changed == "package" else identity["package"]
    signers = "Signer #1 certificate SHA-256 digest: " + contract.cert
    if changed == "certificate":
        signers += "\nSigner #2 certificate SHA-256 digest: " + "b" * 64
    permissions = identity["permissions"] + (["android.permission.INTERNET"] if changed == "permissions" else [])

    def tool(environment, *args, **kwargs):
        if "--print-certs" in args:
            return signers
        if "badging" in args:
            return (f"package: name='{package}' versionCode='{identity['version_code']}' versionName='{identity['version']}'\n"
                    "sdkVersion:'34'\ntargetSdkVersion:'35'\n")
        return "\n".join("uses-permission: name='" + name + "'" for name in permissions)

    monkeypatch.setattr(contract.builder, "run_tool", tool)
    with pytest.raises(ValueError):
        contract.builder.inspect_apk(contract.root / "unused.apk", identity, sdk, {})
