"""Camera identity from the selected capture source, with no hardware or login I/O."""

import hashlib
import json
import re


def camera_source_context(selected):
    if not isinstance(selected, dict):
        return None
    camera = selected.get('id')
    device = selected.get('device')
    did = device.get('did') if isinstance(device, dict) else None
    region = selected.get('region', 'cn')
    if (not isinstance(camera, str) or not re.fullmatch(r'cam_[a-f0-9]{10}', camera)
            or not isinstance(did, str) or not 1 <= len(did) <= 256
            or not isinstance(region, str) or not re.fullmatch(r'[a-z0-9_-]{1,16}', region)):
        return None
    return hashlib.sha256(json.dumps(['friday.capture-source/1', camera, did, region],
                                    separators=(',', ':')).encode()).hexdigest()


def bind_capture_packet(packet, selected):
    # Called inside the SDK's encoder callback with its already-selected camera.
    # Never reconstruct an old frame's identity from the current configuration.
    name = selected.get('name') if isinstance(selected, dict) else None
    if (not isinstance(name, str) or not 1 <= len(name) <= 80
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
        name = None
    return dict(packet, camera_source_context=camera_source_context(selected), camera_source_name=name)


def configured_camera_sources():
    from .paths import configuration
    try:
        config = configuration()
        if config.get('simulated') is True:
            return []
        cameras = config.get('cameras')
        if not isinstance(cameras, list) or not 1 <= len(cameras) <= 8:
            return []
        result = []
        for camera in cameras:
            context = camera_source_context(camera)
            name = camera.get('name') if isinstance(camera, dict) else None
            if (context is None or not isinstance(name, str) or not 1 <= len(name) <= 80
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
                return []
            result.append(dict(id=camera['id'], name=name, source_context=context))
        if len({row['id'] for row in result}) != len(result):
            return []
        physical = [(camera['device']['did'], camera.get('region', 'cn')) for camera in cameras]
        return result if len(set(physical)) == len(physical) else []
    except (OSError, ValueError, KeyError, TypeError):
        return []
