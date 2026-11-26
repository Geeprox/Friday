"""保存用户选定的预置视角，并用独立的背景特征验证实际覆盖。"""

import base64
from dataclasses import dataclass
import json
import math
import os
import re
import secrets
import stat

import cv2
import numpy as np

from .room_protocol import MAX_JPEG


REFERENCE_FILE = "room-view-reference.json"
MAX_REFERENCE = 140_000
MAX_FEATURES = 1000
UNIT_CORNERS = np.float32([[0, 0], [1, 0], [1, 1], [0, 1]])


def _reference_overlap(contour):
    # 直接裁到单位方框，避免近乎重合的边被浮点误差判成交集为空。
    vertices = list(contour.astype(float))
    for axis, edge, sign in ((0, 0, 1), (0, 1, -1), (1, 0, 1), (1, 1, -1)):
        if len(vertices) < 3:
            return 0.
        clipped = []
        previous = vertices[-1]
        previous_distance = sign * (previous[axis] - edge)
        for point in vertices:
            distance = sign * (point[axis] - edge)
            if (distance >= 0) != (previous_distance >= 0):
                ratio = previous_distance / (previous_distance - distance)
                intersection = previous + ratio * (point - previous)
                intersection[axis] = edge
                clipped.append(intersection)
            if distance >= 0:
                clipped.append(point)
            previous, previous_distance = point, distance
        vertices = clipped
    return cv2.contourArea(np.float32(vertices)) if len(vertices) >= 3 else 0.


@dataclass(frozen=True)
class ViewReference:
    revision: str
    preset: int
    points: np.ndarray
    descriptors: np.ndarray

    @classmethod
    def read(cls, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_size > MAX_REFERENCE):
                raise ValueError("invalid_room_view_reference")
            raw = stream.read(MAX_REFERENCE + 1)
        if len(raw) > MAX_REFERENCE:
            raise ValueError("invalid_room_view_reference")
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.keys() != {"version", "revision", "preset", "points", "descriptors"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["revision"], str) or not re.fullmatch("[0-9a-f]{24}", data["revision"])
                or type(data["preset"]) is not int or not 0 <= data["preset"] < 2 ** 31
                or not isinstance(data["points"], list) or not 32 <= len(data["points"]) <= MAX_FEATURES
                or not isinstance(data["descriptors"], str)):
            raise ValueError("invalid_room_view_reference")
        if any(not isinstance(point, list) or len(point) != 2 or
               any(type(value) not in (int, float) or not 0 <= value <= 1 for value in point)
               for point in data["points"]):
            raise ValueError("invalid_room_view_reference")
        points = np.float32(data["points"])
        descriptors = np.frombuffer(base64.b64decode(data["descriptors"], validate=True), np.uint8)
        if descriptors.size != len(points) * 32 or cv2.contourArea(cv2.convexHull(points)) < .20:
            raise ValueError("invalid_room_view_reference")
        return cls(data["revision"], data["preset"], points, descriptors.reshape((-1, 32)))

    def measure(self, room, points, descriptors, visible, now):
        value = dict(revision=self.revision, preset=self.preset, state="unknown", at=now, overlap=None)
        if not visible:
            return value
        match = room.estimate(points, descriptors, self.points, self.descriptors)
        if match is None:
            return value
        count, transform, inliers, observed, reference = match
        inside = inliers.ravel().astype(bool)
        observed, reference = observed[inside], reference[inside]
        reference_spread = cv2.contourArea(cv2.convexHull(reference))
        if (count < 20 or cv2.contourArea(cv2.convexHull(observed)) < .055
                or reference_spread < .025):
            return value
        projected = np.c_[UNIT_CORNERS, np.ones(4)] @ transform.T
        weights = projected[:, 2:]
        if (not np.isfinite(projected).all() or np.any(np.abs(weights) < 1e-6)
                or not (np.all(weights > 0) or np.all(weights < 0))):
            return value
        contour = np.float32(projected[:, :2] / weights)
        if np.abs(contour).max() > 4 or cv2.contourArea(contour, oriented=True) < .03:
            return value
        if not cv2.isContourConvex(contour):
            return value
        projected_points = cv2.perspectiveTransform(observed.reshape((-1, 1, 2)), transform).reshape((-1, 2))
        residual = np.median(np.linalg.norm(projected_points - reference, axis=1))
        if not math.isfinite(residual) or residual > .004:
            return value
        overlap = min(1., _reference_overlap(contour))
        if overlap >= .55 and (reference_spread < .18 or np.ptp(reference, axis=0).min() < .50):
            # 只有局部匹配时，不能把单应矩阵外推出来的面积当成可见房间。
            return value
        value.update(overlap=round(overlap, 3), state="covered" if overlap >= .55 else "partial")
        return value


class ViewReferenceProbe:
    def __init__(self, folder):
        self.path = folder / REFERENCE_FILE
        self.signature = self.reference = self.result = None
        self.next_read = self.next_measurement = 0.

    def observe(self, room, points, descriptors, visible, now, *, stable):
        if now >= self.next_read:
            self.next_read = now + 1
            try:
                info = self.path.lstat()
                signature = (info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid)
            except OSError:
                signature = None
            if signature != self.signature:
                self.signature = signature
                self.reference = self.result = None
                self.next_measurement = 0.
                if signature is not None:
                    try:
                        self.reference = ViewReference.read(self.path)
                    except (ValueError, OSError, RecursionError):
                        pass
        if not stable or self.reference is None:
            self.result = None
            return None
        # 复用当前帧的背景特征，未达到采样间隔时保留原观测时刻。
        if (now >= self.next_measurement or self.result is None
                or not visible and self.result["state"] != "unknown"):
            self.next_measurement = now + .6
            self.result = self.reference.measure(room, points, descriptors, visible, now)
        return self.result


def save_reference(folder, preset, picture):
    from .camera_director import visible_scene_luma
    from .ptz_control import atomic
    from .room_engine import DEFAULT, Room, decode_luma

    if type(preset) is not int or not 0 <= preset < 2 ** 31 or not 0 < len(picture) <= MAX_JPEG:
        raise ValueError("invalid_room_view_reference")
    image = decode_luma(picture)
    if not visible_scene_luma(image):
        raise ValueError("room_view_reference_detail_missing")
    room = Room(DEFAULT, picture)
    points, descriptors = room.ref_points, room.ref_desc
    if (descriptors is None or not 32 <= len(points) <= MAX_FEATURES
            or cv2.contourArea(cv2.convexHull(points)) < .20):
        raise ValueError("room_view_reference_detail_missing")
    data = dict(version=1, revision=secrets.token_hex(12), preset=preset,
                points=np.round(points.astype(float), 5).tolist(),
                descriptors=base64.b64encode(descriptors.tobytes()).decode("ascii"))
    atomic(folder / REFERENCE_FILE, data)
    return data["revision"]


def main():
    import argparse
    import sys

    from .paths import camera_storage

    parser = argparse.ArgumentParser(description="为已确认的米家预置位记录房间背景特征")
    parser.add_argument("--camera", required=True, help="Friday 已登记的镜头标识")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--preset", type=int, help="已核对的米家预置位编号，从标准输入读取当前空房 JPEG")
    actions.add_argument("--clear", action="store_true", help="移除此镜头的恢复参考")
    args = parser.parse_args()
    try:
        folder = camera_storage(args.camera)
        if args.clear:
            (folder / REFERENCE_FILE).unlink(missing_ok=True)
            print("已移除恢复参考")
        else:
            save_reference(folder, args.preset, sys.stdin.buffer.read(MAX_JPEG + 1))
            print("已保存恢复参考")
    except (ValueError, OSError, KeyError, cv2.error):
        parser.exit(1, "无法保存或移除恢复参考，请核对镜头和空房画面。\n")


if __name__ == "__main__":
    main()
