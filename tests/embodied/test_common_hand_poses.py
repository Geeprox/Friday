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
