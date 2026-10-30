"""Explicit, reversible local development install. Never launches the App."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--candidate', required=True, type=Path)
parser.add_argument('--destination', type=Path, default=Path.home() / 'Applications/Friday.app')
parser.add_argument('--backup', type=Path)
args = parser.parse_args()
source, target = args.candidate.absolute(), args.destination.absolute()
if source.is_symlink() or target.is_symlink() or source.name != 'Friday.app':
    parser.error('real Friday.app directories are required')
subprocess.run(['codesign', '--verify', '--strict', '--deep', str(source)], check=True)
if target.exists():
    if not args.backup or args.backup.exists():
        parser.error('existing App requires an explicit unused --backup destination; stop it before replacing')
    # Never replace a currently running App or its service.
    if subprocess.run(['pgrep', '-x', 'Friday'], capture_output=True).returncode != 1:
        parser.error('stop the existing Friday App and household service first')
    # The service execs Python, so its native executable name is insufficient.
    if subprocess.run(['launchctl', 'list', 'com.friday.household.service'], capture_output=True).returncode == 0:
        parser.error('unregister the identified Friday household service before replacing the App')
    args.backup.parent.mkdir(parents=True, exist_ok=True)
    os.rename(target, args.backup)
try:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    subprocess.run(['codesign', '--verify', '--strict', '--deep', str(target)], check=True)
except BaseException:
    if target.exists():
        shutil.rmtree(target)
    if args.backup and args.backup.exists():
        os.rename(args.backup, target)
    raise
print('Installed local development App; not launched.')
