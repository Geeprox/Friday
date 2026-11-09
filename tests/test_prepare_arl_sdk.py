"""准备器只生成经固定摘要核对的源码输入，不执行下载内容。"""

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_arl_sdk", ROOT / "scripts/prepare_arl_sdk.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


@pytest.fixture
def fetch():
    source = os.environ.get("FRIDAY_TEST_ARL_SDK_INPUT")
    if not source:
        pytest.skip("显式公开 SDK 输入用于离线字节复现；CI 在测试前准备")
    source = Path(source)
    _, value, _ = prepare.manifest()
    base = "https://raw.githubusercontent.com/Geeprox/ARL/" + value["source"]["commit"] + "/"

    def read(url, size):
        assert url.startswith(base)
        raw = (source / url.removeprefix(base)).read_bytes()
        assert len(raw) == size
        return raw

    return read


def test_public_lock_matches_single_contract():
    raw, value, contract = prepare.manifest()
    assert hashlib.sha256(raw).hexdigest() == contract["manifest_sha256"]
    assert len(value["modules"]) == 48
    assert {"LICENSE", "NOTICE", "pyproject.toml"} <= value["files"].keys()
    assert value["dependencies"] == []


def test_prepare_verified_reuse_and_owned_cleanup(tmp_path, fetch):
    output = tmp_path / "sdk"
    result = prepare.prepare(output, fetch=fetch)
    assert result["files"] == 52 and not result["reused"]
    assert (output / "LICENSE").read_text().startswith("MIT License")
    assert prepare.prepare(output, fetch=lambda *_: pytest.fail("复用不得重新下载"))["reused"]
    assert not list(output.rglob("*.pyc"))
    assert prepare.clean(output)["cleaned"] and not output.exists()


@pytest.mark.parametrize("change", ["changed", "extra", "missing", "symlink", "directory"])
def test_modified_input_is_refused_and_not_deleted(tmp_path, fetch, change):
    output = tmp_path / "sdk"
    prepare.prepare(output, fetch=fetch)
    if change == "changed":
        (output / "arl/client.py").write_text("modified")
    if change == "extra":
        (output / "unreviewed.py").write_text("raise AssertionError()")
    if change == "missing":
        (output / "LICENSE").unlink()
    if change == "symlink":
        path = output / "arl/client.py"
        path.unlink()
        path.symlink_to(output / "arl/contracts.py")
    if change == "directory":
        (output / "unknown").mkdir()
    with pytest.raises(ValueError):
        prepare.prepare(output, fetch=fetch)
    with pytest.raises(ValueError):
        prepare.clean(output)
    assert output.exists()


def test_changed_download_cannot_leave_an_executable_input(tmp_path):
    output = tmp_path / "sdk"
    with pytest.raises(ValueError, match="sdk_source_changed"):
        prepare.prepare(output, fetch=lambda *_: b"unreviewed source")
    assert not output.exists()
    assert prepare.clean(output)["cleaned"]


def test_cleanup_requires_ownership_even_for_valid_input(tmp_path, fetch):
    output = tmp_path / "sdk"
    prepare.prepare(output, fetch=fetch)
    receipt, _ = prepare.sidecars(output)
    receipt.unlink()
    with pytest.raises(FileNotFoundError):
        prepare.clean(output)
    assert output.exists()


def test_open_input_is_preserved_until_handle_closes(tmp_path, fetch):
    output = tmp_path / "sdk"
    prepare.prepare(output, fetch=fetch)
    with (output / "LICENSE").open("rb"):
        with pytest.raises(ValueError, match="sdk_cleanup_input_in_use"):
            prepare.clean(output)
    assert prepare.clean(output)["cleaned"]


@pytest.mark.parametrize("disable_bytecode", [False, True])
def test_regular_python_import_requires_immutable_input_environment(tmp_path, fetch, disable_bytecode):
    """用常规导入复现 CI 缓存污染；不构造客户端、宿主或模型。"""
    output = tmp_path / "sdk"
    prepare.prepare(output, fetch=fetch)
    environment = dict(os.environ, PYTHONPATH=str(output))
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    if disable_bytecode:
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
    code = """import sys
def boundary(event, args):
    if event.startswith(('socket.', 'subprocess.', 'os.system')) or (event == 'ctypes.dlopen' and args[0] is not None):
        raise AssertionError('SDK import attempted external activity')
sys.addaudithook(boundary)
import arl.client
assert arl.client.__file__ == sys.argv[1]
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, str(output / "arl/client.py")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    caches = list(output.rglob("__pycache__"))
    if disable_bytecode:
        assert not caches and prepare.verify(output)["files"] == 52
        assert prepare.clean(output)["cleaned"]
    else:
        assert caches
        with pytest.raises(ValueError, match="sdk_inventory_mismatch"):
            prepare.verify(output)
        with pytest.raises(ValueError, match="sdk_inventory_mismatch"):
            prepare.clean(output)
        assert output.exists()
