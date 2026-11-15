"""跨平台状态目录，不启动服务或读取账号配置。"""

import os
import sys
from pathlib import Path


def arl_home():
    if sys.platform == 'darwin':
        return Path.home()/'Library/Application Support/ARL'
    base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))
    if not base.is_absolute():
        raise ValueError('state_home_must_be_absolute')
    return base/'arl'
