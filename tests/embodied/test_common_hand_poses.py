"""Conservative hand geometry and teaching; synthetic joints, no camera/model."""

import pytest

from homeplane.embodied.gestures import classify_hand
from homeplane.embodied.interaction import Configuration, GESTURES
from homeplane.embodied.practice import RecognitionPractice
from tests.embodied import test_gestures as hand_fixtures


def hand(label):
    helper = hand_fixtures.HandTests()
    joints = helper.hand()
    for name in ("index", "middle", "ring", "little"):
        if label == "shaka" and name == "little" or label == "horns" and name in {"index", "little"}:
            continue
        helper.folded(joints, name)
    def p(x, y):
        return dict(x=x, y=y, confidence=.95)
    if label in {"thumb_up", "thumb_down"}:
        joints.update(thumbCMC=p(.36, .72), thumbMP=p(.35, .59), thumbIP=p(.35, .42), thumbTip=p(.35, .23))
    elif label == "shaka":
        joints.update(thumbCMC=p(.40, .74), thumbMP=p(.30, .68), thumbIP=p(.20, .63), thumbTip=p(.10, .57))
    else:
        joints.update(thumbCMC=p(.36, .72), thumbMP=p(.36, .62), thumbIP=p(.41, .64), thumbTip=p(.45, .68))
    if label == "thumb_down":
        for point in joints.values():
            point.update(x=1-point["x"], y=1-point["y"])
    return joints


@pytest.mark.parametrize("label", ["fist", "thumb_up", "thumb_down", "shaka", "horns"])
@pytest.mark.parametrize("mirrored", [False, True])
@pytest.mark.parametrize("scale", [.65, 1])
def test_poses_survive_mirror_and_scale(label, mirrored, scale):
    joints = hand(label)
    for p in joints.values():
        p["x"] = .5 + (p["x"]-.5)*scale
        p["y"] = .5 + (p["y"]-.5)*scale
        if mirrored:
            p["x"] = 1-p["x"]
    assert classify_hand(joints) == label


@pytest.mark.parametrize("label", ["fist", "thumb_up", "thumb_down", "shaka", "horns"])
def test_missing_low_confidence_or_outside_landmarks_cannot_claim_new_pose(label):
    for key in ("thumbTip", "thumbIP", "indexTip", "littleTip"):
        j = hand(label)
        del j[key]
        assert classify_hand(j) != label
        j = hand(label)
        j[key]["confidence"] = .45
        assert classify_hand(j) != label
        j = hand(label)
        j[key]["x"] = -1
        assert classify_hand(j) != label


def test_sideways_thumb_is_neither_up_nor_down_and_horns_is_not_pointing():
    joints = hand("thumb_up")
    ratio = 16/9
    for p in joints.values():
        x,y=(p["x"]-.5)*ratio,p["y"]-.5
        p["x"],p["y"] = .5-y/ratio,.5+x
    assert classify_hand(joints) == "hand_visible"
    missing = hand("horns")
    del missing["thumbTip"]
    assert classify_hand(missing) == "hand_visible"


@pytest.mark.parametrize("label", ["fist", "thumb_up", "thumb_down", "shaka", "horns"])
def test_every_new_pose_has_same_bounded_rehearsal_and_no_default_binding(label):
    assert 'hand.'+label in GESTURES
    assert Configuration(revision=0).bindings == []
    now = [0.0]
    practice = RecognitionPractice(clock=lambda: now[0])
    token = practice.begin('hand.'+label, ['SYNTHETIC'])['token']
    for at in (0, .2, .4, .6, .8):
        now[0] = at
        practice.observe('SYNTHETIC','session',[{'id':1,'joints':{}}],
                         [{'person_id':1,'gesture':label}],{'status':'locked'},at,at)
    assert practice.status(token)['phase'] == 'success'
    assert practice.suppresses('SYNTHETIC')
    practice.stop(token)
    assert not practice.suppresses('SYNTHETIC')


@pytest.mark.parametrize("label", ["fist", "thumb_up", "thumb_down", "shaka", "horns"])
def test_geometry_to_bound_fleet_event_and_cross_camera_deduplication(monkeypatch, label):
    from homeplane.embodied import interaction, interaction_fleet
    from homeplane.embodied.interaction import Binding
    from tests.embodied import test_interaction_fleet as fleet_fixtures
    config = Configuration(revision=7, enabled=True, bindings=[Binding(
        id='a'*32, gesture='hand.'+label, target='SYNTHETIC-lamp', action='on', enabled=True)])
    monkeypatch.setattr(interaction, 'load', lambda: config)
    monkeypatch.setattr(interaction_fleet, 'load', lambda: config)
    fleet = interaction_fleet.InteractionFleet()
    camera = fleet_fixtures.CAMERAS[0]
    fleet_fixtures.bind(fleet, camera, person='SYNTHETIC-member')
    observed = classify_hand(hand(label))
    assert not fleet_fixtures.update(fleet, camera, 11.3, observed, 'SYNTHETIC-member')
    event = fleet_fixtures.update(fleet, camera, 12.05, observed, 'SYNTHETIC-member')[0]
    assert event['target'] == 'SYNTHETIC-lamp' and event['revision'] == 7
    assert fleet.current(event, 12.1)
    for at in (12.1, 12.5, 12.9):
        assert not fleet_fixtures.update(fleet, fleet_fixtures.CAMERAS[1], at,
                                        observed, 'SYNTHETIC-member', track=99)
    fleet.clear(camera)
    assert not fleet.current(event, 12.2)


@pytest.mark.parametrize("label", ["ok", "yeah", "open_palm", "pointing", "fist",
                                   "thumb_up", "thumb_down", "shaka", "horns"])
def test_slow_samples_complete_requested_hand_lesson(label):
    from homeplane.embodied.gestures import GestureStabilizer
    helper = hand_fixtures.HandTests()
    joints = helper.hand()
    if label in {"fist", "thumb_up", "thumb_down", "shaka", "horns"}:
        joints = hand(label)
    elif label == "ok":
        joints["indexTip"] = dict(joints["thumbTip"])
    elif label in {"pointing", "yeah"}:
        for finger in (("middle", "ring", "little") if label == "pointing" else ("ring", "little")):
            helper.folded(joints, finger)
    raw = classify_hand(joints)
    assert raw == label
    clock = [0.]
    practice = RecognitionPractice(clock=lambda: clock[0])
    gesture = "hand.point" if label == "pointing" else "hand." + label
    token = practice.begin(gesture, ["cam"])["token"]
    stabilizer = GestureStabilizer()
    for idx, at in enumerate([0., .63, 1.27, 1.91]):
        clock[0] = at
        stable = stabilizer.update((1, "left"), raw, joints["wrist"], at, idx)
        practice.observe("cam", "session", [{"id": 1}], [
            {"person_id": 1, "gesture": stable, "ray": {"direction": [1, 0]}}
        ], {"status": "locked"}, at, at)
        assert (practice.status(token)["phase"] == "success") == (idx == 3)


def test_disagreement_or_wrist_jump_breaks_slow_confirmation():
    from homeplane.embodied.gestures import GestureStabilizer
    stable = GestureStabilizer()
    for idx, (label, x) in enumerate([("ok", .4), ("ok", .4), ("fist", .4),
                                       ("ok", .4), ("ok", .4), ("ok", .7)]):
        assert stable.update("hand", label, {"x": x, "y": .5}, idx*.6, idx) == "hand_visible"


@pytest.mark.parametrize("label", ["fist", "thumb_up", "thumb_down", "shaka", "horns"])
def test_observed_curled_ring_does_not_require_its_knuckle(label):
    joints = hand(label)
    del joints["ringMCP"]
    assert classify_hand(joints) == label
    del joints["ringTip"]
    assert classify_hand(joints) != label


@pytest.mark.parametrize('label', ['thumb_down', 'shaka', 'horns'])
@pytest.mark.parametrize('mirrored', [False, True])
def test_distal_thumb_evidence_with_hidden_base(label, mirrored):
    joints = hand(label)
    del joints['thumbCMC']
    if mirrored:
        for point in joints.values():
            point['x'] = 1 - point['x']
    assert classify_hand(joints) == label
    del joints['thumbIP']
    assert classify_hand(joints) != label


@pytest.mark.parametrize('label', ['fist', 'thumb_up'])
def test_hidden_thumb_base_cannot_invert_vertical_direction(label):
    joints = hand(label)
    del joints['thumbCMC']
    assert classify_hand(joints) not in {'thumb_down', 'shaka', 'horns'}


@pytest.mark.parametrize("degrees, expected", [(55, "thumb_down"), (60, "thumb_down"),
                                              (70, "thumb_down"), (80, "hand_visible"),
                                              (90, "hand_visible"), (110, "hand_visible")])
@pytest.mark.parametrize("mirror", [False, True])
def test_downward_diagonal_both_hands(degrees, expected, mirror):
    import math
    joints = hand("thumb_down")
    angle = math.radians(degrees)
    for p in joints.values():
        x, y = (p["x"]-.5)*16/9, p["y"]-.5
        p["x"] = .5 + (.75 if not mirror else -.75)*(x*math.cos(angle)-y*math.sin(angle))*9/16
        p["y"] = .5 + .75*(x*math.sin(angle)+y*math.cos(angle))
    assert classify_hand(joints) == expected


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize(("tip_y", "expected"), [
    (.68, "thumb_down"),  # 末段仅微微向下，但指尖明确落在拳面下方。
    (.64, "hand_visible"),  # 拇指末段水平。
    (.62, "hand_visible"),  # 拇指末段向上。
])
def test_shallow_thumb_below_folded_fist(mirror, tip_y, expected):
    joints = hand("thumb_down")
    for name, x, y in (("thumbCMC", .29, .52), ("thumbMP", .37, .64),
                       ("thumbIP", .45, (.64 + tip_y) / 2), ("thumbTip", .54, tip_y)):
        joints[name].update(x=x, y=y)
    if mirror:
        for point in joints.values():
            point["x"] = 1 - point["x"]
    assert classify_hand(joints) == expected
    del joints["thumbIP"]
    assert classify_hand(joints) != "thumb_down"


@pytest.mark.parametrize("mirror", [False, True])
def test_phone_with_pinky_extended_sideways(mirror):
    joints = hand("shaka")
    joints["littlePIP"].update(x=.673,y=.77)
    joints["littleTip"].update(x=.696,y=.88)
    if mirror:
        for p in joints.values():
            p["x"] = 1-p["x"]
    assert classify_hand(joints) == "shaka"
    joints["littleTip"] = dict(joints["littleMCP"])
    assert classify_hand(joints) != "shaka"


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize(("tip", "expected"), [
    ((.696, .88), "shaka"),        # 小指沿侧面伸直，拇指朝上。
    ((.683, .807), "hand_visible"), # 小指部分伸出，无法确认握回。
    ((.65, .69), "thumb_up"),      # 小指尖已折回指根附近。
])
def test_upright_thumb_depends_on_visible_pinky(tip, expected, mirror):
    joints = hand("thumb_up")
    joints["littlePIP"].update(x=.673, y=.77)
    joints["littleTip"].update(x=tip[0], y=tip[1])
    if mirror:
        for point in joints.values():
            point["x"] = 1 - point["x"]
    assert classify_hand(joints) == expected


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize(("tip_y", "expected"), [
    (.48, "thumb_up"),       # 朝上伸出，但投影离食指根很近。
    (.56, "hand_visible"),   # 正在收回，尚未落到拳面。
])
def test_foreshortened_thumb_near_knuckle_is_not_a_fist(mirror, tip_y, expected):
    joints = hand("thumb_up")
    joints["thumbCMC"].update(x=.42, y=.72)
    joints["thumbMP"].update(x=.43, y=.65)
    joints["thumbIP"].update(x=.43, y=(.65 + tip_y)/2)
    joints["thumbTip"].update(x=.43, y=tip_y)
    if mirror:
        for point in joints.values():
            point["x"] = 1 - point["x"]
    assert classify_hand(joints) == expected


@pytest.mark.parametrize("mirrored", [False, True])
def test_horns_with_pinky_spread_sideways(mirrored):
    joints = hand("horns")
    joints["littlePIP"].update(x=.673, y=.77)
    joints["littleTip"].update(x=.696, y=.88)
    if mirrored:
        for point in joints.values():
            point["x"] = 1 - point["x"]
    assert classify_hand(joints) == "horns"
    joints["littleTip"] = dict(joints["littleMCP"])
    assert classify_hand(joints) != "horns"
    del joints["littleTip"]
    assert classify_hand(joints) != "horns"


def test_horns_accepts_thumb_pressed_against_curled_middle_knuckle():
    joints = hand("horns")
    for name, x, y in (("thumbCMC",.36,.68), ("thumbMP",.38,.59),
                       ("thumbIP",.42,.51), ("thumbTip",.46,.43)):
        joints[name].update(x=x,y=y)
    assert classify_hand(joints) == "horns"
    joints["thumbTip"].update(x=.24,y=.40)
    assert classify_hand(joints) != "horns"
