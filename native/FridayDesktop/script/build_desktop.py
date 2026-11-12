"""Build a local development App from current source, never install or launch.

Default: show plan. --build-only compiles Swift and packages a launcher for the
selected local Python environment. This App depends on that environment; a
redistributable Python runtime/notarized release is a separate packaging task.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NATIVE = REPO / 'native/FridayDesktop'
SOUND_HELPER = 'Helpers/friday-sound'


def project_cache_root():
    slug = REPO.name + '-' + hashlib.sha256(str(REPO.resolve()).encode()).hexdigest()[:12]
    return Path.home() / 'Library/Caches/CodexBuilds.noindex' / slug


def output_path(path):
    destination = path.absolute()
    if (destination.exists() or destination.is_symlink() or destination.suffix != '.app'
            or not any(destination.resolve().is_relative_to(root.resolve())
                       for root in (REPO / '.artifacts', project_cache_root()))
            or any(parent.is_symlink() for parent in destination.parents)):
        raise ValueError('output must be a new .app under project .artifacts or its own build cache without symlinks')
    return destination


def public_sources():
    # Use exactly the publication inventory; never package ignored local files.
    sys.path.insert(0, str(REPO / 'scripts'))
    from audit_publication import scan
    result = scan(REPO)
    if result['findings']:
        raise ValueError('publication audit must pass before packaging')
    return {name: digest for name, digest in result['records'].items()
            if Path(name).parts[0] in {'homeplane', 'friday', 'fixtures', 'schemas', 'workers'}}


def runtime_checks():
    return (('source_import', NATIVE/'tests/check_packaged_import.py',
             'LOCAL_DEVELOPMENT_APP_SOURCE_IMPORT_BINDING_PASS'),
            ('application_sdk', NATIVE/'tests/check_packaged_application_sdk.py',
             'LOCAL_DEVELOPMENT_APP_APPLICATION_SDK_PASS'))


def frozen_sdk_input(args, records):
    """Copy only the reviewed dependency input pinned by the product contract.

    This checks packaging inventory, not SDK interfaces. The owning adapter's
    separate import-only gate validates those in the selected runtime.
    """
    source = getattr(args, 'arl_sdk_input', None)
    if source is None:
        raise ValueError('provide a reviewed frozen SDK with --arl-sdk-input')
    source = Path(source).absolute()

    def read(path, limit=16 * 1024 * 1024):
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise ValueError('SDK input must contain only regular files without symlinks')
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValueError('invalid SDK input file')
            return stream.read(limit + 1)

    contract_name = 'homeplane/arl_sdk_requirements.json'
    raw = read(REPO/contract_name, 128 * 1024)
    if hashlib.sha256(raw).hexdigest() != records.get(contract_name):
        raise ValueError('SDK contract must match the publication inventory')
    contract = json.loads(raw)
    name = contract['manifest_name']
    if Path(name).name != name or name in {'', '.', '..'}:
        raise ValueError('invalid SDK manifest path')
    raw = read(source/name, 128 * 1024)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != contract['manifest_sha256']:
        raise ValueError('SDK input differs from the reviewed manifest')
    files = json.loads(raw)['files']
    if not isinstance(files, dict) or not 1 <= len(files) <= 4096 or name in files:
        raise ValueError('invalid SDK inventory')
    expected = {name: digest}
    for relative, row in files.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or path.as_posix() != relative or relative in {'', '.'}:
            raise ValueError('invalid SDK input path')
        digest = hashlib.sha256(read(source/path)).hexdigest()
        if digest != row['sha256']:
            raise ValueError('SDK source differs from the reviewed manifest')
        expected[relative] = digest
    actual = set()
    for path in source.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('SDK input contains an unsafe entry')
        if path.is_file():
            actual.add(path.relative_to(source).as_posix())
    if actual != set(expected):
        raise ValueError('SDK input contains missing or unreviewed files')
    return source, expected


def validate_runtime(args, destination, area, run):
    validation = area/'validation'
    validation.mkdir()
    results = {}
    for name, script, phase in runtime_checks():
        output = validation/(name+'.json')
        run([str(args.python), '-I', '-B', str(script), str(destination), str(output)], timeout=30)
        if output.is_symlink() or output.stat().st_size > 1024 * 1024:
            raise ValueError('invalid runtime validation receipt')
        report = json.loads(output.read_text())
        if not isinstance(report, dict) or report.get('passed') is not True or report.get('phase') != phase:
            raise ValueError('runtime validation did not pass')
        results[name] = {'passed': True, 'report': str(output),
                         'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
    return results


def package(args, destination, records, area):
    sound_source = NATIVE.parent / 'EmbodiedVision/SoundWorker.swift'
    native_inputs = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in NATIVE.rglob('*.swift') if not any(x in p.parts for x in ('build', '.build', 'tests'))}
    native_inputs[sound_source.relative_to(REPO).as_posix()] = hashlib.sha256(sound_source.read_bytes()).hexdigest()
    build_inputs = {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (NATIVE/'Package.swift', NATIVE/'backend/desktop_backend.py',
                                 NATIVE/'Resources/product-inputs.json',
                                 NATIVE/'Resources/com.friday.household.service.plist',
                                 NATIVE/'Resources/AppIcon/Friday.icns', Path(__file__),
                                 NATIVE/'script/build_and_run.sh', NATIVE/'script/install_desktop.py',
                                 *(script for _, script, _ in runtime_checks()))}
    value = json.loads((NATIVE / 'Resources/product-inputs.json').read_text())
    manifest = area / 'manifest.json'
    state = {'source_project': str(REPO), 'toolchain': 'SwiftPM macOS release',
             'purpose': 'Source-bound local development App', 'task_id': args.task_id,
             'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'status': 'building', 'pid': os.getpid(), 'commands': [], 'output': str(destination),
             'retention': 'Remove compiled caches/candidate after installed readback; retain small logs and receipts.'}
    manifest.write_text(json.dumps(state, indent=2) + '\n')
    temporary = area / 'temporary'
    temporary.mkdir()
    env = {**os.environ, 'TMPDIR': str(temporary), 'PYTHONDONTWRITEBYTECODE': '1',
           'CLANG_MODULE_CACHE_PATH': str(area/'clang-cache'),
           'SWIFTPM_MODULECACHE_OVERRIDE': str(area/'module-cache')}

    def run(command, *, output=False, timeout=None):
        print('build stage: ' + command[0] + (' ' + command[1] if len(command) > 1 else ''), flush=True)
        entry = {'command': command, 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        state['commands'].append(entry)
        manifest.write_text(json.dumps(state, indent=2) + '\n')
        with (area/'build.log').open('a') as log:
            result = subprocess.run(command, env=env, stdout=subprocess.PIPE if output else log,
                                    stderr=log, text=True, timeout=timeout)
        entry.update(returncode=result.returncode, ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        manifest.write_text(json.dumps(state, indent=2) + '\n')
        result.check_returncode()
        return result.stdout

    try:
        sdk_root, sdk_records = frozen_sdk_input(args, records)
        state['sdk_input'] = {'path': str(sdk_root), 'records': sdk_records}
        run([str(args.python), '-I', '-B', '-c', 'import fastapi, httpx, pydantic, zeroconf, websockets'])
        command = ['xcrun', 'swift', 'build', '--package-path', str(NATIVE),
                   '--scratch-path', str(area/'swift-build'), '--cache-path', str(area/'swift-cache'),
                   '--config-path', str(area/'swift-config'), '--security-path', str(area/'swift-security'),
                   '--disable-automatic-resolution', '-Xswiftc', '-module-cache-path',
                   '-Xswiftc', str(area/'module-cache'), '-c', 'release']
        run(command)
        binary = Path(run([*command, '--show-bin-path'], output=True).strip()) / 'Friday'
        if not binary.is_file() or not binary.resolve().is_relative_to(area):
            raise ValueError('compiler output is outside this build area')
        sound_binary = area / 'friday-sound'
        run(['xcrun', 'swiftc', '-O', '-target', f'{platform.machine()}-apple-macosx14.0',
             '-module-cache-path', str(area/'module-cache'), str(sound_source), '-o', str(sound_binary)])
        assemble(args, destination, records, native_inputs, build_inputs, binary, value,
                 sdk_root=sdk_root, sdk_records=sdk_records, sound_binary=sound_binary)
        state['runtime_validation'] = validate_runtime(args, destination, area, run)
        for name, expected in {**records, **native_inputs, **build_inputs}.items():
            if hashlib.sha256((REPO/name).read_bytes()).hexdigest() != expected:
                raise ValueError('source changed during build; candidate cannot be installed')
        if frozen_sdk_input(args, records) != (sdk_root, sdk_records):
            raise ValueError('SDK input changed during build; candidate cannot be installed')
        # 先签包内程序，记录签名后的字节，再签外层 App。
        sound_helper = destination / 'Contents' / SOUND_HELPER
        run(['codesign', '--force', '--sign', args.signing_identity,
             '--identifier', value['bundle_identifier'] + '.sound-worker', str(sound_helper)])
        sound_digest = hashlib.sha256(sound_helper.read_bytes()).hexdigest()
        binding_path = destination / 'Contents/Resources/FRIDAY-SOURCE-INTEGRATION.json'
        binding = json.loads(binding_path.read_text())
        binding['packaged_inputs'][SOUND_HELPER] = sound_digest
        binding_path.write_text(json.dumps(binding, indent=2))
        run(['codesign', '--force', '--sign', args.signing_identity, str(destination)])
        run(['codesign', '--verify', '--deep', '--strict', str(destination)])
        state.update(status='verified', actual_binary=str(binary.resolve()),
                     compiled_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                     signed_binary_sha256=hashlib.sha256((destination/'Contents/MacOS/Friday').read_bytes()).hexdigest(),
                     sound_helper_sha256=sound_digest, source_files=len(records))
    except BaseException:
        state['status'] = 'failed'
        raise
    finally:
        state['ended_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest.write_text(json.dumps(state, indent=2) + '\n')
        log = area/'build.log'
        if log.exists() and log.stat().st_size > 200000:
            log.write_bytes(log.read_bytes()[-200000:])


def assemble(args, destination, records, native_inputs, build_inputs, binary, value, *,
             sdk_root, sdk_records, sound_binary):
    contents = destination / 'Contents'
    resources = contents / 'Resources'
    (contents / 'MacOS').mkdir(parents=True, exist_ok=False)
    resources.mkdir()
    shutil.copy2(binary, contents / 'MacOS/Friday')
    sound_helper = contents / SOUND_HELPER
    sound_helper.parent.mkdir()
    shutil.copy2(sound_binary, sound_helper)
    backend = resources / 'backend'
    backend.mkdir()
    shutil.copy2(NATIVE / 'backend/desktop_backend.py', backend)
    product = resources / 'product'
    for relative in records:
        target = product / 'app' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != records[relative]:
            raise ValueError('packaged source differs from publication inventory')
    vendor = product/'vendor'
    for relative, expected in sdk_records.items():
        target = vendor/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sdk_root/relative, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError('packaged SDK differs from the reviewed input')
    runtime = product / 'runtime/bin'
    runtime.mkdir(parents=True)
    launcher = runtime / 'python3'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(args.python.absolute())) + ' "$@"\n')
    launcher.chmod(0o755)
    shutil.copy2(NATIVE / 'Resources/AppIcon/Friday.icns', resources / 'Friday.icns')
    agents = contents / 'Library/LaunchAgents'
    agents.mkdir(parents=True)
    shutil.copy2(NATIVE / 'Resources/com.friday.household.service.plist', agents)
    info = {'CFBundleIdentifier': value['bundle_identifier'], 'CFBundleName': 'Friday',
            'CFBundleExecutable': 'Friday', 'CFBundlePackageType': 'APPL',
            'CFBundleShortVersionString': value['version'], 'CFBundleVersion': value['build'],
            'CFBundleIconFile': 'Friday.icns', 'LSMinimumSystemVersion': '14.0',
            'NSLocalNetworkUsageDescription': '连接你选择的家庭服务。',
            'NSMicrophoneUsageDescription': '仅在你开启语音功能后接收指令。'}
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    packaged_inputs = {str(path.relative_to(contents)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (backend/'desktop_backend.py', launcher, contents/'Info.plist',
                                    agents/'com.friday.household.service.plist', resources/'Friday.icns')}
    packaged_inputs.update({'Resources/product/vendor/'+name: digest for name, digest in sdk_records.items()})
    (resources / 'FRIDAY-SOURCE-INTEGRATION.json').write_text(json.dumps(
        {'schema': 3, 'records': records, 'native_inputs': native_inputs,
         'build_inputs': build_inputs, 'packaged_inputs': packaged_inputs,
         'runtime_python': str(args.python.absolute())}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--python', type=Path, default=REPO / '.venv/bin/python')
    parser.add_argument('--arl-sdk-input', type=Path,
                        help='reviewed frozen SDK directory with the manifest pinned by the product contract')
    parser.add_argument('--output', type=Path, default=REPO / '.artifacts/desktop-public/Friday.app')
    parser.add_argument('--signing-identity', default='-')
    parser.add_argument('--task-id', default=datetime.datetime.now(datetime.timezone.utc).strftime('local-%Y%m%dT%H%M%SZ'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,120}', args.task_id):
        parser.error('task-id must contain only letters,digits,hyphens or underscores')
    area = project_cache_root() / 'swift-macos/release' / args.task_id
    value = json.loads((NATIVE / 'Resources/product-inputs.json').read_text())
    plan = {'version': value['version'], 'build': value['build'],
            'python_available': args.python.is_file(), 'swift_available': shutil.which('xcrun') is not None,
            'arl_sdk_input_available': args.arl_sdk_input is not None and args.arl_sdk_input.is_dir(),
            'packaging': 'local-development-runtime', 'redistributable': False,
            'install_or_launch': False, 'personal_signing_identity_required': False, 'build_area': str(area)}
    if not args.build_only:
        print(json.dumps(plan, indent=2))
        return
    if sys.platform != 'darwin' or not args.python.is_file():
        parser.error('macOS, Xcode Command Line Tools and an installed Friday Python environment are required')
    try:
        destination = output_path(args.output)
        records = public_sources()
        if area.exists() or any(parent.is_symlink() for parent in area.parents):
            raise ValueError('choose a new task-id without symlink parents')
        area.mkdir(parents=True, exist_ok=False)
    except ValueError as exc:
        parser.error(str(exc))
    with (area/'running.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        package(args, destination, records, area)
        fcntl.flock(lock, fcntl.LOCK_UN)
    print(json.dumps({**plan, 'built': True, 'source_files': len(records), 'signature_verified': True,
                      'output': str(destination)}, indent=2))


if __name__ == '__main__':
    main()
