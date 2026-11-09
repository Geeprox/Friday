"""Build ordering over tiny synthetic files; no compiler, signer or SDK calls."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    path = ROOT / "native/FridayDesktop/script/build_desktop.py"
    spec = importlib.util.spec_from_file_location("synthetic_desktop_packaging", path)
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    project = tmp_path / "source"
    native = project / "native/FridayDesktop"
    entries = [
        "Package.swift",
        "backend/desktop_backend.py",
        "Resources/product-inputs.json",
        "Resources/com.friday.household.service.plist",
        "Resources/AppIcon/Friday.icns",
        "script/build_desktop.py",
        "script/build_and_run.sh",
        "script/install_desktop.py",
        "tests/check_packaged_import.py",
        "tests/check_packaged_application_sdk.py",
        "Main.swift",
    ]
    for name in entries:
        item = native / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text("# SYNTHETIC BUILD INPUT\n")
    value = dict(version="0.0.0", build="0", bundle_identifier="com.friday.home.desktop")
    (native / "Resources/product-inputs.json").write_text(json.dumps(value))
    sdk = tmp_path / "reviewed-sdk-input"
    (sdk / "arl").mkdir(parents=True)
    payload = {"arl/__init__.py": "# SYNTHETIC SDK INPUT\n", "LICENSE": "SYNTHETIC LICENSE\n"}
    for name, text in payload.items():
        (sdk / name).write_text(text)
    manifest = {
        "files": {
            name: {"sha256": hashlib.sha256(text.encode()).hexdigest()} for name, text in payload.items()
        }
    }
    (sdk / "ARL-SDK-MANIFEST.json").write_text(json.dumps(manifest))
    requirement = project / "homeplane/arl_sdk_requirements.json"
    requirement.parent.mkdir()
    requirement.write_text(
        json.dumps(
            {
                "manifest_name": "ARL-SDK-MANIFEST.json",
                "manifest_sha256": hashlib.sha256((sdk / "ARL-SDK-MANIFEST.json").read_bytes()).hexdigest(),
            }
        )
    )
    monkeypatch.setattr(build, "REPO", project)
    monkeypatch.setattr(build, "NATIVE", native)
    monkeypatch.setattr(build, "__file__", str(native / "script/build_desktop.py"))
    area = tmp_path / "build"
    area.mkdir()
    binary = area / "compiled/Friday"
    binary.parent.mkdir()
    binary.write_bytes(b"SYNTHETIC-NOT-EXECUTABLE")
    result = SimpleNamespace(
        build=build,
        area=area,
        app=tmp_path / "Friday.app",
        calls=[],
        sdk=sdk,
        records={"homeplane/arl_sdk_requirements.json": hashlib.sha256(requirement.read_bytes()).hexdigest()},
        outcome="pass",
        fail_stage="check_packaged_import.py",
        args=SimpleNamespace(
            python=Path("/synthetic/python"), task_id="synthetic", signing_identity="-", arl_sdk_input=sdk
        ),
    )

    def run(command, **options):
        result.calls.append(command)
        if command[0] == "xcrun":
            return subprocess.CompletedProcess(
                command, 0, str(binary.parent) + "\n" if "--show-bin-path" in command else ""
            )
        if command[0] == "codesign":
            return subprocess.CompletedProcess(command, 0, "")
        assert command[:3] == ["/synthetic/python", "-I", "-B"]
        if command[3] == "-c":
            return subprocess.CompletedProcess(command, 0, "")
        stage = Path(command[3]).name
        assert stage in {"check_packaged_import.py", "check_packaged_application_sdk.py"}
        outcome = result.outcome if stage == result.fail_stage else "pass"
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if outcome == "process-failed":
            return subprocess.CompletedProcess(command, 1, "")
        if outcome != "missing-receipt":
            output.write_text(
                json.dumps(
                    dict(
                        phase="wrong"
                        if outcome == "wrong-phase"
                        else "LOCAL_DEVELOPMENT_APP_APPLICATION_SDK_PASS"
                        if stage == "check_packaged_application_sdk.py"
                        else "LOCAL_DEVELOPMENT_APP_SOURCE_IMPORT_BINDING_PASS",
                        passed=outcome != "refused",
                    )
                )
            )
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(build.subprocess, "run", run)
    return result


@pytest.mark.parametrize("outcome", ["process-failed", "missing-receipt", "wrong-phase", "refused"])
@pytest.mark.parametrize("stage", ["check_packaged_import.py", "check_packaged_application_sdk.py"])
def test_runtime_check_failure_prevents_signing_and_keeps_failed_receipt(candidate, outcome, stage):
    candidate.outcome = outcome
    candidate.fail_stage = stage
    with pytest.raises((ValueError, OSError, subprocess.CalledProcessError)):
        candidate.build.package(candidate.args, candidate.app, candidate.records, candidate.area)
    assert not any(c[0] == "codesign" for c in candidate.calls)
    assert json.loads((candidate.area / "manifest.json").read_text())["status"] == "failed"


def test_package_binds_the_runtime_check_before_signing(candidate):
    candidate.build.package(candidate.args, candidate.app, candidate.records, candidate.area)
    commands = candidate.calls
    check = next(
        i for i, c in enumerate(commands) if len(c) > 3 and Path(c[3]).name == "check_packaged_import.py"
    )
    signing = next(i for i, c in enumerate(commands) if c[0] == "codesign")
    assert check < signing
    sdk_check = next(
        i
        for i, c in enumerate(commands)
        if len(c) > 3 and Path(c[3]).name == "check_packaged_application_sdk.py"
    )
    assert check < sdk_check < signing
    receipt = json.loads((candidate.area / "manifest.json").read_text())
    assert (
        receipt["status"] == "verified" and receipt["runtime_validation"]["source_import"]["passed"] is True
    )
    binding = json.loads((candidate.app / "Contents/Resources/FRIDAY-SOURCE-INTEGRATION.json").read_text())
    assert "native/FridayDesktop/tests/check_packaged_import.py" in binding["build_inputs"]
    assert "native/FridayDesktop/tests/check_packaged_application_sdk.py" in binding["build_inputs"]


def test_package_carries_frozen_sdk_and_license_with_exact_hash_bindings(candidate):
    candidate.build.package(candidate.args, candidate.app, candidate.records, candidate.area)
    vendor = candidate.app / "Contents/Resources/product/vendor"
    binding = json.loads((candidate.app / "Contents/Resources/FRIDAY-SOURCE-INTEGRATION.json").read_text())
    for original in candidate.sdk.rglob("*"):
        if original.is_file():
            relative = original.relative_to(candidate.sdk)
            assert (vendor / relative).read_bytes() == original.read_bytes()
            path = "Resources/product/vendor/" + relative.as_posix()
            assert binding["packaged_inputs"][path] == hashlib.sha256(original.read_bytes()).hexdigest()


@pytest.mark.parametrize("change", ["missing", "changed", "extra", "symlink", "pin"])
def test_unreviewed_sdk_input_cannot_reach_the_compiler(candidate, change):
    target = candidate.sdk / "arl/__init__.py"
    if change == "missing":
        target.unlink()
    if change == "changed":
        target.write_text("# UNREVIEWED SDK BYTES\n")
    if change == "extra":
        (candidate.sdk / "arl/extra.py").write_text("# UNREVIEWED\n")
    if change == "symlink":
        target.unlink()
        target.symlink_to(candidate.sdk / "LICENSE")
    if change == "pin":
        path = candidate.sdk / "ARL-SDK-MANIFEST.json"
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        candidate.build.package(candidate.args, candidate.app, candidate.records, candidate.area)
    assert not candidate.calls and not candidate.app.exists()
    assert json.loads((candidate.area / "manifest.json").read_text())["status"] == "failed"
