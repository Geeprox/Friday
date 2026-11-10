"""NAS boot-task supervisor for only the KTV guest process (Python 3.8+)."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path


class RestartBudget:
    """At most five short-lived children; an established run resets the budget."""
    def __init__(self):
        self.failures = 0

    def exited(self, uptime):
        if uptime >= 300:
            self.failures = 0
        self.failures += 1
        return (2, 4, 8, 16)[self.failures - 1] if self.failures <= 4 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = args.config.absolute()
    if config.is_symlink() or config.stat().st_uid != os.getuid():
        raise ValueError("private_config_required")
    # SMB mode requests may not map to POSIX modes; enforce locally as the owner.
    config.chmod(0o600)
    root = config.parent
    os.umask(0o077)
    lock = (root / "supervisor.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    child, stopping = None, False
    budget = RestartBudget()

    def stop(*unused):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        started = time.monotonic()
        child = subprocess.Popen(["/usr/bin/python3", "-B", str(Path(__file__).with_name("nas_server.py")),
                                  "--config", str(config)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
        (root / "process.json").write_text(json.dumps({"supervisor_pid": os.getpid(), "server_pid": child.pid,
                                                      "started": int(time.time())}))
        code = child.wait()
        if not stopping:
            (root / "last-exit.json").write_text(json.dumps({"at": int(time.time()), "returncode": code}))
            delay = budget.exited(time.monotonic() - started)
            if delay is None:
                (root / "restart-blocked.json").write_text(json.dumps({"at": int(time.time()), "reason": "repeated_child_exit",
                                                                      "failures": budget.failures, "requires_manual_start": True}))
                break
            for unused in range(delay * 10):
                if stopping:
                    break
                time.sleep(.1)


if __name__ == "__main__":
    main()
