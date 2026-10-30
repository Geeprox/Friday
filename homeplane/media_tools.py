"""Locate an already installed media probe from GUI and shell environments.

The name and fallback locations are fixed. No downloader, shell or user command
is accepted. NAS work still executes in the existing read-only bounded worker.
"""
import os
from pathlib import Path
import shutil
import stat

FALLBACKS = ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe")


def ffprobe_executable():
    found = shutil.which("ffprobe")
    for candidate in ([found] if found else []) + list(FALLBACKS):
        try:
            path = Path(candidate).resolve(strict=True)
            info = path.stat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid in {0, os.getuid()}
                    and not info.st_mode & 0o022 and os.access(path, os.X_OK)):
                return str(path)
        except OSError:
            continue
    return None
