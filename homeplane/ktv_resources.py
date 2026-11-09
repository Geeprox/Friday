"""Read-only Mac resource facts; no inference, network, or device capture."""

import os
import re
import subprocess


def machine():
    result = {"cpu": "未知处理器", "memory_bytes": 0, "cpu_cores": os.cpu_count() or 1}
    try:

        def read(name):
            return (
                subprocess.check_output(
                    ["/usr/sbin/sysctl", "-n", name], stderr=subprocess.DEVNULL, timeout=2
                )
                .decode()
                .strip()
            )

        result.update(
            cpu=read("machdep.cpu.brand_string")[:80],
            memory_bytes=int(read("hw.memsize")),
            cpu_cores=int(read("hw.logicalcpu")),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def available_memory():
    try:
        text = subprocess.check_output(["/usr/bin/vm_stat"], stderr=subprocess.DEVNULL, timeout=2).decode()
        size = int(re.search(r"page size of (\d+) bytes", text)[1])
        pages = sum(
            int(re.search(r"^" + re.escape(name) + r":\s*(\d+)", text, re.M)[1])
            for name in ("Pages free", "Pages inactive", "Pages speculative")
        )
        return pages * size
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return 0
