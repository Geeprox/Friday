"""Ordinary-user NAS boot/recovery entry. Only its own verified children may be restarted."""
import argparse
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path


def owned(pid, script, config, *, proc=Path('/proc')):
    if type(pid) is not int or pid <= 1:
        return False
    process = proc / str(pid)
    try:
        command = (process / 'cmdline').read_bytes()[:8192].split(b'\0')
        at = command.index(b'--config')
        return process.stat().st_uid == os.getuid() and str(script).encode() in command and command[at+1] == str(config).encode()
    except (OSError, ValueError, IndexError):
        return False


def health(config):
    try:
        port = json.loads(config.read_text()).get('port',19089)
        if type(port) is not int or not 1024 <= port <= 65535:
            return False
        # No redirects or environment proxies; only the fixed private loopback health resource.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open('http://127.0.0.1:'+str(port)+'/health',timeout=2) as response:
            return json.loads(response.read(1024)).get('service') == 'friday-ktv-public'
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--ensure', action='store_true', help='Idempotent recovery for the scheduled guardian')
    args = parser.parse_args()
    config = args.config.absolute()
    if config.resolve() != config or config.stat().st_uid != os.getuid():
        raise ValueError('owned_config_required')
    os.umask(0o077)
    config.chmod(0o600)
    root = config.parent
    blocked = root / 'restart-blocked.json'
    if blocked.exists():
        if args.ensure:
            print(json.dumps({'status':'blocked','requires_manual_start':True}));return
        if blocked.is_symlink() or blocked.stat().st_uid != os.getuid():
            raise ValueError('owned_recovery_marker_required')
        # Only the explicit operator entry can rearm a repeatedly failing child.
        blocked.unlink()
    entry = Path(__file__).resolve().with_name('supervisor.py')
    receipt = root / 'process.json'
    value = {}
    if receipt.is_file() and not receipt.is_symlink() and receipt.stat().st_size < 1024:
        value = json.loads(receipt.read_text())
    supervisor = owned(value.get('supervisor_pid'),entry,config)
    server = owned(value.get('server_pid'),entry.with_name('nas_server.py'),config)
    if args.ensure and supervisor:
        if health(config):
            (root/'recovery.json').write_text(json.dumps({'at':int(time.time()),'status':'healthy','failures':0}))
            print(json.dumps({'status':'already_running'}));return
        previous = root/'recovery.json'
        failures = 0
        if previous.is_file() and not previous.is_symlink() and previous.stat().st_size < 1024:
            failures = json.loads(previous.read_text()).get('failures',0)
        failures = min(3,failures+1) if type(failures) is int else 1
        previous.write_text(json.dumps({'at':int(time.time()),'status':'recovering','failures':failures}))
        if not server or failures < 3:
            print(json.dumps({'status':'recovering'}));return
    if server:
        os.kill(value['server_pid'],signal.SIGTERM)
        if supervisor:
            print(json.dumps({'status':'own_child_restart_requested'}));return
    with (root/'startup-error.log').open('wb') as error:
        subprocess.Popen(['/usr/bin/python3','-B',str(entry),'--config',str(config)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=error,
            start_new_session=True,cwd=str(root),env={'PATH':'/usr/bin:/bin'})
    print(json.dumps({'status':'supervisor_start_requested'}))


if __name__ == '__main__':
    main()
