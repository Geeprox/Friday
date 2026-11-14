"""将播报目标独立于摄像头配置保存，并兼容旧版设置。"""

import json
import os
import stat
from typing import Literal, get_args

from friday.home import private_json


OutputKind = Literal['panel', 'media_player', 'jbl', 'homepod', 'mac']
OUTPUTS = get_args(OutputKind)
FILENAME = 'speech-output.json'


def output_configuration(private, legacy):
    try:
        fd = os.open(private / FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        selected = None
    else:
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('speech_output_not_available')
            body = stream.read(1025)
        if len(body) > 1024:
            raise ValueError('speech_output_not_available')
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) != {'output'} or data['output'] not in OUTPUTS:
            raise ValueError('speech_output_not_available')
        selected = data['output']
    if selected in {'mac', 'homepod'}:
        return {'speech_output': selected}
    try:
        config = legacy()
    except FileNotFoundError:
        config = {}
    return config if selected is None else {**config, 'speech_output': selected}


def save_output(private, output: OutputKind):
    if output not in OUTPUTS or private.is_symlink():
        raise ValueError('speech_output_not_available')
    private_json(private / FILENAME, {'output': output})
