"""Finite ski course and kinematics, independent of camera/frame cadence."""

import math
import random
from dataclasses import dataclass
from typing import Literal

SkiTrailId = Literal['first-snow', 'pine-slalom', 'summit']

SKI_COURSES = (
    dict(name="林间初雪", length=60.0, speed=2.4, spacing=4.5,
         gate_width=.48, gates=(0, -.35, .35, 0, .35, -.35)),
    dict(name="松林回弯", length=60.0, speed=2.8, spacing=4.0,
         gate_width=.40, gates=(-.5, .5, -.5, .5, 0, -.5)),
    dict(name="山顶归途", length=60.0, speed=3.2, spacing=3.5,
         gate_width=.34, gates=(.55, -.55, 0, .55, -.55, 0)),
)
SKI_WORLD_TRAVEL = 3.0
SKI_VISIBLE_AHEAD = 30.0
SKI_TREE_REACH = .30


class SkiWarmup:
    """在原地练习左右转向和摆臂，每一步由连续的新画面确认。"""

    steps = ('left', 'right', 'arms', 'ready')

    def __init__(self):
        self.step = 0
        self.frame_at = None
        self.clear_hold()

    def clear_hold(self):
        self.match_at = None
        self.held = 0.0

    def accept_frame(self, captured_at, now):
        if (type(captured_at) not in (int, float) or not 0 <= now - captured_at <= 1.5
                or self.frame_at is not None and captured_at <= self.frame_at):
            return False
        self.frame_at = captured_at
        return True

    def observe(self, player):
        if self.steps[self.step] == 'ready':
            return
        lateral = player['lateral']
        matched = (lateral <= -.35, lateral >= .35,
                   abs(lateral) <= .30 and player['arm_effort'] >= .16)[self.step]
        if not matched:
            self.clear_hold()
            return
        if self.match_at is not None:
            gap = self.frame_at - self.match_at
            self.held = self.held + min(.12, gap) if gap <= .35 else 0.0
        self.match_at = self.frame_at
        if self.held >= self.duration:
            self.step += 1
            self.clear_hold()

    @property
    def duration(self):
        return .35 if self.steps[self.step] == 'arms' else .24

    def public(self):
        return dict(step=self.steps[self.step], fraction=round(self.held / self.duration, 2))


@dataclass(frozen=True)
class SkiFeature:
    id: str
    kind: str
    at: float
    x: float
    course: int
    width: float = 0


@dataclass(frozen=True)
class SkiTrail:
    id: SkiTrailId
    name: str
    sections: tuple
    length: float
    features: tuple[SkiFeature, ...]
    gates: tuple[SkiFeature, ...]
    path: tuple

    def public(self, *, preview=False):
        result = dict(id=self.id, name=self.name, distance=self.length, gate_count=len(self.gates))
        if preview:
            result['path'] = tuple((distance, x) for distance, x, _ in self.path)
        return result


def _build_trail(trail, name, sections, seed):
    length = sum(section['length'] for section in sections)
    gates, start = [], 0.0
    for course, spec in enumerate(sections):
        distances = spec.get('positions', range(8, 8 * (len(spec['gates']) + 1), 8))
        for at, x in zip(distances, spec['gates'], strict=True):
            gates.append(SkiFeature(f"gate-{len(gates) + 1}", "gate", start + at,
                                    x, course, spec["gate_width"]))
        start += spec["length"]
    # A tree may narrow the corridor, never block the traverse between two gates.
    anchors = [(0.0, 0.0), *[(g.at, g.x) for g in gates], (length, 0.0)]
    trees, start, rng = [], 0.0, random.Random(seed)
    for course, spec in enumerate(sections):
        at = start + 12
        while at < start + spec["length"] - 4:
            left, right = next((a, b) for a, b in zip(anchors, anchors[1:]) if a[0] <= at <= b[0])
            lo, hi = sorted((left[1], right[1]))
            choices = [x for x in (-.95, -.65, 0, .65, .95)
                       if x <= lo - .35 or x >= hi + .35]
            trees.append(SkiFeature(f"tree-{len(trees) + 1}", "tree", at, rng.choice(choices), course))
            at += spec["spacing"]
        start += spec["length"]
    features = tuple(sorted((*gates, *trees), key=lambda feature: (feature.at, feature.id)))
    path = ((0, 0, 0), *((gate.at, gate.x, gate.width) for gate in gates), (length, 0, 0))
    return SkiTrail(trail, name, sections, length, features, tuple(gates), path)


SKI_TRAILS = (
    _build_trail('first-snow', '初雪坡', (
        dict(name='初雪坡', length=54.0, speed=2.15, spacing=6.0, gate_width=.54,
             positions=(7, 16, 26, 37, 46), gates=(0, -.20, .24, -.18, 0)),
    ), 23),
    _build_trail('pine-slalom', '松林回转', (
        dict(name='林边起步', length=44.0, speed=2.65, spacing=4.8, gate_width=.44,
             positions=(6, 15, 28, 36), gates=(0, -.38, .38, 0)),
        dict(name='松林连续弯', length=48.0, speed=3.0, spacing=4.2, gate_width=.38,
             positions=(7, 17, 27, 39), gates=(.46, -.47, .48, -.42)),
    ), 31),
    _build_trail('summit', '三段山径', SKI_COURSES, 7),
)
DEFAULT_SKI_TRAIL = SKI_TRAILS[-1]
SKI_LENGTH, SKI_ROUTE, SKI_GATES, SKI_PATH = (
    DEFAULT_SKI_TRAIL.length, DEFAULT_SKI_TRAIL.features, DEFAULT_SKI_TRAIL.gates, DEFAULT_SKI_TRAIL.path,
)


def get_ski_trail(value):
    for trail in SKI_TRAILS:
        if trail.id == value:
            return trail
    raise ValueError('invalid_ski_trail')


def ski_trail_options(*, preview=False):
    return [trail.public(preview=preview) for trail in SKI_TRAILS]


def advance(speed, target, seconds):
    """Exact integral of the same gentle acceleration/deceleration response."""
    tau = .18 if target > speed else .48
    gain = -math.expm1(-seconds / tau)
    return speed + (target - speed) * gain, target * seconds + (speed - target) * tau * gain


def crossing_time(speed, target, distance, limit):
    """Bound a feature/course crossing within this observed interval."""
    lo, hi = 0.0, limit
    for _ in range(40):
        mid = (lo + hi) / 2
        if advance(speed, target, mid)[1] < distance:
            lo = mid
        else:
            hi = mid
    return hi
