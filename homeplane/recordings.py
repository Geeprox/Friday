"""Read-only original NAS recordings, bounded worker and expiring file leases.

Filename time is a label from the recorder; it is not a calibrated event clock.
The worker opens paths beneath the configured mount with no-follow directory FDs.
"""
import asyncio
import heapq
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path

from .archive_worker import root_handle, open_beneath, signature
from .archive_playback import byte_range
from .worker_runtime import module_command

FOLDER = re.compile(r'XiaomiCamera_[0-9]{2}_[A-Fa-f0-9]{12}')
FILE = re.compile(r'[0-9]{2}_([0-9]{14})_([0-9]{14})\.(?:mp4|mov)', re.I)


def listing(root, selected=None, date=None, limit=80):
    candidates,folders,visited=[],[],0
    fd=root_handle(root)
    try:
        with os.scandir(fd)as entries:
            names=[e.name for e in entries if FOLDER.fullmatch(e.name)and e.is_dir(follow_symlinks=False)][:8]
        for folder in sorted(names):
            folders.append(folder)
            if selected and selected!=folder:continue
            sub=open_beneath(fd,folder,directory=True)
            try:
                with os.scandir(sub)as entries:
                    for e in entries:
                        visited+=1
                        if visited>20000:break
                        m=FILE.fullmatch(e.name)
                        if not m or date and m[1][:8]!=date:continue
                        row=dict(folder=folder,name=e.name,recorded_start=m[1],recorded_end=m[2])
                        heapq.heappush(candidates,(m[1]+'/'+folder+'/'+e.name,row))
                        if len(candidates)>limit:heapq.heappop(candidates)
            finally:os.close(sub)
        result=[]
        # SMB directory enumeration is cheap relative to one stat per historic
        # file. Inspect only the selected newest candidates and never follow links.
        for _,row in sorted(candidates,reverse=True):
            try:
                candidate=open_beneath(fd,row['folder']+'/'+row['name'])
                try:info=os.fstat(candidate)
                finally:os.close(candidate)
                if not stat.S_ISREG(info.st_mode)or info.st_size<1:continue
                result.append(dict(row,size=info.st_size,revision=signature(info)))
            except OSError:continue
        return dict(folders=folders,files=result,visited=visited,partial=visited>20000)
    finally:os.close(fd)


class Recordings:
    def __init__(self, archive):
        self.archive = archive
        self.files, self.leases = {}, {}
        self.lock = asyncio.Lock()

    async def browse(self, folder=None, date=None):
        if folder is not None and not FOLDER.fullmatch(folder):
            raise ValueError('invalid_recording_folder')
        if date is not None and not re.fullmatch(r'[0-9]{8}', date):
            raise ValueError('invalid_recording_date')
        async with self.lock:
            root = await self.archive._root()
            proc = await asyncio.create_subprocess_exec(*module_command('homeplane.recordings'),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(json.dumps(dict(root=root, folder=folder, date=date)).encode()), 25)
                if len(out) > 256000 or proc.returncode:
                    raise ValueError('recordings_unavailable')
                result = json.loads(out)
                self.files.clear()
                for row in result['files']:
                    ref = secrets.token_hex(16)
                    self.files[ref] = dict(row, root=root)
                    row['id'] = ref
                return dict(connected=True, **result, clock_basis='recorder_filename', read_only=True)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    async def prepare(self, ref):
        item = self.files.get(ref)
        if item is None:
            raise ValueError('recording_refresh_required')
        root = await self.archive._root()
        if root != item['root']:
            raise ValueError('recording_source_changed')
        token = secrets.token_hex(24)
        now = time.monotonic()
        self.leases = {k: v for k, v in self.leases.items() if v['deadline'] > now}
        if len(self.leases) >= 8:
            self.leases.pop(next(iter(self.leases)))
        self.leases[token] = dict(item, deadline=now + 1800, remaining=1024**3)
        return dict(url='/api/home/recordings/media/' + token, expires_in=1800, name=item['name'], size=item['size'])

    def media(self, token, header):
        """Each response verifies the exact original revision, with bounded reads."""
        lease = self.leases.get(token)
        if lease is None or time.monotonic() >= lease['deadline']:
            raise ValueError('recording_expired')
        root = root_handle(lease['root'])
        try:
            fd = open_beneath(root, lease['folder'] + '/' + lease['name'])
        finally:
            os.close(root)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or signature(info) != lease['revision']:
                raise ValueError('recording_source_changed')
            # Browsers first request metadata without a Range on some versions.
            # Serve the first bounded part with 206 instead of copying the file.
            start, length, partial = byte_range(header or 'bytes=0-', info.st_size)
            if lease['remaining'] < length:
                raise ValueError('recording_budget_exceeded')
            lease['remaining'] -= length
            os.close(fd)
            fd=None
            def chunks():
                root_fd=root_handle(lease['root'])
                try:stream_fd=open_beneath(root_fd,lease['folder']+'/'+lease['name'])
                finally:os.close(root_fd)
                try:
                    if signature(os.fstat(stream_fd))!=lease['revision']:return
                    offset, left = start, length
                    while left:
                        if time.monotonic() >= lease['deadline'] or signature(os.fstat(stream_fd)) != lease['revision']:
                            return
                        data = os.pread(stream_fd, min(262144, left), offset)
                        if not data:
                            return
                        yield data
                        offset += len(data)
                        left -= len(data)
                finally:
                    os.close(stream_fd)
            return chunks(), {'Content-Range':f'bytes {start}-{start+length-1}/{info.st_size}',
                             'Content-Length':str(length), 'Accept-Ranges':'bytes', 'Cache-Control':'no-store'}, 206
        except BaseException:
            if fd is not None:os.close(fd)
            raise


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.read(4096))
        print(json.dumps(listing(request['root'], request.get('folder'), request.get('date'))))
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit(1)
