"""云台转动前后的短时画面对应，仅用于本镜头的跟随决策。"""

import base64
from dataclasses import dataclass
import math

import cv2
import numpy as np

from .stable_tracks import valid_box, warp_box


@dataclass(frozen=True)
class FollowAppearance:
    basis: str
    bounds: tuple
    species: str | None
    points: np.ndarray
    descriptors: np.ndarray


def _view(sample):
    view = sample.get('_patrol_view')
    if not isinstance(view, tuple) or len(view) != 3 or not isinstance(view[0], str):
        return None
    try:
        matrix = np.asarray(view[1], dtype=float)
    except (TypeError, ValueError):
        return None
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        return None
    return view[0], matrix


def _decode(sample):
    jpeg = sample.get('_follow_jpeg')
    encoded = sample.get('frame')
    if jpeg is None and isinstance(encoded, str) and len(encoded) <= 2_666_668:
        try:
            jpeg = base64.b64decode(encoded, validate=True)
        except ValueError:
            return None
    if isinstance(jpeg, bytes) and 0 < len(jpeg) <= 2_000_000:
        try:
            return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_2)
        except cv2.error:
            return None
    return None


def _features(image, box, detector):
    height, width = image.shape
    x, y, w, h = (box[key] for key in ('x', 'y', 'w', 'h'))
    # 取目标框内部，减少框边的静止背景参与对应。
    left, right = round((x + .14*w)*width), round((x + .86*w)*width)
    top, bottom = round((y + .14*h)*height), round((y + .86*h)*height)
    if min(right-left, bottom-top) < 24:
        return None
    patch = cv2.resize(image[top:bottom, left:right], (96, 128), interpolation=cv2.INTER_LINEAR)
    points, descriptors = detector.detectAndCompute(patch, None)
    if descriptors is None or len(points) < 12:
        return None
    return np.float32([point.pt for point in points]), descriptors


def _detector():
    return cv2.ORB_create(nfeatures=120, edgeThreshold=8, patchSize=15, fastThreshold=12)


def remember_follow_target(sample, subject):
    """从触发转动的画面提取有界特征，原图由原采集流程管理。"""
    view, box = _view(sample), subject.get('box')
    if view is None or not valid_box(box) or box['confidence'] < .65:
        return None
    bounds = warp_box(box, view[1])
    image = _decode(sample)
    if bounds is None or image is None:
        return None
    appearance = _features(image, box, _detector())
    return FollowAppearance(view[0], tuple(bounds), subject.get('species'), *appearance) if appearance else None


def _near(before, after):
    for axis, limit in ((0, .32), (1, .26)):
        span = before[axis+2] - before[axis]
        ratio = (after[axis+2] - after[axis]) / span
        shift = abs((after[axis+2]+after[axis] - before[axis+2]-before[axis]) / 2)
        if not .55 <= ratio <= 1.85 or shift > limit:
            return False
    return True


def _corresponds(anchor, appearance, matcher):
    points, descriptors = appearance
    reverse = {match.queryIdx: match.trainIdx for pair in matcher.knnMatch(descriptors, anchor.descriptors, k=2)
               if len(pair) == 2 and (match := pair[0]).distance < .72*pair[1].distance}
    pairs = [(match.queryIdx, match.trainIdx) for pair in matcher.knnMatch(anchor.descriptors, descriptors, k=2)
             if len(pair) == 2 and (match := pair[0]).distance <= 48 and match.distance < .72*pair[1].distance
             and reverse.get(match.trainIdx) == match.queryIdx]
    if len(pairs) < 10:
        return False
    source = anchor.points[[pair[0] for pair in pairs]]
    current = points[[pair[1] for pair in pairs]]
    transform, inliers = cv2.estimateAffinePartial2D(source, current, ransacReprojThreshold=3)
    if transform is None or inliers is None:
        return False
    accepted = inliers.ravel().astype(bool)
    scale = math.hypot(transform[0, 0], transform[1, 0])
    return (accepted.sum() >= max(9, len(pairs)*.65) and .68 <= scale <= 1.47
            and cv2.contourArea(cv2.convexHull(source[accepted])) >= 850)


def find_follow_target(anchor, sample, subjects):
    """同一视野基准内只返回唯一获得位置和画面支持的候选。"""
    if anchor is None:
        return None
    view = _view(sample)
    if view is None or view[0] != anchor.basis:
        return None
    nearby = []
    for subject in subjects:
        box = subject.get('box')
        if subject.get('species') != anchor.species or not valid_box(box) or box['confidence'] < .65:
            continue
        bounds = warp_box(box, view[1])
        if bounds is not None and _near(anchor.bounds, bounds):
            nearby.append(subject)
    if not 1 <= len(nearby) <= 4:
        return None
    image = _decode(sample)
    if image is None:
        return None
    detector, matcher = _detector(), cv2.BFMatcher(cv2.NORM_HAMMING)
    result = None
    for subject in nearby:
        appearance = _features(image, subject['box'], detector)
        if appearance is not None and _corresponds(anchor, appearance, matcher):
            if result is not None:
                return None
            result = subject
    return result
