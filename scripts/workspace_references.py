"""检查可回收输入的打开引用，保留检测结果不明的目录。"""

import os
import re
import subprocess
import sys
from itertools import chain

_DARWIN_SKIPPED_STAT = re.compile(
    rb"^lsof: avoiding (?:lstat|readlink|stat)\([^\n]*\): -b was specified\.\n"
    rb'|^lsof: WARNING: can\x27t stat\(\) [^ \n]+ file system [^\n]+\n'
    rb'[ \t]+Output information may be incomplete\.\n'
    rb'[ \t]+assuming "dev=[a-fA-F0-9]+" from mount table\n',
    re.MULTILINE,
)


def _scan(arguments, *, timeout, attempts=1):
    for attempt in range(attempts):
        try:
            return subprocess.run(
                ["lsof", "-nP", *arguments], capture_output=True, timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired as error:
            if attempt + 1 == attempts:
                raise OSError("workspace_reference_scan_timed_out") from error


def workspace_in_use(root, *, own_lock=None):
    """检查当前可见的引用，只允许本进程持有指定的锁文件。"""
    root = root.resolve(strict=True)
    darwin = sys.platform == "darwin"
    if darwin:
        # Darwin 的 libproc 会直接返回设备和 inode。用这些字段核对本地文件，
        # 避免为了扫描目标目录而逐一 stat 其他挂载点。
        identities = set()
        for entry in chain((root,), root.rglob("*")):
            info = entry.lstat()
            identities.add((info.st_dev, info.st_ino))
        scan = _scan(["-b", "-F0pfDin"], timeout=10, attempts=2)
        diagnostics = _DARWIN_SKIPPED_STAT.sub(b"", scan.stderr)
        if scan.returncode != 0 or not scan.stdout:
            raise OSError("workspace_reference_scan_incomplete")
    else:
        scope = "+D" if root.is_dir() else "--"
        scan = _scan(["-S", "2", "-F0pfn", scope, str(root)], timeout=20)
        diagnostics = scan.stderr
    if diagnostics or scan.returncode not in {0, 1} or (scan.stdout and not scan.stdout.endswith(b"\0\n")):
        raise OSError("workspace_reference_scan_incomplete")

    name_prefix = os.fsencode(root)
    allowed_name = os.fsencode(own_lock) if own_lock is not None else None
    own_pid = str(os.getpid()).encode()
    pid = None
    for record in scan.stdout.split(b"\0\n"):
        fields = {field[:1]: field[1:] for field in record.split(b"\0") if field}
        if not fields:
            continue
        if b"p" in fields:
            pid = fields[b"p"]
            continue
        if pid is None or b"f" not in fields:
            raise OSError("workspace_reference_scan_incomplete")
        name = fields.get(b"n", b"")
        if pid == own_pid and name == allowed_name:
            continue
        if not darwin:
            return True
        if name == name_prefix or name.startswith(name_prefix + b"/"):
            return True
        if b"D" in fields and b"i" in fields:
            identity = (int(fields[b"D"], 16), int(fields[b"i"]))
            if identity in identities:
                return True
    return False
