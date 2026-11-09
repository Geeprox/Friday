"""准备器只生成经固定摘要核对的源码输入，不执行下载内容。"""

import hashlib
import importlib.util
import os
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
