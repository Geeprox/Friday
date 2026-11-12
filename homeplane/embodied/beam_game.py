"""光线保卫队：在本局内标定 L 手势，依据新鲜的关节观测推进游戏。"""

import math
import secrets
from collections import deque


CALIBRATION_MARKS = ((.5, .50), (.25, .50), (.75, .50), (.5, .29), (.5, .71))
TARGET_POOL = ((.25, .33), (.74, .35), (.30, .63), (.71, .62), (.42, .34), (.60, .66), (.77, .49))
TOTAL_TARGETS = 6
HIT_RADIUS = .108
BOARD_ASPECT = 16 / 9
LOCK_SECONDS = .54
CALIBRATE_SECONDS = .46
RELEASE_SECONDS = .25
HIT_SECONDS = .68
MAX_FRAME_AGE = .60
MAX_FRAME_GAP = .36
MIN_FRAME_GAP = .008
SIDES = ('left', 'right')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _delta(a, b):
    return a[0] - b[0], a[1] - b[1]


def _midpoint(a, b):
    return (a[0]+b[0])*.5, (a[1]+b[1])*.5


def _dot(a, b):
    return a[0]*b[0] + a[1]*b[1]


def _length(p):
    return math.hypot(*p)


def _joint(joints, name, aspect):
    point = joints.get(name)
    if not isinstance(point, dict) or not all(
        _number(point.get(key)) and 0 <= point[key] <= 1 for key in ('x', 'y', 'confidence')
    ) or point['confidence'] < .52:
        return None
    return point['x'] * aspect, point['y']


def read_l_pose(joints, aspect):
    """返回 L 的瞄准样本或可见的放手；遮挡和过渡姿势都不算放手。"""
    if not isinstance(joints, dict) or not _number(aspect) or not .5 <= aspect <= 3:
        return 'unseen', None
    shoulders, hips, elbows, wrists = (
        [_joint(joints, side + name, aspect) for side in SIDES]
        for name in ('Shoulder', 'Hip', 'Elbow', 'Wrist')
    )
    if any(point is None for point in shoulders + hips + elbows + wrists):
        return 'unseen', None
    shoulder, hip = _midpoint(*shoulders), _midpoint(*hips)
    scale = math.dist(shoulder, hip)
    if not .085 <= scale <= .63 or hip[1]-shoulder[1] < scale*.65:
        return 'unseen', None
    if all(w[1] > shoulder[1]+.46*scale and w[1] > e[1]-.10*scale
           for w, e in zip(wrists, elbows)):
        return 'released', None
    axes = [_delta(w, e) for w, e in zip(wrists, elbows)]
    lengths = [_length(axis) for axis in axes]
    if not all(.24*scale <= length <= 1.18*scale for length in lengths):
        return 'forming', None
    for upright, across in ((0, 1), (1, 0)):
        v, h = axes[upright], axes[across]
        vl, hl = lengths[upright], lengths[across]
        if (-v[1] < .83*vl or abs(h[0]) < .83*hl
                or abs(_dot(v, h)) > .48*vl*hl
                or h[0] * (shoulder[0]-elbows[across][0]) <= 0):
            continue
        # 横臂手腕应靠近竖臂的肘到前臂中段，避免把两个分离的手臂认作 L。
        contact = _delta(wrists[across], elbows[upright])
        along = _dot(contact, v) / (vl*vl)
        if not -.35 <= along <= .85 or abs(contact[0]*v[1]-contact[1]*v[0])/vl > .28*scale:
            continue
        if any(w[1] < shoulder[1]-.90*scale or w[1] > hip[1]+.08*scale for w in wrists):
            continue
        origin = _midpoint(elbows[upright], wrists[across])
        # 用身体中心消掉大部分站位差，保留少量身体横移来补足肩臂转动。
        # 左右方向由本局标定，适应镜头的镜像设置。
        return 'l', ((origin[0]-.73*hip[0]-.135*aspect) / scale, (origin[1]-hip[1]) / scale)
    return 'forming', None


def _mapping(samples):
    """用四个方向拟合以中心点为原点的变换，拒绝退化或互相矛盾的样本。"""
    center = samples[0]
    xy = [_delta(p, center) for p in samples[1:]]
    screen = [_delta(p, CALIBRATION_MARKS[0]) for p in CALIBRATION_MARKS[1:]]
    xx, yy, cross = (sum(x*x for x, y in xy), sum(y*y for x, y in xy), sum(x*y for x, y in xy))
    determinant = xx*yy-cross*cross
    if min(xx, yy) < .013 or determinant < xx*yy*.36:
        return None
    rows = []
    for axis in (0, 1):
        u = sum(x*d[axis] for (x, _), d in zip(xy, screen))
        v = sum(y*d[axis] for (_, y), d in zip(xy, screen))
        rows.append(((u*yy-v*cross)/determinant, (v*xx-u*cross)/determinant))
    if any(math.dist(tuple(_dot(row, offset) for row in rows), target) > .073
           for offset, target in zip(xy, screen)):
        return None
    return center, rows


class BeamRound:
    def __init__(self, *, sample_targets=None):
        draw = secrets.SystemRandom().sample if sample_targets is None else sample_targets
        self.targets = (CALIBRATION_MARKS[0], *draw(TARGET_POOL, TOTAL_TARGETS-1))
        self.hits = 0
        self.calibration = []
        self.transform = None
        self.samples = deque(maxlen=32)
        self.last_capture = None
        self.aspect = None
        self.event = None
        self.wait_release = False
        self.phase = 'waiting'
        self.retry = False
        self._clear_observation()

    @property
    def complete(self):
        return self.hits == len(self.targets)

    def _clear_observation(self):
        self.samples.clear()
        self.aim = self.release_at = self.lock_at = self.last_l_at = None
        self.charge = self.calibration_hold = 0.

    def interrupt(self, *, remeasure=False, phase='waiting'):
        self._clear_observation()
        self.event = None
        self.phase = phase
        if remeasure:
            self.calibration.clear()
            self.transform = self.aspect = None
            self.wait_release = False
            self.retry = False

    def _observe_calibration(self, signal, sample, at):
        self.phase = 'calibrate'
        if signal != 'l':
            self.samples.clear()
            self.calibration_hold = 0.
            return
        index = len(self.calibration)
        if index:
            x, y = _delta(sample, self.calibration[0])
            horizontal = index <= 2
            ok = abs(x) >= .12 and abs(y) <= abs(x)*.70 if horizontal else (
                y <= -.12 if index == 3 else y >= .12
            ) and abs(x) <= abs(y)*.70
            if index == 2:
                ok = ok and x * (self.calibration[1][0]-self.calibration[0][0]) < 0
            if not ok:
                self.samples.clear()
                self.calibration_hold = 0.
                return
        if self.samples and math.dist(sample, self.samples[0][1]) > .09:
            self.samples.clear()
        self.samples.append((at, sample))
        elapsed = at - self.samples[0][0]
        self.calibration_hold = min(1., elapsed / CALIBRATE_SECONDS)
        if elapsed < CALIBRATE_SECONDS or len(self.samples) < 3:
            return
        self.calibration.append(tuple(sum(p[i] for _, p in self.samples)/len(self.samples) for i in (0, 1)))
        self.samples.clear()
        self.calibration_hold = 0.
        if len(self.calibration) != len(CALIBRATION_MARKS):
            return
        self.transform = _mapping(self.calibration)
        if self.transform is None:
            self.calibration.clear()
            self.retry = True
            return
        self.wait_release = True
        self.phase = 'release'

    def update(self, joints, *, now, captured_at, aspect):
        if self.complete:
            return
        if (not _number(now) or not _number(captured_at) or not 0 <= now-captured_at <= MAX_FRAME_AGE
                or not _number(aspect) or not .5 <= aspect <= 3
                or self.last_capture is not None and captured_at-self.last_capture < MIN_FRAME_GAP):
            self.interrupt()
            return
        if self.aspect is not None and abs(aspect/self.aspect - 1.) > .03:
            self.interrupt(remeasure=True)
        self.aspect = aspect
        previous, self.last_capture = self.last_capture, captured_at
        if previous is not None and captured_at - previous > MAX_FRAME_GAP:
            self.interrupt()
        signal, sample = read_l_pose(joints, aspect)
        if signal == 'unseen':
            self.interrupt()
            return
        if self.transform is None:
            self._observe_calibration(signal, sample, captured_at)
            return
        if self.wait_release:
            self.phase = 'release'
            if signal == 'released':
                self.release_at = captured_at if self.release_at is None else self.release_at
                if (captured_at-self.release_at >= RELEASE_SECONDS
                        and (self.event is None or now-self.event['at'] >= HIT_SECONDS)):
                    self.wait_release = False
                    self.phase = 'ready'
            else:
                self.release_at = None
            return
        if signal != 'l':
            self._clear_observation()
            self.phase = 'ready'
            return
        center, axes = self.transform
        x, y = _delta(sample, center)
        measured = tuple(min(.93, max(.07, .50 + _dot(row, (x, y)))) for row in axes)
        if self.aim is None:
            self.aim = measured
        else:
            alpha = 1 - math.exp(-(captured_at-self.last_l_at)/.075)
            self.aim = tuple(old + alpha * (new-old) for old, new in zip(self.aim, measured))
        self.last_l_at = captured_at
        target = self.targets[self.hits]
        # 未经平滑和显示中的准星都要留在目标内；甩开时立即取消蓄力。
        inside = all(math.hypot((p[0]-target[0])*BOARD_ASPECT, p[1]-target[1]) <= HIT_RADIUS
                     for p in (self.aim, measured))
        if not inside:
            self.phase = 'aim'
            self.lock_at = None
            self.charge = 0.
            return
        self.phase = 'lock'
        self.lock_at = captured_at if self.lock_at is None else self.lock_at
        self.charge = min(1., (captured_at-self.lock_at)/LOCK_SECONDS)
        if self.charge < 1:
            return
        self.hits += 1
        self.event = dict(kind='hit', sequence=self.hits, at=now, point=target)
        self.wait_release = True
        self._clear_observation()
        self.phase = 'complete' if self.complete else 'release'

    def public(self, now):
        fresh = self.last_capture is not None and 0 <= now-self.last_capture <= MAX_FRAME_AGE
        measuring = self.transform is None
        event_age = now - self.event['at'] if self.event else None
        event = {key: value for key, value in self.event.items() if key != 'at'} if (
            event_age is not None and 0 <= event_age <= HIT_SECONDS
        ) else None
        if event is not None:
            event['age'] = round(event_age, 3)
        return dict(
            phase='complete' if self.complete else self.phase if fresh else 'waiting',
            frame_age=round(now-self.last_capture, 3) if fresh else None,
            hits=self.hits, total=len(self.targets), radius=HIT_RADIUS,
            target=None if measuring or self.complete or self.wait_release else self.targets[self.hits],
            aim=None if not fresh or self.aim is None else tuple(round(n, 4) for n in self.aim),
            charge=round(self.charge, 3) if fresh else 0., event=event,
            calibration=dict(step=len(self.calibration), total=len(CALIBRATION_MARKS),
                             mark=CALIBRATION_MARKS[len(self.calibration)] if measuring else None,
                             hold=round(self.calibration_hold, 3) if fresh else 0., retry=self.retry),
        )
