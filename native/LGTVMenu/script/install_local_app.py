#!/usr/bin/env python3
"""Install the already-built local app with signature validation and a retained backup.

Does not install the HAL component or change preferences. Run build_and_run.sh
--build-only first. A sandboxed runner needs permission to write Applications.
"""
from pathlib import Path
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import time
import uuid

repo = Path(__file__).resolve().parents[3]
staged = repo / '.artifacts/lg-tv-menu/LG TV.app'
installed = Path('/Applications/LG TV.app')
output = repo / '.artifacts/lg-tv-menu/install-receipts'
stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]

def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True)

def requirement(path):
    result = run('codesign', '-d', '-r-', str(path))
    return next(line for line in (result.stdout + '\n' + result.stderr).splitlines()
                if line.startswith('designated =>'))

run('codesign', '--verify', '--deep', '--strict', str(staged))
info = plistlib.loads((staged / 'Contents/Info.plist').read_bytes())
assert info['CFBundleIdentifier'] == 'local.friday.LGTVMenu'
if installed.exists():
    assert requirement(installed) == requirement(staged), 'Signing identity changed; retain current app'
fresh = installed.with_name('.LG-TV-update-' + uuid.uuid4().hex + '.app')
run('ditto', str(staged), str(fresh))
run('codesign', '--verify', '--deep', '--strict', str(fresh))
# Stop only processes executing this exact installed app, not similarly named tools.
processes = subprocess.run(['pgrep', '-x', 'LGTVMenu'], capture_output=True, text=True)
for pid in processes.stdout.split():
    command = subprocess.run(['ps', '-p', pid, '-o', 'comm='], capture_output=True, text=True).stdout.strip()
    if command != str(installed / 'Contents/MacOS/LGTVMenu'):
        continue
    os.kill(int(pid), signal.SIGTERM)
    for _ in range(60):
        try: os.kill(int(pid), 0)
        except ProcessLookupError: break
        time.sleep(0.05)
    else: raise RuntimeError('App did not exit; retain current installation')
backup = output / stamp / 'previous/LG TV.app'
if installed.exists():
    backup.parent.mkdir(parents=True)
    installed.rename(backup)
try:
    fresh.rename(installed)
except BaseException:
    if backup.exists() and not installed.exists(): backup.rename(installed)
    raise
run('codesign', '--verify', '--deep', '--strict', str(installed))
digest = lambda p: hashlib.sha256((p / 'Contents/MacOS/LGTVMenu').read_bytes()).hexdigest()
assert digest(staged) == digest(installed)
receipt = dict(installed_build=info['CFBundleVersion'], binary_sha256=digest(installed),
               signature_verified=True, pairing_settings_modified=False, audio_component_install_attempted=False)
output.mkdir(parents=True, exist_ok=True)
(output / (stamp + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
run('open', '-n', str(installed))
print(json.dumps(receipt))
