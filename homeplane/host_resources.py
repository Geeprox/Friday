"""Bounded, passive host resource samples. No device, model or network access."""
from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def _read_host():
    if platform.system() == 'Linux':
        ticks = [int(n) for n in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        memory = {key: int(value.split()[0]) * 1024 for key, value in
                  (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
        return sum(ticks), ticks[3] + ticks[4], memory['MemTotal'] - memory['MemAvailable'], memory['MemTotal']
    if platform.system() != 'Darwin':
        raise OSError('resource_sampling_unavailable')
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    library.mach_host_self.restype = ctypes.c_uint
    library.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]
    host = library.mach_host_self()
    ticks = (ctypes.c_int * 4)()
    count = ctypes.c_uint(4)
    try:
        if library.host_statistics(host, 3, ticks, ctypes.byref(count)) != 0 or count.value != 4:
            raise OSError('resource_sampling_unavailable')
        unsigned = [value & 0xffffffff for value in ticks]
    finally:
        library.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        library.mach_port_deallocate(ctypes.c_uint.in_dll(library, 'mach_task_self_').value, host)
    total = ctypes.c_uint64()
    length = ctypes.c_size_t(ctypes.sizeof(total))
    library.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    if library.sysctlbyname(b'hw.memsize', ctypes.byref(total), ctypes.byref(length), None, 0) != 0:
        raise OSError('resource_sampling_unavailable')
    result = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True, timeout=1, check=True)
    page = re.search(r'page size of (\d+) bytes', result.stdout)
    rows = {name.strip(): int(value) for name, value in re.findall(r'^([^:\n]+):\s+(\d+)\.', result.stdout, re.M)}
    if page is None or not {'Anonymous pages', 'Pages purgeable', 'Pages wired down', 'Pages occupied by compressor'} <= rows.keys():
        raise OSError('resource_sampling_unavailable')
    used = (max(0, rows['Anonymous pages'] - rows['Pages purgeable']) + rows['Pages wired down']
            + rows['Pages occupied by compressor']) * int(page[1])
    return sum(unsigned), unsigned[2], used, total.value


class HostResources:
    def __init__(self, reader=None, clock=None):
        self.reader = reader or _read_host
        self.clock = clock or time.monotonic
        self.previous = None
        self.cached = None
        self.sampled = None
        self.lock = threading.Lock()

    def sample(self):
        with self.lock:
            now = self.clock()
            if self.cached is not None and self.sampled is not None and 0 <= now - self.sampled < 2:
                return dict(self.cached)
            cpu, used, total = None, None, None
            available = False
            try:
                ticks, idle, used, total = self.reader()
                if not all(type(n) is int and n >= 0 for n in (ticks, idle, used, total)) or not 0 < total >= used or idle > ticks:
                    raise ValueError('invalid_resource_sample')
                if self.previous and self.sampled is not None and now > self.sampled:
                    elapsed, waiting = ticks - self.previous[0], idle - self.previous[1]
                    if elapsed > 0 and 0 <= waiting <= elapsed:
                        cpu = round((elapsed - waiting) * 100 / elapsed, 1)
                self.previous = (ticks, idle)
                available = True
            except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
                self.previous = None
                used, total = None, None
            self.sampled = now
            self.cached = {'available': available, 'cpu_percent': cpu, 'memory_used_bytes': used,
                           'memory_total_bytes': total, 'cpu_count': os.cpu_count(),
                           'sampled_at': datetime.now(UTC).isoformat(), 'valid_for_seconds': 15,
                           'scope': 'host', 'basis': 'operating_system_counters'}
            return dict(self.cached)
