"""保存每台镜头是否接收语音和手势，默认参与。"""

import fcntl
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import camera_storage
from .ptz_control import atomic, read_lease


@dataclass(frozen=True)
class CameraInteraction:
    enabled: bool = True
    revision: int = 0


def read(camera_id, *, directory=None):
    if not re.fullmatch(r'cam_[a-f0-9]{10}', camera_id):
        raise ValueError('invalid_camera_interaction')
    path = (camera_storage(camera_id) if directory is None else Path(directory) / 'cameras' / camera_id) / 'interaction.json'
    try:
        value = read_lease(path)
    except FileNotFoundError:
        return CameraInteraction()
    if (set(value) != {'enabled', 'revision'} or type(value['enabled']) is not bool
            or type(value['revision']) is not int or value['revision'] < 0):
        raise ValueError('invalid_camera_interaction')
    return CameraInteraction(**value)


def save(camera_id, enabled, expected_revision):
    path = camera_storage(camera_id) / 'interaction.json'
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = read(camera_id)
        if current.revision != expected_revision:
            raise ValueError('camera_interaction_changed')
        if current.enabled != enabled:
            current = CameraInteraction(enabled, current.revision + 1)
            atomic(path, vars(current))
        return current
    finally:
        os.close(fd)


def allowed(camera_id, *, directory=None):
    try:
        return read(camera_id, directory=directory).enabled
    except (OSError, ValueError):
        return False
