"""本地 ONNX CPU 识别进程：沿用既有逐行协议，不连接摄像头或下载模型。"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import math
import sys
import time

BODY = {
    'nose': 0, 'leftShoulder': 11, 'rightShoulder': 12,
    'leftElbow': 13, 'rightElbow': 14, 'leftWrist': 15, 'rightWrist': 16,
    'leftHip': 23, 'rightHip': 24, 'leftKnee': 25, 'rightKnee': 26,
    'leftAnkle': 27, 'rightAnkle': 28,
}
HAND = ('wrist', 'thumbCMC', 'thumbMP', 'thumbIP', 'thumbTip',
        'indexMCP', 'indexPIP', 'indexDIP', 'indexTip',
        'middleMCP', 'middlePIP', 'middleDIP', 'middleTip',
        'ringMCP', 'ringPIP', 'ringDIP', 'ringTip',
        'littleMCP', 'littlePIP', 'littleDIP', 'littleTip')
PIPE_LIMIT = 6_000_000


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def point(landmark, region=(0, 0, 1, 1), *, confidence=None):
    """模型坐标已经以左上角为原点；保留低置信度与越界的拒绝语义。"""
    x, y = landmark.x, landmark.y
    if confidence is None:
        confidence = min(getattr(landmark, 'visibility', 0) or 0,
                         getattr(landmark, 'presence', 0) or 0)
    if (not all(finite(v) for v in (x, y, confidence))
            or not 0 <= x <= 1 or not 0 <= y <= 1 or not .4 <= confidence <= 1):
        return None
    rx, ry, rw, rh = region
    return dict(x=rx+x*rw, y=ry+y*rh, confidence=confidence)


def body_rows(landmarks, region=(0, 0, 1, 1)):
    boxes, poses = [], []
    for row in landmarks[:6]:
        if len(row) != 33:
            continue
        joints = {name: p for name, index in BODY.items()
                  if (p := point(row[index], region)) is not None}
        # MediaPipe 没有 neck/root；仅在两侧观测均有效时计算几何中点。
        for name, left, right in [('neck', 'leftShoulder', 'rightShoulder'),
                                  ('root', 'leftHip', 'rightHip')]:
            if left in joints and right in joints:
                a, b = joints[left], joints[right]
                joints[name] = dict(x=(a['x']+b['x'])/2, y=(a['y']+b['y'])/2,
                                    confidence=min(a['confidence'], b['confidence']))
        if len(joints) < 6 or not ('root' in joints or 'neck' in joints):
            continue
        confidence = sum(p['confidence'] for p in joints.values())/len(joints)
        xs, ys = [p['x'] for p in joints.values()], [p['y'] for p in joints.values()]
        x, y = max(0, min(xs)-.02), max(0, min(ys)-.02)
        w, h = min(1, max(xs)+.02)-x, min(1, max(ys)+.02)-y
        if confidence >= .65 and w > 0 and h > 0:
            boxes.append(dict(x=x, y=y, w=w, h=h, confidence=confidence, source='body_pose'))
            poses.append(dict(joints=joints, confidence=confidence))
    return boxes, poses


def hand_rows(result, sample, region=(0, 0, 1, 1)):
    hands = []
    for row, categories in zip(result.hand_landmarks[:4], result.handedness[:4]):
        if len(row) != 21 or not categories:
            continue
        category = categories[0]
        # Tasks 不返回逐点置信度；左右手分数只筛选侧别，不能冒充关节质量。
        score = category.score
        if not finite(score) or not .75 <= score <= 1:
            continue
        joints = {name: p for name, landmark in zip(HAND, row)
                  if (p := point(landmark, region, confidence=.75)) is not None}
        if len(joints) != 21:
            continue
        side = category.category_name.lower()
        hands.append(dict(joints=joints, confidence=.75, handedness_score=score,
                          confidence_source='model_presence_gate_not_joint_visibility',
                          side=side if side in {'left', 'right'} else 'unknown', sample_id=sample))
    return hands


def decode_image(encoded):
    import numpy as np
    from PIL import Image

    if not isinstance(encoded, str) or len(encoded) > 5_333_336:
        raise ValueError('invalid_vision_image')
    raw = base64.b64decode(encoded, validate=True)
    if not 0 < len(raw) < 4_000_000:
        raise ValueError('invalid_vision_image')
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != 'JPEG' or image.width*image.height > 20_000_000:
            raise ValueError('invalid_vision_image')
        return np.asarray(image.convert('RGB'))


class PortableVision:
    def __init__(self, models):
        from .portable_models import ONNX_MODELS, verify
        from .onnx_vision import Pose, Hand
        verify(models, manifest=ONNX_MODELS)
        self.pose = Pose(models)
        try:
            self.hand = Hand(models)
        except BaseException:
            self.pose.close()
            raise
        # 人体/手部路径不导入要求 AVX 的 MediaPipe 原生库。
        # 实验性虹膜视线尚无等价 ONNX 实现，明确报告不可用。
        self.face = None
        self.scene = None
        self.models = models
        self.sequence = 0
        self.last_hands = []
        self.last_sample = -math.inf

    def image(self, array):
        return array

    def crop(self, array, region):
        height, width = array.shape[:2]
        x, y, w, h = region
        left, top = max(0, int(x*width)), max(0, int(y*height))
        right, bottom = min(width, math.ceil((x+w)*width)), min(height, math.ceil((y+h)*height))
        if right-left < 8 or bottom-top < 8:
            return None
        return self.image(array[top:bottom, left:right].copy()), (
            left/width, top/height, (right-left)/width, (bottom-top)/height)

    def detect(self, request):
        if not isinstance(request, dict):
            raise ValueError('invalid_vision_request')
        started = time.monotonic()
        array = decode_image(request.get('jpeg'))
        image = self.image(array)
        boxes, poses = body_rows(self.pose.detect(image).pose_landmarks)
        body_rescue_passes = 0
        if not poses:
            rois = request.get('person_rois', [])
            if not isinstance(rois, list) or len(rois) > 10:
                raise ValueError('invalid_person_rois')
            regions = []
            for row in rois[:2]:
                if (not isinstance(row, dict) or any(not finite(row.get(k)) for k in ('x', 'y', 'w', 'h'))
                        or not 0 <= row['x'] <= 1 or not 0 <= row['y'] <= 1
                        or not 0 < row['w'] <= 1 or not 0 < row['h'] <= 1):
                    raise ValueError('invalid_person_rois')
                regions.append((row['x']-.04, row['y']-.05, row['w']+.08, row['h']+.1))
            if not regions and (request.get('allow_wide_pose_rescue') is not False or boxes):
                regions = [(0, 0, .6, 1), (.4, 0, .6, 1)]
            for region in regions:
                crop = self.crop(array, region)
                if crop:
                    body_rescue_passes += 1
                    found_boxes, found_poses = body_rows(self.pose.detect(crop[0]).pose_landmarks, crop[1])
                    for box, pose in zip(found_boxes, found_poses):
                        root = pose['joints'].get('root') or pose['joints']['neck']
                        if any(b['x'] <= root['x'] <= b['x']+b['w']
                               and b['y'] <= root['y'] <= b['y']+b['h'] for b in boxes):
                            continue
                        boxes.append(box)
                        poses.append(pose)
        body_ms = (time.monotonic()-started)*1000
        interval = request.get('hand_interval_ms', 0)
        if not finite(interval):
            raise ValueError('invalid_hand_interval')
        sampled = time.monotonic()-self.last_sample >= min(250, max(0, interval))/1000
        hands = []
        if sampled:
            self.sequence += 1
            hands = hand_rows(self.hand.detect(image), self.sequence)
            regions = []
            for pose in poses[:3]:
                for name in ('leftWrist', 'rightWrist'):
                    wrist = pose['joints'].get(name)
                    if wrist is not None:
                        regions.append((wrist['x']-.075, wrist['y']-.11, .15, .22))
            for region in regions[:4]:
                if len(hands) >= 4:
                    break
                crop = self.crop(array, region)
                if crop:
                    for hand in hand_rows(self.hand.detect(crop[0]), self.sequence, crop[1]):
                        wrist = hand['joints']['wrist']
                        if not any(math.hypot(h['joints']['wrist']['x']-wrist['x'],
                                              h['joints']['wrist']['y']-wrist['y']) < .035 for h in hands):
                            hands.append(hand)
                            if len(hands) >= 4:
                                break
            self.last_hands, self.last_sample = hands, time.monotonic()
        elif time.monotonic()-self.last_sample < .28:
            hands = self.last_hands
        hand_ms = (time.monotonic()-started)*1000-body_ms
        faces = []
        if (getattr(self, 'face', None) is not None and request.get('attention_enabled') is True
                and request.get('face_jpeg')):
            from .portable_attention import face_rows
            face_image = decode_image(request['face_jpeg'])
            faces = face_rows(self.face.detect(self.image(face_image)), face_image.shape[1], face_image.shape[0])
        animals = []
        animal_status = 'disabled'
        if request.get('animals_enabled') is True:
            from .scene_detector import SceneDetector
            if self.scene is None:
                self.scene = SceneDetector(self.models.parent/'yolov8n.onnx')
            found = self.scene.detect(base64.b64decode(request['jpeg'], validate=True), animals=True)
            animals = [dict(species=row['class'], box=dict(row['box'], confidence=row['confidence']))
                       for row in found['objects'] if row['class'] in {'cat', 'dog'} and row['confidence'] >= .75][:10]
            animal_status = 'ready'
        # 同一缓存的 sample_id 不变，既有稳定器不能将缓存计作新证据。
        return dict(boxes=boxes, poses=poses, hands=hands, faces=faces,
                    body_ms=body_ms, body_rescue_passes=body_rescue_passes, hand_ms=hand_ms,
                    hands_sampled=sampled, inference_ms=(time.monotonic()-started)*1000,
                    backend='onnx_cpu', attention_supported=getattr(self, 'face', None) is not None,
                    attention_status='unsupported_onnx_iris_geometry',
                    animals=animals, animals_supported=True, animals_status=animal_status)

    def close(self):
        if self.face is not None:
            self.face.close()
        self.hand.close()
        self.pose.close()


def main():
    from .paths import MODELS

    worker = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            worker = PortableVision(MODELS/'opencv-zoo')
        while line := sys.stdin.buffer.readline(PIPE_LIMIT+1):
            if len(line) > PIPE_LIMIT or not line.endswith(b'\n'):
                raise ValueError('vision_request_too_large')
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    result = worker.detect(json.loads(line))
                print(json.dumps(result, allow_nan=False, separators=(',', ':')), flush=True)
            except Exception:
                print(json.dumps(dict(error='vision_request_failed', boxes=[], poses=[])), flush=True)
                return 1
    finally:
        if worker is not None:
            worker.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
