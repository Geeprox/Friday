"""Real packaging gate with explicitly synthetic SDK files and no host calls."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "native/FridayDesktop/tests/check_packaged_application_sdk.py"


def package(tmp_path, code, *, vendor=True):
    app = tmp_path / "Friday.app"
    product = app / "Contents/Resources/product"
    home = product / "app/homeplane"
    home.mkdir(parents=True)
    (home / "__init__.py").write_text("")
    (home / "arl_application.py").write_text(code)
    target = product / "vendor" if vendor else tmp_path / "synthetic-unfrozen-sdk"
    sdk = target / "arl"
    sdk.mkdir(parents=True)
    for name in ("__init__.py", "client.py", "application_session.py"):
        (sdk / name).write_text("# SYNTHETIC SDK, NO CLIENT/SESSION IMPLEMENTATION\n")
    return app, target


def check(app, tmp_path):
    output = tmp_path / "gate.json"
    run = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), str(app), str(output)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert output.exists(), run.stdout + run.stderr
    return run, json.loads(output.read_text())


@pytest.mark.parametrize(
    "body,expected",
    [
        ("pass", "application_sdk_probe_unavailable"),
        ('def sdk_compatibility(): return {"compatible":False}', "application_sdk_incompatible"),
        ('def sdk_compatibility(): return {"compatible":True}', "application_sdk_imports_unverified"),
    ],
)
def test_missing_or_unqualified_contract_cannot_pass(tmp_path, body, expected):
    app, _ = package(tmp_path, body)
    run, result = check(app, tmp_path)
    assert run.returncode == 1 and result["passed"] is False and result["code"] == expected


@pytest.mark.parametrize("vendor", [False, True])
def test_import_origin_is_bound_to_the_frozen_package_instead_of_version_strings(tmp_path, vendor):
    app, source = package(tmp_path, "", vendor=vendor)
    code = """def sdk_compatibility():
 import sys
 sys.path.insert(0, SOURCE)
 import arl.client,arl.application_session
 return {'compatible':True}
""".replace("SOURCE", repr(str(source)))
    (app / "Contents/Resources/product/app/homeplane/arl_application.py").write_text(code)
    run, result = check(app, tmp_path)
    assert (run.returncode == 0) is vendor and result["passed"] is vendor
    if vendor:
        assert set(result["modules"]) == {"arl", "arl.client", "arl.application_session"}
        assert all(row["path"].startswith("vendor/arl/") for row in result["modules"].values())
    else:
        assert result["code"] == "application_sdk_not_frozen"


@pytest.mark.parametrize("effect", ["network", "thread", "write", "model"])
def test_check_contract_cannot_start_a_host_thread_write_private_state_or_load_models(tmp_path, effect):
    # If a guard regresses, the only socket is a test-owned inert listener.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        mutation = tmp_path / "must-not-be-written"
        operation = {
            "network": f"import socket;socket.socket().connect(('127.0.0.1',{listener.getsockname()[1]}))",
            "thread": "import threading;threading.Thread(target=lambda:None).start()",
            "write": f"from pathlib import Path;Path({str(mutation)!r}).write_text('synthetic forbidden mutation')",
            "model": "import torch",
        }[effect]
        code = (
            "def sdk_compatibility():\n import arl.client,arl.application_session\n "
            + operation
            + "\n return {'compatible':True}\n"
        )
        app, _ = package(tmp_path, code)
        run, result = check(app, tmp_path)
    assert run.returncode == 1 and result["passed"] is False
    assert result["code"] == "application_sdk_probe_failed"
    assert result["error_type"] == "AssertionError"
    assert not mutation.exists()


def test_a_contract_cannot_suppress_a_blocked_effect_and_return_compatible(tmp_path):
    code = """def sdk_compatibility():
 import arl.client,arl.application_session,threading
 try:threading.Thread(target=lambda:None).start()
 except AssertionError:pass
 return {'compatible':True}
"""
    app, _ = package(tmp_path, code)
    run, result = check(app, tmp_path)
    assert run.returncode == 1 and result["passed"] is False
    assert result["code"] == "application_sdk_effect_attempted"
    assert result["blocked_effect_attempts"] == 1
