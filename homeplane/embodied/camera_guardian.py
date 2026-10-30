"""Independent exit recovery. No SDK or models while active leases are held.

Only the perception worker holds the pipe's write end. EOF survives SIGKILL and
parent-service crashes; each repair is fenced by the camera lock and owner token.
"""
import argparse
import asyncio
import os
import re
import stat
import threading
import time

from .paths import PRIVATE, configuration
from .ptz_control import read_lease, restore_camera


def exit_with_owner(parent):
    """Do not let a blocked SDK/executor keep an orphaned control connection."""
    if type(parent) is not int or parent <= 1 or os.getppid() != parent:
        os._exit(0)
    def watch():
        while os.getppid() == parent:time.sleep(.1)
        os._exit(0)
    threading.Thread(target=watch, name='friday-owner-lifetime', daemon=True).start()


def check_guardian():
    """A dead/stalled guardian revokes capture rather than leaving it unguarded."""
    raw = os.environ.get('FRIDAY_CAMERA_LIFETIME_FD', '')
    if not raw.isdecimal() or int(raw) < 3:
        raise RuntimeError('camera_guardian_required')
    fd = int(raw)
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise RuntimeError('invalid_camera_guardian_pipe')
    os.set_blocking(fd, False)
    os.write(fd, b'.')


async def watch_owner(parent, on_exit, *, interval=1):
    while os.getppid() == parent:
        try:check_guardian()
        except (OSError, RuntimeError):break
        await asyncio.sleep(interval)
    on_exit()


async def wait_for_exit(fd):
    reader = asyncio.StreamReader()
    pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(fd, 'rb', buffering=0))
    try:
        while await reader.read(256):
            pass
    finally:
        pipe.close()


async def recover_owned(owner_token, cameras, *, root=PRIVATE, repair=restore_camera, interval=5,
                        once=False, released_only=False):
    pending = set(cameras)
    while pending:
        async def recover(camera_id):
            path = root / 'cameras' / camera_id / 'ptz-lease.json'
            try:
                if not path.exists():
                    pending.remove(camera_id)
                    return
                record = read_lease(path)
                if record.get('owner_token') != owner_token or record.get('status') == 'restored':
                    pending.remove(camera_id)
                    return
                if released_only and record.get('status') not in {'restoring', 'restore_pending'}:
                    pending.remove(camera_id)
                    return
                if await asyncio.wait_for(repair(camera_id, path=path, owner_token=owner_token), 50):
                    pending.remove(camera_id)
            except Exception:
                # Durable baseline stays in place. Network loss must not discard
                # it or let a new owner capture Friday's disabled flags as baseline.
                pass
        await asyncio.gather(*(recover(camera_id) for camera_id in list(pending)))
        if once:return
        if pending:await asyncio.sleep(interval)


async def monitor_owner(fd, owner_token, cameras, *, root=PRIVATE, repair=restore_camera, interval=5):
    exited = asyncio.create_task(wait_for_exit(fd))
    try:
        while not exited.done():
            try:await asyncio.wait_for(asyncio.shield(exited), interval)
            except asyncio.TimeoutError:
                # A single camera can fail cleanup while the rest of the worker
                # stays live. Retry only released leases; locks fence in-flight cleanup.
                await recover_owned(owner_token, cameras, root=root, repair=repair,
                                    interval=interval, once=True, released_only=True)
        await exited
        # Capture helpers have an independent parent-death thread as well, so
        # their SDK/executor cleanup cannot strand a motor connection.
        await asyncio.sleep(.3)
        await recover_owned(owner_token, cameras, root=root, repair=repair, interval=interval)
    finally:
        if not exited.done():exited.cancel()
        await asyncio.gather(exited, return_exceptions=True)


async def main(fd, owner_token):
    cameras = [c['id'] for c in configuration()['cameras']]
    print('ready', flush=True)
    await monitor_owner(fd, owner_token, cameras)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--owner', required=True)
    args = parser.parse_args()
    if args.fd < 3 or not re.fullmatch('[a-f0-9]{32}', args.owner):
        parser.error('invalid_guardian_owner')
    asyncio.run(main(args.fd, args.owner))
