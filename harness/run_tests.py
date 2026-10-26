"""One offline subprocess in the already approved scientific venv, no installs."""

import argparse
import importlib.abc
import json
import resource
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVED = ROOT / '.venv'
APP_PACKAGES = APPROVED / 'lib/python3.12/site-packages'
OUTPUT = ROOT / '.artifacts/scientific-check'
parser = argparse.ArgumentParser()
parser.add_argument('--dependency-check', action='store_true')
args = parser.parse_args()
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.append(str(APP_PACKAGES))  # framework-only fallback, after isolated science
import homeplane  # noqa: E402

# Read-only support modules for the actual parent/callback classes; candidate
# recognition/media files remain first. No model/SDK/source service is started.
homeplane.__path__.append(str(ROOT / 'homeplane'))


class NativeFence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cv2', 'sherpa_onnx', 'miot', 'torch', 'mlx', 'av',
                                     'onnxruntime', 'transformers', 'openai', 'anthropic', 'sklearn'}:
            raise ImportError('native_or_model_or_clustering_runtime_forbidden')


sys.meta_path.insert(0, NativeFence())


def audit(event, args):
    if event in {'socket.connect', 'socket.bind', 'subprocess.Popen', 'os.system', 'os.exec', 'os.fork'}:
        raise RuntimeError('network_or_additional_subprocess_forbidden')
    if event == 'open' and isinstance(args[0], str) and args[0].startswith('/dev/'):
        if args[0] not in {'/dev/null', '/dev/urandom', '/dev/random'}:
            raise RuntimeError('device_access_forbidden')


sys.addaudithook(audit)
resource.setrlimit(resource.RLIMIT_CPU, (30, 30))

import numpy  # noqa: E402
import pytest  # noqa: E402
import scipy  # noqa: E402
import scipy.optimize  # noqa: E402
import scipy.optimize._lsap  # noqa: E402

# Report current versions without transferring the old 2.3.3/1.16.2 qualification.
HISTORICAL_VERSIONS = {'numpy': '2.3.3', 'scipy': '1.16.2'}
assert Path(sys.prefix).resolve() == APPROVED.resolve()
for module in (numpy, scipy, scipy.optimize):
    assert Path(module.__file__).resolve().is_relative_to(APPROVED.resolve())

if args.dependency_check:
    rows, cols = scipy.optimize.linear_sum_assignment(numpy.asarray([[1, 4], [4, 1]]))
    assert rows.tolist() == [0, 1] and cols.tolist() == [0, 1]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    record = {'phase': 'CURRENT_PROJECT_SCIENTIFIC_INPUTS_CHECK_PASS', 'synthetic_only': True,
              'numpy': numpy.__version__, 'scipy': scipy.__version__,
              'numpy_path': numpy.__file__, 'scipy_path': scipy.__file__,
              'historical_versions': HISTORICAL_VERSIONS,
              'historical_full_suite_qualification_transferred': False,
              'assignment_calls': 1, 'production_services_or_models_started': 0}
    (OUTPUT / 'DEPENDENCY-RESULT.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))
    raise SystemExit(0)

real_assignment = scipy.optimize.linear_sum_assignment
assignment_shapes = []


def observed_assignment(matrix):
    output = real_assignment(matrix)
    assignment_shapes.append([len(matrix), len(matrix[0])])
    return output


scipy.optimize.linear_sum_assignment = observed_assignment


class Evidence:
    def __init__(self):
        self.counts = {}

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed:
            self.counts[report.outcome] = self.counts.get(report.outcome, 0) + 1


recorder = Evidence()
exit_code = pytest.main(['-q', '-p', 'no:cacheprovider', '--basetemp', str(OUTPUT/'pytest-tmp'),
                         str(ROOT/'tests')], plugins=[recorder])
record = {'exit_code': int(exit_code), 'results': recorder.counts, 'python': sys.version.split()[0],
          'numpy': numpy.__version__, 'scipy': scipy.__version__, 'scipy_path': scipy.__file__,
          'assignment_implementation': real_assignment.__module__,
          'assignment_extension': scipy.optimize._lsap.__file__,
          'real_assignment_calls': len(assignment_shapes), 'assignment_shapes': assignment_shapes,
          'scientific_environment': str(APPROVED), 'framework_fallback': str(APP_PACKAGES),
          'synthetic_only': True, 'cpu_limit_seconds': 30,
          'self_peak_rss_bytes_macos': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
          'native_model_media_network_or_clustering_runtime': False,
          'extra_subprocesses': False, 'hard_rss_monitor': 'not_run', 'real_camera_validation': False}
record['v3_evidence_kind'] = 'synthetic OpenCV API fixtures, constructor-only native records, in-memory parent IPC'
record['native_admission_issuer_or_enablement'] = False
OUTPUT.mkdir(parents=True, exist_ok=True)
(OUTPUT/'test-summary.json').write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps({key: value for key, value in record.items() if key != 'assignment_shapes'}, sort_keys=True))
raise SystemExit(exit_code)
