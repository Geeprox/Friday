"""No test may inherit a household camera configuration, model or runtime log."""
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

_ARTIFACTS = Path(__file__).resolve().parents[1] / '.artifacts'
(_ARTIFACTS / 'test-evidence').mkdir(parents=True, exist_ok=True)
(_ARTIFACTS / 'tmp').mkdir(exist_ok=True)

_TEST_DATA = Path(tempfile.mkdtemp(prefix='friday-tests-', dir='/tmp'))
_PREVIOUS_DATA = os.environ.get('FRIDAY_EMBODIED_DATA')
os.environ['FRIDAY_EMBODIED_DATA'] = str(_TEST_DATA)


def pytest_sessionfinish(session, exitstatus):
    if _PREVIOUS_DATA is None:
        os.environ.pop('FRIDAY_EMBODIED_DATA', None)
    else:
        os.environ['FRIDAY_EMBODIED_DATA'] = _PREVIOUS_DATA
    shutil.rmtree(_TEST_DATA, ignore_errors=True)


@pytest.fixture(scope='session', autouse=True)
def browser_dependencies():
    # Load the real display-copy dependency before VM-mounted UI fragments.
    original = os.environ.get('NODE_OPTIONS')
    preload = Path(__file__).parent / 'copy_layout_preload.cjs'
    os.environ['NODE_OPTIONS'] = (original or '') + ' --require=' + json.dumps(str(preload))
    yield
    if original is None:
        os.environ.pop('NODE_OPTIONS', None)
    else:
        os.environ['NODE_OPTIONS'] = original
