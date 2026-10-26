#!/usr/bin/env python3
"""Prepare reproducible, private dependency configuration; never logs credentials.

The default plan does not install/start anything. HA runs on Linux Docker Engine.
Miloco is separately installed from its pinned upstream source for an explicitly
selected supported agent platform. Account login remains in the upstream UI.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MILOCO_VERSION = 'v2026.10.16'
HA_VERSION = '2026.10.4'
XIAOMI_HOME_VERSION = 'v0.4.3'


def plan():
    configured = {'ha': bool(os.environ.get('HOME_HA_URL')), 'camera_service': False,
                  'miloco_source': (ROOT / 'data/upstream/miloco/.git').is_dir()}
    for path in (ROOT / 'data/homeplane/settings.json',
                 Path.home() / 'Library/Application Support/Friday/data/homeplane/settings.json'):
        try:
            if path.is_symlink() or path.stat().st_size > 32768:
                continue
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                continue
            configured['ha'] |= bool(value.get('ha_url'))
            configured['camera_service'] |= bool(value.get('camera_service_url'))
        except (OSError, ValueError, TypeError):
            continue
    versions = {}
    for name in ('uv', 'node', 'docker'):
        if not shutil.which(name):
            versions[name] = None
            continue
        try:
            result = subprocess.run([name, '--version'], capture_output=True, text=True, timeout=3)
            versions[name] = result.stdout.strip()[:160] if result.returncode == 0 else 'unavailable'
        except (OSError, subprocess.TimeoutExpired):
            versions[name] = 'unavailable'
    return {'python': platform.python_version(), 'os': platform.system(),
            'tools': {name: shutil.which(name) is not None for name in ('uv', 'node', 'docker', 'git')},
            'tool_versions': versions, 'existing_configuration': configured,
            'service_connectivity': 'not_probed; inspect configured service in its local UI before creating another instance',
            'home_assistant': {'version': HA_VERSION, 'requires': 'Linux Docker Engine or an existing HA instance',
                               'configuration': 'data/deployment/compose.yaml'},
            'miloco': {'version': MILOCO_VERSION, 'upstream': 'https://github.com/XiaoMi/xiaomi-miloco',
                       'agent_platforms': ['openclaw', 'hermes'], 'login': 'upstream UI'},
            'device_discovery': 'HA area/device/entity registries; no model or room-name allowlist',
            'layout': 'import your own Mi Home BakeData or floormap JSON; absent until imported',
            'services_started': False, 'credentials_collected': False}


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value)


def prepare(directory):
    if ((ROOT / 'data').is_symlink() or directory.is_symlink()
            or not directory.resolve().is_relative_to((ROOT / 'data').resolve())
            or any(parent.is_symlink() for parent in directory.parents if parent != ROOT)):
        raise ValueError('deployment_directory_must_be_under_private_data')
    compose = f'''services:
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:{HA_VERSION}
    restart: unless-stopped
    stop_grace_period: 60s
    network_mode: host
    environment:
      TZ: ${{TZ:-UTC}}
    volumes:
      - ./homeassistant:/config
'''
    private_write(directory / 'compose.yaml', compose)
    private_write(directory / '.env', 'TZ=UTC\n')
    return {'prepared': True, 'services_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'prepare', 'ha-up', 'ha-down', 'xiaomi-home', 'miloco-source'], nargs='?', default='plan')
    parser.add_argument('--directory', type=Path, default=ROOT / 'data/deployment')
    args = parser.parse_args()
    directory = args.directory.absolute()
    if args.operation == 'plan':
        result = plan()
    elif args.operation == 'prepare':
        result = prepare(directory)
    elif args.operation in {'ha-up', 'ha-down'}:
        if platform.system() != 'Linux':
            parser.error('Home Assistant Container requires Linux Docker Engine; on macOS use a Linux VM or existing HA')
        if directory.is_symlink() or not directory.resolve().is_relative_to((ROOT / 'data').resolve()):
            parser.error('deployment directory must be under private data')
        command = ['docker', 'compose', '-f', str(directory / 'compose.yaml'), '--env-file', str(directory / '.env')]
        subprocess.run(command + (['up', '-d'] if args.operation == 'ha-up' else ['down']), check=True)
        result = {'completed': True, 'operation': args.operation, 'account_login_required': args.operation == 'ha-up'}
    elif args.operation == 'xiaomi-home':
        target = ROOT / 'data/upstream/ha-xiaomi-home'
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            parser.error('upstream directory already exists; review it before updating')
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', XIAOMI_HOME_VERSION,
                        'https://github.com/XiaoMi/ha_xiaomi_home.git', str(target)], check=True)
        destination = directory / 'homeassistant/custom_components/xiaomi_home'
        if destination.exists():
            parser.error('existing Xiaomi Home integration is never overwritten')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target / 'custom_components/xiaomi_home', destination)
        result = {'installed': True, 'restart_home_assistant': True, 'account_login_required': True}
    else:
        target = ROOT / 'data/upstream/miloco'
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            parser.error('upstream directory already exists; review it before updating')
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', MILOCO_VERSION,
                        'https://github.com/XiaoMi/xiaomi-miloco.git', str(target)], check=True)
        result = {'downloaded': True, 'installed': False, 'services_started': False,
                  'next': 'Read data/upstream/miloco/scripts/install-guide.md for your agent platform'}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
