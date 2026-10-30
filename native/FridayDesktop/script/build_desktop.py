"""Build a local development App from current source, never install or launch.

Default: show plan. --build-only compiles Swift and packages a launcher for the
selected local Python environment. This App depends on that environment; a
redistributable Python runtime/notarized release is a separate packaging task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
NATIVE = REPO / 'native/FridayDesktop'


def output_path(path):
    destination = path.absolute()
    if (destination.exists() or destination.is_symlink() or destination.suffix != '.app'
            or not destination.resolve().is_relative_to(REPO / '.artifacts')
            or any(parent.is_symlink() for parent in destination.parents)):
        raise ValueError('output must be a new .app directory under project .artifacts without symlinks')
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--python', type=Path, default=REPO / '.venv/bin/python')
    parser.add_argument('--output', type=Path, default=REPO / '.artifacts/desktop-public/Friday.app')
    parser.add_argument('--signing-identity', default='-')
    args = parser.parse_args()
    value = json.loads((NATIVE / 'Resources/product-inputs.json').read_text())
    plan = {'version': value['version'], 'build': value['build'],
            'python_available': args.python.is_file(), 'swift_available': shutil.which('xcrun') is not None,
            'packaging': 'local-development-runtime', 'redistributable': False,
            'install_or_launch': False, 'personal_signing_identity_required': False}
    if not args.build_only:
        print(json.dumps(plan, indent=2))
        return
    if sys.platform != 'darwin' or not args.python.is_file():
        parser.error('macOS, Xcode Command Line Tools and an installed Friday Python environment are required')
    try:
        destination = output_path(args.output)
        records = public_sources()
    except ValueError as exc:
        parser.error(str(exc))
    subprocess.run([str(args.python), '-c', 'import fastapi, httpx, pydantic, zeroconf, websockets'], check=True)
    scratch = REPO / '.artifacts/desktop-public/swift-build'
    subprocess.run(['xcrun', 'swift', 'build', '--package-path', str(NATIVE),
                    '--scratch-path', str(scratch), '-c', 'release'], check=True)
    contents = destination / 'Contents'
    resources = contents / 'Resources'
    (contents / 'MacOS').mkdir(parents=True)
    resources.mkdir()
    shutil.copy2(scratch / 'release/Friday', contents / 'MacOS/Friday')
    backend = resources / 'backend'
    backend.mkdir()
    shutil.copy2(NATIVE / 'backend/desktop_backend.py', backend)
    product = resources / 'product'
    for relative in records:
        target = product / 'app' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
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
    native_inputs = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in NATIVE.rglob('*.swift') if not any(x in p.parts for x in ('build', '.build', 'tests'))}
    build_inputs = {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (NATIVE/'backend/desktop_backend.py', NATIVE/'Resources/product-inputs.json',
                                 NATIVE/'Resources/com.friday.household.service.plist',
                                 NATIVE/'Resources/AppIcon/Friday.icns', Path(__file__),
                                 NATIVE/'script/install_desktop.py')}
    packaged_inputs = {str(path.relative_to(contents)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (backend/'desktop_backend.py', launcher, contents/'Info.plist',
                                    agents/'com.friday.household.service.plist', resources/'Friday.icns')}
    (resources / 'FRIDAY-SOURCE-INTEGRATION.json').write_text(json.dumps(
        {'schema': 3, 'records': records, 'native_inputs': native_inputs,
         'build_inputs': build_inputs, 'packaged_inputs': packaged_inputs,
         'runtime_python': str(args.python.absolute())}, indent=2))
    subprocess.run(['codesign', '--force', '--deep', '--sign', args.signing_identity, str(destination)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(destination)], check=True)
    print(json.dumps({**plan, 'built': True, 'source_files': len(records), 'signature_verified': True,
                      'output': str(destination)}, indent=2))


if __name__ == '__main__':
    main()
