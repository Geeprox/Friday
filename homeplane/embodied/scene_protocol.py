"""Bounded, fixed-vocabulary messages for the optional local scene child."""

import math
import re

MAX_JPEG = 2 * 1024 * 1024
MAX_REQUEST = (MAX_JPEG + 2) // 3 * 4 + 8192
MAX_REPLY = 128 * 1024
CLASSES = {'person': '人体', 'chair': '椅子', 'sofa': '沙发',
           'soft_furniture': '软体家具', 'table': '桌子'}
REASONS = {'scene_model_unavailable', 'scene_frame_unavailable', 'scene_protocol_invalid'}


def source_context(value):
    if (not isinstance(value, dict)
            or set(value) != {'generation', 'wall_epoch', 'motion_generation',
                             'capture_session', 'camera_source_context'}
            or any(type(value[k]) is not int or not 0 <= value[k] <= 2**53
                   for k in ('generation', 'wall_epoch', 'motion_generation'))
            or not isinstance(value['capture_session'], str)
            or not re.fullmatch('[a-f0-9]{32}', value['capture_session'])
            or not isinstance(value['camera_source_context'], str)
            or not re.fullmatch('[a-f0-9]{64}', value['camera_source_context'])):
        raise ValueError('scene_protocol_invalid')
    return dict(value)


def number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def scene_result(value):
    if (not isinstance(value, dict) or set(value) != {'objects', 'scene_ms'}
            or not number(value['scene_ms'], 0, 60000)
            or not isinstance(value['objects'], list) or len(value['objects']) > 256):
        raise ValueError('scene_protocol_invalid')
    objects = []
    for item in value['objects']:
        if (not isinstance(item, dict)
                or set(item) != {'class', 'name', 'confidence', 'box', 'partial'}
                or not isinstance(item['class'], str) or item['class'] not in CLASSES
                or item['name'] != CLASSES[item['class']]
                or not number(item['confidence'], 0, 1) or type(item['partial']) is not bool):
            raise ValueError('scene_protocol_invalid')
        box = item['box']
        if (not isinstance(box, dict) or set(box) != {'x', 'y', 'w', 'h'}
                or any(not number(v, 0, 1) for v in box.values())
                or box['w'] <= 0 or box['h'] <= 0
                or box['x'] + box['w'] > 1.000001 or box['y'] + box['h'] > 1.000001):
            raise ValueError('scene_protocol_invalid')
        objects.append(dict(item, box=dict(box)))
    return dict(objects=objects, scene_ms=value['scene_ms'])
