"""Launch owned Python workers from the same source tree as their parent.

Desktop source/vendor paths are inserted by the backend and are not inherited
by a fresh interpreter. Bind them explicitly while keeping the worker isolated
from the caller's working directory and environment Python paths.
"""
import json
import sys
from pathlib import Path


def module_command(module: str, *arguments: str) -> list[str]:
    if module not in {'homeplane.settings', 'homeplane.recordings', 'homeplane.archive_worker'}:
        raise ValueError('unsupported_owned_worker')
    source = Path(__file__).resolve().parent.parent
    paths = [str(source)]
    vendor = source.parent / 'vendor'
    if vendor.is_dir():
        paths.insert(0, str(vendor))
    bootstrap = (
        'import sys,runpy;'
        f'sys.path[:0]={json.dumps(paths)};'
        f'sys.argv={json.dumps([module, *arguments])};'
        f'runpy.run_module({json.dumps(module)},run_name="__main__")'
    )
    return [sys.executable, '-I', '-B', '-c', bootstrap]
