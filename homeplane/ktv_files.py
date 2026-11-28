"""KTV 制备和发布共用的文件边界、摘要与原子写入。"""

import hashlib
import json
import os
import secrets
from pathlib import Path

MAX_MEDIA = 512 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6))
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_file(path: Path, root: Path | None = None) -> Path:
    path = path.absolute()
    if path.resolve() != path or not path.is_file() or not 0 < path.stat().st_size <= MAX_MEDIA:
        raise ValueError("source_unavailable")
    if root is not None and not path.is_relative_to(root):
        raise ValueError("source_unavailable")
    return path


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path.resolve() != path:
        raise ValueError("unsafe_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("unsafe_directory")
    path.chmod(0o700)
    return path
