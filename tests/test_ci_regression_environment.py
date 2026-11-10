"""真实 CI 子进程的临时数据库、不可变输入环境与结束清理。"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_regression", ROOT / "scripts/run_ci_regression.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_workspace_runs_original_store_without_changing_location_policy(tmp_path):
    receipt = tmp_path / "receipt.json"
    code = """import os, pathlib, sys, tempfile
sys.path.insert(0, sys.argv[1])
from homeplane.store import TraceStore, local_database_path
root = pathlib.Path(os.environ['TMPDIR']).resolve()
assert pathlib.Path(tempfile.gettempdir()).resolve() == root
assert sys.dont_write_bytecode
try:
    local_database_path(pathlib.Path(sys.argv[1]) / '.artifacts' / 'not-a-temp-database.sqlite')
except ValueError as error:
    assert str(error).startswith('local_database_path_required')
else:
    raise AssertionError('production path policy was weakened')
store = TraceStore(root / 'p' / 'trace.sqlite')
assert store.connection.execute('PRAGMA quick_check').fetchone() == ('ok',)
store.connection.close()
"""
    with runner.workspace(receipt, parent=tmp_path) as (root, environment, state):
        result = subprocess.run(
            [sys.executable, "-c", code, str(ROOT)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        state["returncode"] = result.returncode
    saved = json.loads(receipt.read_text())
    assert saved["cleaned"] and saved["returncode"] == 0 and not root.exists()


def test_failed_pytest_keeps_exit_code_and_cleans_owned_workspace(tmp_path):
    receipt = tmp_path / "failed.json"
    observed = tmp_path / "cache-observed.json"
    fixture = tmp_path / "test_synthetic_failure.py"
    fixture.write_text("""import json, os
from pathlib import Path

def test_synthetic_failure(pytestconfig):
    expected = Path(os.environ['TMPDIR']).resolve() / 'pytest-cache'
    assert Path(pytestconfig.getini('cache_dir')).resolve() == expected
    assert pytestconfig.cache is not None
    pytestconfig.cache.set('ci/owned', {'value': 'SYNTHETIC'})
    entry = expected / 'v' / 'ci' / 'owned'
    assert json.loads(entry.read_text()) == {'value': 'SYNTHETIC'}
    Path(os.environ['FRIDAY_TEST_CACHE_OBSERVATION']).write_text(
        json.dumps({'cache_dir': str(expected), 'existed_before_exit': entry.is_file()}))
    assert False, 'SYNTHETIC failure'
""")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "scripts/run_ci_regression.py"),
            "--receipt",
            str(receipt),
            "--temp-parent",
            str(tmp_path),
            "--",
            "-q",
            str(fixture),
        ],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", FRIDAY_TEST_CACHE_OBSERVATION=str(observed)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    saved = json.loads(receipt.read_text())
    assert saved["returncode"] == 1 and saved["cleaned"] and not Path(saved["root"]).exists()
    cache = json.loads(observed.read_text())
    assert cache["existed_before_exit"]
    assert cache["cache_dir"] == str(Path(saved["root"]) / "pytest-cache")
    assert not Path(cache["cache_dir"]).exists() and not (tmp_path / ".pytest_cache").exists()


def test_cleanup_refuses_open_reference_and_preserves_receipt(tmp_path):
    receipt = tmp_path / "opened.json"
    stream = None
    try:
        with pytest.raises(ValueError, match="ci_workspace_in_use"):
            with runner.workspace(receipt, parent=tmp_path) as (root, _, _):
                stream = (root / "still-open").open("w")
        saved = json.loads(receipt.read_text())
        assert saved["status"] == "cleanup_refused" and not saved["cleaned"] and root.exists()
    finally:
        if stream is not None:
            stream.close()
    runner.clean_owned(root)
    assert not root.exists()
