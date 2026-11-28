"""用合成关节点验证 Linux 输出可消费，不启动视觉模型或摄像头。"""

import base64
import io
import math
from types import SimpleNamespace as Row

import pytest
from PIL import Image

from homeplane.embodied.gestures import GestureStabilizer, classify_hand
from homeplane.embodied.portable_attention import face_rows
from homeplane.embodied.portable_vision import BODY, HAND, PortableVision, body_rows, decode_image, hand_rows
from homeplane.embodied.pose_rules import classify


def pose():
    rows = [Row(x=.5, y=.5, visibility=.95, presence=.9) for _ in range(33)]
    for name, (x, y) in {
        'leftShoulder': (.4, .35), 'rightShoulder': (.6, .35),
        'leftWrist': (.4, .15), 'rightWrist': (.6, .5),
        'leftHip': (.4, .55), 'rightHip': (.6, .55),
        'leftKnee': (.4, .7), 'rightKnee': (.6, .7),
        'leftAnkle': (.4, .9), 'rightAnkle': (.6, .9),
    }.items():
        rows[BODY[name]].x, rows[BODY[name]].y = x, y
    return rows


def hand():
    points = {name: Row(x=.5, y=.5) for name in HAND}
    points['wrist'] = Row(x=.5, y=.85)
    points['thumbTip'] = Row(x=.3, y=.5)
    for name, x, y in [('index', .44, .6), ('middle', .51, .58), ('ring', .58, .61), ('little', .65, .66)]:
        for joint, offset in [('MCP', 0), ('PIP', -.18), ('DIP', -.3), ('Tip', -.43)]:
            points[name+joint] = Row(x=x, y=y+offset)
    return Row(hand_landmarks=[[points[name] for name in HAND]],
               handedness=[[Row(score=.95, category_name='Left')]])


def jpeg():
    stream = io.BytesIO()
    Image.new('RGB', (128, 96), 'white').save(stream, format='JPEG')
    return base64.b64encode(stream.getvalue()).decode()


@pytest.mark.parametrize('features', [{}, {'AVX':False}, {'AVX':True}])
def test_linux_vision_checks_real_cpu_instruction_requirement(features):
    from homeplane.embodied.portable_models import cpu_support
    result=cpu_support(system='Linux',machine='x86_64',features=features)
    assert result['ready'] is (features.get('AVX') is True)
    assert result['avx_required'] is True


def test_onnx_vision_does_not_load_mediapipe_or_require_avx(monkeypatch, tmp_path):
    import builtins
    from homeplane.embodied import portable_models, onnx_vision
    original = builtins.__import__
    def safe_import(name, *args, **kwargs):
        if name.startswith('mediapipe'):
            pytest.fail('ONNX vision must not import AVX-dependent MediaPipe')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', safe_import)
    monkeypatch.setattr(portable_models, 'require_cpu_support', lambda: pytest.fail('unexpected AVX gate'))
    monkeypatch.setattr(portable_models, 'verify', lambda *args, **kwargs: None)
    monkeypatch.setattr(onnx_vision, 'Pose', lambda _: Row(close=lambda: None))
    monkeypatch.setattr(onnx_vision, 'Hand', lambda _: Row(close=lambda: None))
    worker = PortableVision(tmp_path)
    assert worker.face is None
    worker.close()


def test_body_coordinate_mapping_preserves_existing_pose_rules():
    boxes, poses = body_rows([pose()])
    assert len(boxes) == len(poses) == 1
    assert poses[0]['joints']['root'] == dict(x=.5, y=.55, confidence=.9)
    assert set(classify(poses[0]['joints'])) == {'standing', 'raised_arm'}


def test_low_confidence_or_nonfinite_pose_cannot_supply_actions():
    rows = pose()
    for point in rows:
        point.visibility = .1
    assert body_rows([rows]) == ([], [])
    rows = pose()
    rows[BODY['leftWrist']].x = math.nan
    _, found = body_rows([rows])
    assert 'raised_arm' not in classify(found[0]['joints'])


def test_crop_is_mapped_back_to_whole_image():
    _, poses = body_rows([pose()], (.2, .1, .5, .5))
    wrist = poses[0]['joints']['leftWrist']
    assert wrist['x'] == pytest.approx(.4)
    assert wrist['y'] == pytest.approx(.175)


def test_hand_mapping_uses_existing_gesture_classifier_and_sample_identity():
    detection = hand_rows(hand(), 8)[0]
    assert classify_hand(detection['joints']) == 'open_palm'
    stabilizer = GestureStabilizer()
    for tick in range(6):
        assert stabilizer.update('hand', 'open_palm', detection['joints']['wrist'], tick*.02,
                                 detection['sample_id']) == 'hand_visible'
    assert stabilizer.update('hand', 'open_palm', detection['joints']['wrist'], .13, 9) == 'hand_visible'
    assert stabilizer.update('hand', 'open_palm', detection['joints']['wrist'], .2, 10) == 'open_palm'


@pytest.mark.parametrize('change', ['outside', 'missing', 'nan'])
def test_incomplete_hand_is_not_promoted_to_a_control_gesture(change):
    result = hand()
    if change == 'outside':
        result.hand_landmarks[0][3].x = 1.2
    elif change == 'missing':
        result.hand_landmarks[0].pop()
    else:
        result.handedness[0][0].score = math.nan
    assert hand_rows(result, 1) == []


def test_cached_frame_does_not_become_new_hand_evidence(monkeypatch):
    worker = PortableVision.__new__(PortableVision)
    worker.image = lambda array: array
    worker.pose = Row(detect=lambda _: Row(pose_landmarks=[pose()]))
    calls = []
    worker.hand = Row(detect=lambda _: calls.append(True) or hand())
    worker.sequence, worker.last_hands, worker.last_sample = 0, [], -math.inf
    now = [1.0]
    monkeypatch.setattr('homeplane.embodied.portable_vision.time.monotonic', lambda: now[0])
    request = dict(jpeg=jpeg(), hand_interval_ms=200)
    first = worker.detect(request)
    count = len(calls)
    now[0] += .05
    second = worker.detect(request)
    assert not second['hands_sampled'] and len(calls) == count
    assert second['hands'][0]['sample_id'] == first['hands'][0]['sample_id']


@pytest.mark.parametrize('encoded', ['', 'invalid!', base64.b64encode(b'not an image').decode()])
def test_malformed_frame_rejected(encoded):
    with pytest.raises((ValueError, OSError)):
        decode_image(encoded)


def test_small_or_rotated_face_never_qualifies_as_attention():
    points = [Row(x=.5, y=.5) for _ in range(478)]
    points[0] = Row(x=.48, y=.48)
    points[1] = Row(x=.52, y=.52)
    matrix = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    result = Row(face_landmarks=[points], facial_transformation_matrixes=[matrix])
    rows = face_rows(result, 640, 480)
    assert not rows[0]['gaze_candidate'] and not rows[0]['eyes_clear']
    matrix[0][0] = math.nan
    assert face_rows(result, 640, 480) == []


def test_unused_dip_outside_crop_still_has_observed_open_hand():
    result = hand()
    result.hand_landmarks[0][HAND.index('ringDIP')].x = 1.02
    detection, = hand_rows(result, 1)
    assert 'ringDIP' not in detection['joints']
    assert classify_hand(detection['joints']) == 'open_palm'


@pytest.mark.parametrize('label', ['shaka', 'horns'])
def test_crop_can_recover_weak_full_frame_hand_without_double_counting(label):
    from tests.embodied.test_common_hand_poses import hand as shaped
    from homeplane.embodied.portable_vision import merge_hand

    def estimate(target, presence):
        raw = hand()
        points = shaped(target)
        for name, point in points.items():
            raw.hand_landmarks[0][HAND.index(name)] = Row(x=point['x'], y=point['y'])
        raw.handedness[0][0].presence = presence
        return hand_rows(raw, 3)[0]

    hands = [estimate('fist', .80)]
    merge_hand(hands, estimate(label, .94))
    merge_hand(hands, estimate('fist', .81))
    assert len(hands) == 1 and hands[0]['sample_id'] == 3
    assert classify_hand(hands[0]['joints']) == label


def test_uncertain_chirality_requires_two_separated_body_wrists():
    raw = hand()
    raw.handedness[0][0].score = .58
    left = ('left', {'x':.2, 'y':.3})
    right = ('right', {'x':.5, 'y':.85})
    assert hand_rows(raw, 1) == []
    assert hand_rows(raw, 1, wrists=[right]) == []
    assert hand_rows(raw, 1, wrists=[('left', {'x':.505,'y':.85}), right]) == []
    assert hand_rows(raw, 1, wrists=[left, right])[0]['side'] == 'right'
    raw.handedness[0][0].presence = .6
    assert hand_rows(raw, 1, wrists=[left, right]) == []


def test_near_wrist_crop_includes_spread_fingertips():
    worker = PortableVision.__new__(PortableVision)
    worker.pose = Row(detect=lambda _: Row(pose_landmarks=[pose()]))
    worker.sequence, worker.last_hands, worker.last_sample = 0, [], -math.inf
    crops = []
    def detect(image):
        crops.append(image.shape[:2])
        return Row(hand_landmarks=[], handedness=[])
    worker.hand = Row(detect=detect)
    worker.detect(dict(jpeg=jpeg()))
    height, width = crops[1]
    assert height >= 38 and width >= 38


@pytest.mark.parametrize('extended', [('index', 'little'), ('index',), ('little',), (),
                                      ('index', 'middle'), ('index', 'middle', 'ring', 'little')])
def test_world_horns_requires_both_outer_fingers_and_two_curls(extended):
    import numpy as np
    from homeplane.embodied.onnx_vision import world_horns
    points = [(0, 0, 0), (-.4, .2, 0), (-.6, .5, 0), (-.4, .7, .1), (-.2, .8, .1)]
    for x, name in zip((-.35, -.1, .12, .33), ('index', 'middle', 'ring', 'little')):
        segments = [(1, 0), (1.4, 0), (1.7, 0), (1.9, 0)] if name in extended else [
            (1, 0), (1.3, 0), (1.3, .25), (1.1, .25)]
        points.extend((x, y, z) for y, z in segments)
    expected = extended == ('index', 'little')
    points = np.asarray(points)
    for angle in (0, .8, 1.3):
        c, s = math.cos(angle), math.sin(angle)
        rotated = points @ np.array([[1, 0, 0], [0, c, s], [0, -s, c]])
        assert world_horns(rotated) is expected
        rotated[:, 0] *= -1
        assert world_horns(rotated) is expected
    points[4] = (-1.5, .8, 0)
    assert not world_horns(points)


def test_world_evidence_can_recover_projected_pointing_but_requires_complete_hand():
    from tests.embodied.test_common_hand_poses import hand as fixture_hand
    from tests.embodied.test_gestures import HandTests
    raw = hand()
    joints = fixture_hand('horns')
    HandTests().folded(joints, 'little')
    for name, p in zip(HAND, raw.hand_landmarks[0]):
        if name in joints:
            p.x, p.y = joints[name]['x'], joints[name]['y']
    raw.handedness[0][0].world_horns = True
    row, = hand_rows(raw, 1)
    assert classify_hand(row['joints']) == 'pointing'
    assert classify_hand(row['joints'], world_horns=row['world_horns']) == 'horns'
    raw.hand_landmarks[0][HAND.index('littleDIP')].x = -1
    row, = hand_rows(raw, 2)
    assert 'world_horns' not in row


def test_single_person_mask_requires_connected_torso_wrist_and_palm():
    import numpy as np
    from homeplane.embodied.portable_vision import hand_in_person_mask
    body = body_rows([pose()])[1][0]['joints']
    joints = hand_rows(hand(), 1)[0]['joints']
    mask = np.zeros((100,100),np.uint8)
    mask[20:95,25:77] = 255
    assert hand_in_person_mask(mask, body, joints, 1)
    mask[53:56] = 0
    assert not hand_in_person_mask(mask, body, joints, 1)
    mask[:] = 255
    assert not hand_in_person_mask(mask, body, joints, 1)


def test_mask_association_is_emitted_only_for_fresh_hand_samples(monkeypatch):
    import numpy as np
    worker = PortableVision.__new__(PortableVision)
    def detect(image):
        mask = np.zeros(image.shape[:2], np.uint8)
        height, width = mask.shape
        mask[height//5:height*19//20,width//4:width*4//5] = 255
        return Row(pose_landmarks=[pose()], single_mask=mask)
    worker.pose = Row(detect=detect)
    worker.hand = Row(detect=lambda _: hand())
    worker.crop = lambda *_: None
    worker.sequence, worker.last_hands, worker.last_sample = 0, [], -math.inf
    now = [1.0]
    monkeypatch.setattr('homeplane.embodied.portable_vision.time.monotonic', lambda: now[0])
    request = dict(jpeg=jpeg(), hand_interval_ms=200)
    fresh = worker.detect(request)
    assert fresh['hands'][0]['mask_owner_box'] == fresh['boxes'][0]
    now[0] += .05
    cached = worker.detect(request)
    assert cached['hands'][0]['sample_id'] == fresh['hands'][0]['sample_id']
    assert 'mask_owner_box' not in cached['hands'][0]
