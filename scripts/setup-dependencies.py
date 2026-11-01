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
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MILOCO_VERSION = 'v2026.10.16'
HA_VERSION = '2026.10.4'
XIAOMI_HOME_VERSION = 'v0.5.0'
XIAOMI_HOME_COMMIT = '75b3acb6325a3a8d295688b13904737d6738a6a5'


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
            'xiaomi_home': {'version': XIAOMI_HOME_VERSION, 'commit': XIAOMI_HOME_COMMIT,
                            'upgrade': 'explicit offline upgrade; existing configuration is retained'},
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


def private_directory(directory):
    directory = directory.absolute()
    if (directory.is_symlink() or (ROOT / 'data').is_symlink()
            or not directory.resolve().is_relative_to((ROOT / 'data').resolve())
            or any(parent.is_symlink() for parent in directory.parents if parent != ROOT)):
        raise ValueError('deployment_directory_must_be_under_private_data')
    return directory


def xiaomi_source():
    """Fetch only the reviewed release; a tag alone is not content verification."""
    target = private_directory(ROOT / 'data/upstream' / ('ha-xiaomi-home-' + XIAOMI_HOME_VERSION))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', XIAOMI_HOME_VERSION,
                        'https://github.com/XiaoMi/ha_xiaomi_home.git', str(target)], check=True)
    if not (target / '.git').is_dir() or (target / '.git').is_symlink():
        raise ValueError('xiaomi_source_not_a_reviewed_checkout')
    commit = subprocess.run(['git', '-C', str(target), 'rev-parse', 'HEAD'], check=True,
                            capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(target), 'status', '--porcelain'], check=True,
                           capture_output=True, text=True).stdout.strip()
    component = target / 'custom_components/xiaomi_home'
    manifest = json.loads((component / 'manifest.json').read_text())
    if (commit != XIAOMI_HOME_COMMIT or dirty or component.is_symlink()
            or any(path.is_symlink() for path in component.rglob('*'))
            or manifest.get('domain') != 'xiaomi_home' or manifest.get('version') != XIAOMI_HOME_VERSION):
        raise ValueError('xiaomi_source_verification_failed')
    return component


def require_ha_stopped(directory, container, docker='docker', docker_sudo=False):
    """A private local bind mount and stopped exact container are both required."""
    if not container:
        raise ValueError('explicit_ha_container_required_for_upgrade')
    command = (['sudo', '-n'] if docker_sudo else []) + [docker, 'inspect', container]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
    records = json.loads(result.stdout)
    if len(records) != 1 or records[0].get('State', {}).get('Running') is not False:
        raise ValueError('stop_home_assistant_before_upgrade')
    config = directory / 'homeassistant'
    mounts = [mount for mount in records[0].get('Mounts', []) if mount.get('Destination') == '/config']
    if (len(mounts) != 1 or mounts[0].get('Type') != 'bind'
            or Path(mounts[0].get('Source', '')).resolve() != config.resolve()):
        raise ValueError('ha_container_config_mount_mismatch')
    return records[0]['Id']


def install_xiaomi(directory, *, upgrade=False, container=None, docker='docker', docker_sudo=False):
    directory = private_directory(directory)
    destination = directory / 'homeassistant/custom_components/xiaomi_home'
    private_directory(destination)
    if destination.exists() and not upgrade:
        raise ValueError('existing_xiaomi_home_requires_explicit_offline_upgrade')
    if upgrade and not destination.is_dir():
        raise ValueError('existing_xiaomi_home_required_for_upgrade')
    if upgrade:
        require_ha_stopped(directory, container, docker, docker_sudo)
    component = xiaomi_source()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Stage a complete component first. Account and registry files are outside
    # this directory and are never rewritten by this installer.
    with tempfile.TemporaryDirectory(prefix='.xiaomi-stage-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'xiaomi_home'
        shutil.copytree(component, staged)
        backup = None
        previous = None
        if upgrade:
            previous = json.loads((destination / 'manifest.json').read_text()).get('version')
            require_ha_stopped(directory, container, docker, docker_sudo)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = directory / 'integration-backups' / stamp / 'xiaomi_home'
            backup.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            destination.rename(backup)
        try:
            staged.rename(destination)
        except BaseException:
            if backup is not None and backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
    return {'installed': True, 'version': XIAOMI_HOME_VERSION, 'commit': XIAOMI_HOME_COMMIT,
            'previous_version': previous, 'backup': str(backup) if backup else None,
            'restart_home_assistant': True, 'account_login_required': not upgrade,
            'configuration_retained': upgrade, 'services_started': False}


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'prepare', 'ha-up', 'ha-down', 'xiaomi-home',
                                           'xiaomi-home-source', 'xiaomi-home-upgrade', 'miloco-source'],
                        nargs='?', default='plan')
    parser.add_argument('--project', type=Path, default=ROOT, help='Friday source root; required for downloaded copies')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--ha-container', help='Exact existing HA container ID or name; upgrade only')
    parser.add_argument('--docker-executable', default='docker')
    parser.add_argument('--docker-sudo', action='store_true')
    args = parser.parse_args()
    ROOT = args.project.resolve()
    if not (ROOT / 'homeplane').is_dir():
        parser.error('Friday source root required; set --project to your existing installation')
    directory = (args.directory or ROOT / 'data/deployment').absolute()
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
    elif args.operation == 'xiaomi-home-source':
        xiaomi_source()
        result = {'downloaded': True, 'version': XIAOMI_HOME_VERSION, 'commit': XIAOMI_HOME_COMMIT,
                  'installed': False, 'services_started': False}
    elif args.operation in {'xiaomi-home', 'xiaomi-home-upgrade'}:
        try:
            result = install_xiaomi(directory, upgrade=args.operation == 'xiaomi-home-upgrade',
                                    container=args.ha_container, docker=args.docker_executable,
                                    docker_sudo=args.docker_sudo)
        except (ValueError, OSError, subprocess.SubprocessError):
            parser.error('Xiaomi Home dependency update refused; verify source, private path, stopped container and backup')
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
