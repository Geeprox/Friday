"""One import-only compatibility boundary for Friday's reviewed ARL SDK input.

Call in a fresh, single-threaded probe interpreter. This is a guard around fixed,
reviewed source, not a sandbox for arbitrary Python. No SDK constructor or RPC is
called. A compatible result says nothing about host registration or inference.
"""
from __future__ import annotations

import hashlib
import _thread
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import sysconfig
import threading


class _Refused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise _Refused(code)


def _read_regular(path, limit):
    if any(part.is_symlink() for part in (path, *path.parents)):
        _fail('sdk_unsafe_path')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            _fail('sdk_unsafe_path')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        _fail('sdk_unsafe_path')
    return raw


def _verified_input(root, pin, requirements, result):
    manifest_path = root / requirements['manifest_name']
    if not manifest_path.exists():
        _fail('sdk_manifest_missing')
    raw = _read_regular(manifest_path, 128 * 1024)
    digest = hashlib.sha256(raw).hexdigest()
    result['manifest_sha256'] = digest
    if digest != pin:
        _fail('sdk_manifest_pin_mismatch')
    manifest = json.loads(raw)
    if (manifest.get('schema_version') != 1 or manifest.get('contract') != requirements['contract']
            or manifest.get('protocol') != requirements['protocol'] or manifest.get('dependencies') != []
            or manifest.get('source', {}).get('repository') != requirements['source_repository']
            or manifest.get('source', {}).get('commit') != requirements['source_commit']
            or manifest.get('absent_optional_modules') != requirements['absent_optional_modules']
            or manifest.get('license') != 'MIT'):
        _fail('sdk_manifest_invalid')
    files, modules = manifest['files'], manifest['modules']
    if not isinstance(files, dict) or not 3 <= len(files) <= 128 or not isinstance(modules, dict):
        _fail('sdk_manifest_invalid')
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or '\\' in name:
            _fail('sdk_unsafe_path')
    actual = set()
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(directory) / name
            if path.is_symlink():
                _fail('sdk_unsafe_path')
            if name in names:
                actual.add(path.relative_to(root).as_posix())
    if actual != set(files) | {requirements['manifest_name']}:
        _fail('sdk_inventory_mismatch')
    sources = {}
    for name, entry in files.items():
        raw = _read_regular(root / name, 1024 * 1024)
        if (len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']
                or hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != entry['git_blob_sha1']):
            _fail('sdk_source_changed')
        sources[name] = raw
    expected_modules = {}
    for path in files:
        if path.startswith('arl/') and path.endswith('.py'):
            name = path[:-3].replace('/', '.')
            if name.endswith('.__init__'):
                name = name[:-9]
            expected_modules[name] = path
    if modules != expected_modules or not set(requirements['modules']) <= set(modules):
        _fail('sdk_manifest_invalid')
    if not {'LICENSE', 'NOTICE', 'pyproject.toml'} <= set(files):
        _fail('sdk_manifest_invalid')
    result.update(source_commit=manifest['source']['commit'], sdk_version=manifest['sdk_version'])
    return manifest, sources


class _PinnedSource(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root, manifest, sources):
        self.root, self.manifest, self.sources = root, manifest, sources

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'arl' and not fullname.startswith('arl.'):
            return None
        relative = self.manifest['modules'].get(fullname)
        if relative is None:
            if fullname in self.manifest['absent_optional_modules']:
                # The upstream public defaults explicitly handle this absence.
                # Raising here prevents any installed/global adapter fallback.
                raise ModuleNotFoundError(fullname, name=fullname)
            _fail('sdk_module_not_pinned')
        return importlib.util.spec_from_loader(fullname, self, origin=str(self.root / relative),
                                               is_package=relative.endswith('/__init__.py'))

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        relative = self.manifest['modules'][module.__name__]
        module.__file__ = str(self.root / relative)
        exec(compile(self.sources[relative], module.__file__, 'exec'), module.__dict__)


class _ImportGuard:
    """Prevent effects while evaluating the already hash-verified modules."""
    def __init__(self, result, root):
        self.result, self.active = result, False
        # Stdlib extension modules are legitimate imports, not SDK binaries.
        self.read_roots = tuple({str(Path(sysconfig.get_path('stdlib')).resolve()),
                                 str(Path(sysconfig.get_config_var('DESTSHARED') or sysconfig.get_path('stdlib')).resolve()),
                                 str(root)})

    def deny(self, event):
        self.result['denied_events'].append(event)
        _fail('sdk_side_effect_attempt')

    def audit(self, event, args):
        if not self.active:
            return
        if (event.startswith(('socket.', 'subprocess.', 'shutil.')) or event in {
                'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.exec', 'os.spawn',
                'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.link', 'os.symlink', 'os.truncate',
                'os.chmod', 'os.chown', 'os.utime', 'os.chdir', 'os.putenv', 'os.unsetenv',
                'sqlite3.connect', 'ctypes.dlsym'}):
            self.deny(event)
        if event == 'ctypes.dlopen' and args[0] is not None:
            self.deny(event)
        if event == 'open':
            path, mode, flags = args
            if (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                    or isinstance(mode, str) and any(flag in mode for flag in 'wax+')):
                self.deny('file.write')
            if not isinstance(path, (str, bytes, os.PathLike)):
                self.deny('file.descriptor_read')
            resolved = os.path.realpath(os.fsdecode(path))
            if not any(resolved == root or resolved.startswith(root + os.sep) for root in self.read_roots):
                self.deny('file.read_outside_sdk_or_stdlib')

    def profile(self, frame, event, arg):
        if event == 'call' and frame.f_globals.get('__name__', '').startswith('arl.'):
            qualified = frame.f_code.co_qualname.split('.')
            owner = qualified[0]
            if len(qualified) > 1 and owner in {'Client', 'ApplicationSession', 'Runtime', 'RuntimeLauncher', 'CodexService', 'CodexAdapter'}:
                self.deny('sdk.call.' + owner + '.' + frame.f_code.co_name)

    def __enter__(self):
        self.old_profile, self.old_thread_start = sys.getprofile(), threading.Thread.start
        self.old_raw_thread_start = _thread.start_new_thread
        self.old_bytecode = sys.dont_write_bytecode
        self.before_threads = {thread.ident for thread in threading.enumerate()}
        sys.addaudithook(self.audit)
        sys.dont_write_bytecode = True
        threading.Thread.start = lambda *args, **kwargs: self.deny('thread.start')
        _thread.start_new_thread = lambda *args, **kwargs: self.deny('thread.start_raw')
        self.active = True
        sys.setprofile(self.profile)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.active = False
        sys.setprofile(self.old_profile)
        threading.Thread.start = self.old_thread_start
        _thread.start_new_thread = self.old_raw_thread_start
        sys.dont_write_bytecode = self.old_bytecode
        self.result['new_threads'] = len({thread.ident for thread in threading.enumerate()} - self.before_threads)


def _attribute(value, dotted):
    for component in dotted.split('.'):
        value = inspect.getattr_static(value, component)
    return value


def check_sdk(*, vendor_root=None, expected_manifest_sha256=None):
    """Inspect one frozen payload; never consult an ARL host or user config."""
    requirements = json.loads(Path(__file__).with_name('arl_sdk_requirements.json').read_text())
    result = dict(compatible=False, code='sdk_unavailable', contract=requirements['contract'],
                  protocol=requirements['protocol'], modules={}, denied_events=[], new_threads=0,
                  registration_attempted=False, model_attempted=False)
    try:
        if any(name == 'arl' or name.startswith('arl.') for name in sys.modules) or threading.active_count() != 1:
            _fail('sdk_probe_requires_fresh_process')
        if vendor_root is None:
            spec = importlib.machinery.PathFinder.find_spec('arl', sys.path)
            if spec is None or not spec.origin:
                _fail('sdk_not_found')
            root = Path(spec.origin).absolute().parent.parent
        else:
            root = Path(vendor_root).absolute()
        manifest, sources = _verified_input(root, expected_manifest_sha256 or requirements['manifest_sha256'], requirements, result)
        finder = _PinnedSource(root, manifest, sources)
        sys.meta_path.insert(0, finder)
        try:
            with _ImportGuard(result, root):
                loaded = {name: importlib.import_module(name) for name in requirements['modules']}
                for name, rules in requirements['modules'].items():
                    module = loaded[name]
                    for field, value in rules.get('values', {}).items():
                        if _attribute(module, field) != value:
                            _fail('sdk_interface_mismatch')
                    for field in rules.get('classes', []):
                        if not inspect.isclass(_attribute(module, field)):
                            _fail('sdk_interface_mismatch')
                    for field, parameters in rules.get('callables', {}).items():
                        function = _attribute(module, field)
                        if not callable(function) or not set(parameters) <= set(inspect.signature(function).parameters):
                            _fail('sdk_interface_mismatch')
                for name, module in tuple(sys.modules.items()):
                    if name == 'arl' or name.startswith('arl.'):
                        relative = manifest['modules'].get(name)
                        if relative is None or module.__loader__ is not finder:
                            _fail('sdk_module_not_pinned')
                        result['modules'][name] = dict(path=relative, sha256=manifest['files'][relative]['sha256'], pinned=True)
        finally:
            sys.meta_path.remove(finder)
        if result['denied_events'] or result['new_threads']:
            _fail('sdk_side_effect_attempt')
        result.update(compatible=True, code='compatible')
    except _Refused as error:
        result['code'] = 'sdk_side_effect_attempt' if result['denied_events'] else error.code
    except (AttributeError, TypeError, ValueError, KeyError):
        result['code'] = 'sdk_interface_mismatch' if 'finder' in locals() else 'sdk_manifest_invalid'
    except (ImportError, OSError, RuntimeError, SyntaxError):
        result['code'] = 'sdk_side_effect_attempt' if result['denied_events'] else 'sdk_import_failed'
    if result['denied_events']:
        result.update(compatible=False, code='sdk_side_effect_attempt')
    return result
