"""从连续可见的两个人物轨迹截取快速搬动线索。"""

import math
from collections import deque
from dataclasses import dataclass

from .pose_rules import body_track_revision

SAMPLE_INTERVAL = .15
MAX_FRAME_GAP = .55
MAX_CANDIDATE_AGE = 6


@dataclass(frozen=True)
class _Body:
    track: int
    revision: int
    center: tuple[float, float]
    torso: float
    wrists: tuple[tuple[float, float], ...]


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high


def _body(person, aspect):
    if not isinstance(person, dict) or person.get('motion') == 'camera_moving':
        return None
    track, box, joints = person.get('id'), person.get('box'), person.get('joints')
    # 人物靠近时检测框可以部分重叠，仍需有各自独立归属的完整躯干关节。
    revision = body_track_revision(person, allow_overlap=True)
    if (type(track) is not int or track < 1 or revision is None
            or not isinstance(box, dict) or not isinstance(joints, dict)):
        return None
    x, y, w, h = (box.get(key) for key in ('x', 'y', 'w', 'h'))
    if (not _number(box.get('confidence'), .65, 1)
            or not all(_number(value, 0, 1) for value in (x, y, w, h))
            or not (.01 <= x < x+w <= .99 and .01 <= y < y+h <= .99)
            or min(w*aspect, h) < .045):
        return None

    def point(name):
        joint = joints.get(name)
        if not isinstance(joint, dict) or not _number(joint.get('confidence'), .6, 1):
            return None
        px, py = joint.get('x'), joint.get('y')
        if _number(px, x, x+w) and _number(py, y, y+h):
            return px*aspect, py
        return None

    torso = [point(side+part) for part in ('Shoulder', 'Hip') for side in ('left', 'right')]
    if any(joint is None for joint in torso):
        return None
    shoulder, hip = (tuple((a+b)/2 for a,b in zip(*pair)) for pair in (torso[:2], torso[2:]))
    length = math.dist(shoulder, hip)
    if length < .05:
        return None
    center = tuple((a+b)/2 for a,b in zip(shoulder, hip))
    wrists = tuple(joint for side in ('left', 'right') if (joint := point(side+'Wrist')) is not None)
    return _Body(track, revision, center, length, wrists)


def _relative(subject, anchor):
    return tuple(a-b for a,b in zip(subject.center, anchor.center))


def _moves_with_hand_proximity(subjects, anchors):
    scale = subjects[0].torso
    if (any(not .7 <= body.torso/scale <= 1.4 for body in subjects[1:])
            or any(math.dist(body.center, anchors[0].center) > anchors[0].torso*.4 for body in anchors[1:])):
        return False
    baseline, before, during, after = [_relative(subject, anchor) for subject,anchor in zip(subjects, anchors)]
    if math.dist(baseline, before) > scale*.18:
        return False
    dx, dy = after[0]-before[0], after[1]-before[1]
    distance = math.hypot(dx, dy)
    if distance < scale*.75:
        return False
    step_x, step_y = during[0]-before[0], during[1]-before[1]
    projection = (step_x*dx + step_y*dy)/distance
    if not scale*.18 <= projection <= distance-scale*.18 or abs(step_x*dy-step_y*dx)/distance > scale*.25:
        return False
    # 双手靠近是独立的画面线索，不能据此判断接触、力度或照护意图。
    for subject,anchor in zip(subjects[2:], anchors[2:]):
        if len(anchor.wrists) != 2 or max(math.dist(wrist, subject.center) for wrist in anchor.wrists) > scale*1.2:
            return False
    return True


class RapidReviewWindow:
    """每个镜头最多持有四帧，冻结一次动作的起点与后续。"""

    def __init__(self):
        self.samples = deque(maxlen=4)
        self.frames = ()
        self.aspect = None

    def pending(self, now):
        if self.frames and not 0 <= now-self.frames[-1][0] <= MAX_CANDIDATE_AGE:
            self.frames = ()
        return self.frames

    def take(self):
        frames, self.frames = self.frames, ()
        return frames

    def clear(self):
        self.samples.clear()
        self.frames = ()
        self.aspect = None

    def offer(self, people, aspect, captured, jpeg, now):
        if not _number(aspect, .2, 5):
            self.clear()
            return
        if self.aspect is None or not math.isclose(self.aspect, aspect, rel_tol=.01):
            self.clear()
            self.aspect = aspect
        if self.pending(now):
            return
        if not isinstance(people, list) or len(people) != 2:
            self.samples.clear()
            return
        pair = [_body(person, self.aspect) for person in people]
        if any(body is None for body in pair) or pair[0].track == pair[1].track:
            self.samples.clear()
            return
        pair.sort(key=lambda body: body.track)
        if self.samples:
            previous, _, previous_pair = self.samples[-1]
            same_pair = all((a.track, a.revision) == (b.track, b.revision) for a, b in zip(pair, previous_pair))
            if not same_pair or captured-previous > MAX_FRAME_GAP:
                self.samples.clear()
            elif captured-previous < SAMPLE_INTERVAL:
                return
        self.samples.append((captured, jpeg, pair))
        if len(self.samples) < 4 or self.samples[-1][0]-self.samples[1][0] > .85:
            return
        a, b = zip(*(sample[2] for sample in self.samples))
        if _moves_with_hand_proximity(a, b) or _moves_with_hand_proximity(b, a):
            self.frames = tuple((stamp, image) for stamp,image,_ in self.samples)
            self.samples.clear()
