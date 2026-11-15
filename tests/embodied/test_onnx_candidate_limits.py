"""用合成检测张量验证重叠候选不会挤掉其他目标，不加载模型。"""

from types import SimpleNamespace

import numpy as np
import pytest

from homeplane.embodied import onnx_vision
from homeplane.embodied.vendor.opencv_zoo import mp_handpose, mp_palmdet, mp_persondet, mp_pose


@pytest.mark.parametrize("kind,limit", [("pose", 6), ("hand", 4)])
@pytest.mark.parametrize("extra_targets", [1, 8])
def test_nms_preserves_other_targets_before_landmark_limit(monkeypatch, tmp_path, kind, limit, extra_targets):
    detector_module = mp_persondet if kind == "pose" else mp_palmdet
    landmark_module = mp_pose if kind == "pose" else mp_handpose
    landmark_name = "MPPose" if kind == "pose" else "MPHandPose"
    monkeypatch.setattr(detector_module, "CpuNet", lambda _: SimpleNamespace(
        setPreferableBackend=lambda _: None, setPreferableTarget=lambda _: None))
    calls = []

    def landmark(image, candidate):
        calls.append(candidate)
        if kind == "pose":
            return None, np.zeros((33, 5))
        return np.zeros(132)

    monkeypatch.setattr(landmark_module, landmark_name,
                        lambda *args, **kwargs: SimpleNamespace(infer=landmark))
    worker = (onnx_vision.Pose if kind == "pose" else onnx_vision.Hand)(tmp_path)
    detector = worker.detector
    duplicates = limit + 2
    count = duplicates + extra_targets
    # 得分最高的一组候选全部属于同一目标，其余目标得分稍低但不重叠。
    centers = np.linspace(.05, .95, extra_targets + 1)
    detector.anchors = np.array([[centers[0], .5]] * duplicates
                               + [[x, .5] for x in centers[1:]])
    boxes = np.zeros((1, count, 12 if kind == "pose" else 18))
    boxes[0, :, 2:4] = detector.input_size * .04
    scores = np.linspace(7, 3, count).reshape(1, count, 1)
    found = detector._postprocess([boxes, scores], np.array([256, 256]), np.zeros(2))
    assert len(found) == extra_targets + 1
    monkeypatch.setattr(detector, "infer", lambda _: found)
    result = worker.detect(np.zeros((256, 256, 3), dtype=np.uint8))
    rows = result.pose_landmarks if kind == "pose" else result.hand_landmarks
    assert len(calls) == len(rows) == min(limit, extra_targets + 1)
