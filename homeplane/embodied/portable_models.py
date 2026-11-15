"""公开视觉模型的固定来源与摘要；运行时只校验，不进行联网下载。"""

import hashlib
import platform
from pathlib import Path

MODELS = {
    'yamnet.tflite': (
        'https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite',
        '4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf', 4126810),
    'pose_landmarker.task': (
        'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task',
        '59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a', 5777746),
    'hand_landmarker.task': (
        'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task',
        'fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1', 7819105),
    'face_landmarker.task': (
        'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
        '64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff', 3758596),
}


ONNX_MODELS = {
    'handpose_estimation_mediapipe_2023feb.onnx': ('https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/handpose_estimation_mediapipe/handpose_estimation_mediapipe_2023feb.onnx',
        'db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c', 4099621),
    'palm_detection_mediapipe_2023feb.onnx': ('https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/palm_detection_mediapipe/palm_detection_mediapipe_2023feb.onnx',
        '78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c', 3905734),
    'person_detection_mediapipe_2023mar.onnx': ('https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx',
        '47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f', 11990159),
    'pose_estimation_mediapipe_2023mar.onnx': ('https://media.githubusercontent.com/media/opencv/opencv_zoo/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/pose_estimation_mediapipe/pose_estimation_mediapipe_2023mar.onnx',
        '9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f', 5557238),
}

def cpu_support(*, system=None, machine=None, features=None):
    """在加载 MediaPipe 原生库前检查其 AVX 前提，避免无指令集时直接崩溃。"""
    target = (system or platform.system(), machine or platform.machine())
    required = target == ('Linux', 'x86_64')
    if required and features is None:
        import numpy as np
        # 使用独立环境锁定的 NumPy CPU 探测；不读取宿主 ARM 的 /proc/cpuinfo。
        core = getattr(np, '_core', None)
        features = getattr(getattr(core, '_multiarray_umath', None), '__cpu_features__', {})
    ready = not required or (isinstance(features, dict) and features.get('AVX') is True)
    return dict(ready=ready, avx_required=required,
                reason=None if ready else 'mediapipe_cpu_avx_required')


def require_cpu_support():
    """前置失败只返回固定原因，不让原生库以非法指令终止进程。"""
    report = cpu_support()
    if not report['ready']:
        raise ValueError(report['reason'])


def verify(folder, names=None, *, manifest=MODELS):
    for name in manifest if names is None else names:
        path = Path(folder)/name
        _, digest, size = manifest[name]
        if path.is_symlink() or path.stat().st_size != size:
            raise ValueError('portable_model_pin_mismatch')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('portable_model_pin_mismatch')
