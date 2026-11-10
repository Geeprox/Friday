"""Finite ski course and kinematics, independent of camera/frame cadence."""

import math
import random
from dataclasses import dataclass

SKI_COURSES = (
    dict(name="林间初雪", length=60.0, speed=2.4, spacing=4.5,
         gate_width=.48, gates=(0, -.35, .35, 0, .35, -.35)),
    dict(name="松林回弯", length=60.0, speed=2.8, spacing=4.0,
         gate_width=.40, gates=(-.5, .5, -.5, .5, 0, -.5)),
    dict(name="山顶归途", length=60.0, speed=3.2, spacing=3.5,
         gate_width=.34, gates=(.55, -.55, 0, .55, -.55, 0)),
)
SKI_LENGTH = sum(c["length"] for c in SKI_COURSES)
SKI_WORLD_TRAVEL = 3.0
SKI_VISIBLE_AHEAD = 30.0
SKI_TREE_REACH = .30


@dataclass(frozen=True)
class SkiFeature:
    id: str
    kind: str
    at: float
    x: float
    course: int
    width: float = 0


def _build_route():
    gates, start = [], 0.0
    for course, spec in enumerate(SKI_COURSES):
        for i, x in enumerate(spec["gates"]):
            gates.append(SkiFeature(f"gate-{len(gates) + 1}", "gate", start + 8 + i * 8,
                                    x, course, spec["gate_width"]))
        start += spec["length"]
    # A tree may narrow the corridor, never block the traverse between two gates.
    anchors = [(0.0, 0.0), *[(g.at, g.x) for g in gates], (SKI_LENGTH, 0.0)]
    trees, start, rng = [], 0.0, random.Random(7)
    for course, spec in enumerate(SKI_COURSES):
        at = start + 12
        while at < start + spec["length"] - 4:
            left, right = next((a, b) for a, b in zip(anchors, anchors[1:]) if a[0] <= at <= b[0])
            lo, hi = sorted((left[1], right[1]))
            choices = [x for x in (-.95, -.65, 0, .65, .95)
                       if x <= lo - .35 or x >= hi + .35]
            trees.append(SkiFeature(f"tree-{len(trees) + 1}", "tree", at, rng.choice(choices), course))
            at += spec["spacing"]
        start += spec["length"]
    return tuple(sorted((*gates, *trees), key=lambda feature: (feature.at, feature.id)))


SKI_ROUTE = _build_route()
SKI_GATES = tuple(f for f in SKI_ROUTE if f.kind == "gate")


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
