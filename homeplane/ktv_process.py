"""KTV 本地媒体子进程的时限、取消和进程组回收。"""

import os
import signal
import subprocess
import threading
import time


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def run_bounded(command: list[str], event: threading.Event, *, timeout: float, stderr=subprocess.DEVNULL):
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if event.wait(0.05):
                raise ValueError("cancelled")
            if time.monotonic() > deadline:
                raise ValueError("transcode_failed")
        if process.returncode != 0:
            raise ValueError("transcode_failed")
    finally:
        terminate(process)
