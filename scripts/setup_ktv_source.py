"""Install the locked KTV source API dependency without lifecycle scripts or a server."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / 'data/ktv/provider'
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(ROOT / 'integrations/ktv_source' / name, destination / name)
    subprocess.run(['npm', 'ci', '--prefix', str(destination), '--ignore-scripts', '--no-audit', '--no-fund'], check=True)
    manifest = json.loads((destination / 'node_modules/@neteasecloudmusicapienhanced/api/package.json').read_text())
    if manifest['version'] != '4.41.1':
        raise RuntimeError('source_api_version_mismatch')
    print(json.dumps({'installed': True, 'version': manifest['version'], 'server_started': False, 'credentials_read': False}))


if __name__ == '__main__':
    main()
