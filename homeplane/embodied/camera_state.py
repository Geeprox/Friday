"""摄像头状态的原子提交和有界 JSON 读取，用于隐私、会话、云台与场景。"""

import json
import os
import stat
import tempfile


def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix='.ptz-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_lease(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('invalid_camera_lease')
        value = json.load(stream)
    if not isinstance(value, dict):raise ValueError('invalid_camera_lease')
    return value
