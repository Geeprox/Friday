"""为隔离采集进程准备平台、网卡和可选中继环境。"""

import os
import re
import sys

from .paths import DATA, PRIVATE, PROJECT, SOCKETS, camera, configuration


def capture_environment(camera_id=None):
    environment = dict(os.environ)
    environment['FRIDAY_CAPTURE_OWNER_PID'] = str(os.getpid())
    interface = configuration().get("camera_interface")
    if interface:
        if sys.platform != 'darwin':
            # Linux 由系统路由选择网卡，不能加载 macOS 的动态库或静默忽略指定网卡。
            raise ValueError('camera_interface_override_unsupported')
        if not re.fullmatch(r"en[0-9]{1,2}", interface) or not SOCKETS.is_file():
            raise ValueError("camera_interface_unavailable")
        environment["FRIDAY_CAMERA_INTERFACE"] = interface
        # Only supplied to the isolated Miloco SDK subprocess, never Console,
        # games, models or the system. No shell/global environment is modified.
        environment["DYLD_INSERT_LIBRARIES"] = str(SOCKETS)
    if sys.platform.startswith('linux'):
        from .camera_udp_relay import capture_environment as relay_environment
        library = DATA/'bin/camera-udp-tunnel.so'
        if not library.is_file():
            library = PROJECT/'native/PortableVision/camera-udp-tunnel.so'
        environment = relay_environment(PRIVATE, camera(camera_id), environment, library)
    return environment
