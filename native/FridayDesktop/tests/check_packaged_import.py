"""Local development App import binding and pure generated projection.

Import the backend module definitions without constructing service/app/ARL or
calling config/credentials. Audit denies network, child processes and private
runtime data access. No server, ASGI lifespan, real model or account is started.
"""
from pathlib import Path
import ast
import hashlib
import importlib.abc
import importlib.util
import json
import socket
import sys

REPO = Path(__file__).resolve().parents[3]
if len(sys.argv) != 3:
    raise SystemExit('Usage: embedded-python -I -B check_packaged_import.py APP OUTPUT')
APP = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[2]).resolve()
ARTIFACTS = REPO / '.artifacts'
assert APP.is_relative_to(ARTIFACTS.resolve()) and APP.name == 'Friday.app'
assert OUTPUT.is_relative_to(ARTIFACTS.resolve()) and not OUTPUT.exists()
PRODUCT = APP / 'Contents/Resources/product'
BACKEND = APP / 'Contents/Resources/backend/desktop_backend.py'
assert (PRODUCT / 'runtime/bin/python3').is_file()
# Development packages use the selected local Python environment, not an embedded runtime.
sys.path[:0] = [str(PRODUCT / 'vendor'), str(PRODUCT / 'app')]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(event, args):
    if event.startswith(('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostby')):
        raise AssertionError('No network authorized by package import check')
    if event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.spawn', 'ctypes.dlopen'}:
        # Standard Python extension modules load through the interpreter, not
        # ctypes; no model/camera/other native SDK needs ctypes in this check.
        raise AssertionError('No process/native SDK authorized by package import check')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        path = str(args[0])
        if '/Application Support/Friday/' in path or '/Application Support/ARL/' in path or '/data/credentials/' in path or '/Repository/Friday/data/' in path:
            raise AssertionError('Private runtime configuration/account data access denied')


class NoModelSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'arl', 'miloco', 'mlx', 'torch', 'transformers'}:
            raise AssertionError('No actual model/SDK import authorized')
        return None


def forbidden(*args, **kwargs):
    raise AssertionError('No sockets or DNS authorized')


sys.addaudithook(audit)
sys.meta_path.insert(0, NoModelSDK())
for name in ['connect', 'connect_ex', 'bind', 'listen']:
    setattr(socket.socket, name, forbidden)
socket.getaddrinfo = socket.create_connection = forbidden
spec = importlib.util.spec_from_file_location('friday_packaged_backend_import_only', BACKEND)
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)
assert backend.PRODUCT.resolve() == PRODUCT.resolve()
assert callable(backend.serve) and hasattr(backend, 'DesktopService')
tree = ast.parse(BACKEND.read_text())
assert any(isinstance(n, ast.If) and '__main__' in ast.dump(n.test) for n in tree.body)
assert not any(isinstance(n, (ast.Assign, ast.AnnAssign)) and 'DesktopService(' in ast.get_source_segment(BACKEND.read_text(), n) for n in tree.body)

import homeplane.runtime_log as log
from starlette.responses import JSONResponse

assert Path(log.__file__).resolve() == (PRODUCT / 'app/homeplane/runtime_log.py').resolve()
checks = []
for code in ['control_cancelled_after_send', 'flow_operation_invalidated', 'rule_cleanup_pending', 'nas_source_changed', 'ha_authorization_rejected']:
    assert log.fixed_code(code) == code
    response = JSONResponse({'code': code, 'detail': 'generated fixture text'}, status_code=409)
    assert log.result_projection(response) == ('refused', code)
    checks.append({'generated_allowlisted_response_code': code, 'projection': ['refused', code]})
assert log.fixed_code('generated-private-body-must-not-be-code') == 'unclassified'
assert log.result_projection(JSONResponse({'code': 'generated-private-body-must-not-be-code'}, status_code=503)) == ('unavailable', 'unclassified')
modules = {}
for name, module in list(sys.modules.items()):
    path = getattr(module, '__file__', None)
    if not path:
        continue
    if name == spec.name or name.split('.')[0] in {'homeplane', 'friday', 'fastapi', 'starlette', 'httpx', 'pydantic'}:
        path = Path(path).resolve()
        if name == spec.name or name.split('.')[0] in {'homeplane', 'friday'}:
            assert path.is_relative_to(APP.resolve()), 'External project source fallback: ' + name
        modules[name] = {'path': str(path), 'sha256': sha(path)}
assert modules[spec.name]['sha256'] == sha(BACKEND)
result = {'phase': 'LOCAL_DEVELOPMENT_APP_SOURCE_IMPORT_BINDING_PASS',
          'passed': True, 'python_executable': sys.executable, 'python_prefix': sys.prefix,
          'python_version': sys.version.split()[0], 'backend_imported_only': True,
          'module_count': len(modules), 'modules': modules, 'generated_pure_projection_checks': checks,
          'external_project_source_fallback': False, 'external_python_environment': True,
          'service_app_ARL_or_ASGI_lifespan_constructed_or_started': 0,
          'private_config_credential_model_nativeSDK_childprocess_network_reads_or_calls': 0,
          'native_or_GUI_runtime_qualification': False}
OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'phase': result['phase'], 'module_count': len(modules), 'pure_generated_cases': len(checks), 'services_started': 0}))
