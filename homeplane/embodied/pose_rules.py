"""Temporary track IDs and conservative geometric pose labels, not identity or learned actions."""

import math


def iou(a, b):
    x = max(a["x"], b["x"])
    y = max(a["y"], b["y"])
    area = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - x) * max(
        0, min(a["y"] + a["h"], b["y"] + b["h"]) - y
    )
    return area / max(1e-9, a["w"] * a["h"] + b["w"] * b["h"] - area)


def angle(a, b, c):
    u = (a["x"] - b["x"], a["y"] - b["y"])
    v = (c["x"] - b["x"], c["y"] - b["y"])
    norm = math.hypot(*u) * math.hypot(*v)
    return (
        math.degrees(math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / norm))))
        if norm > 0.0001
        else None
    )


def classify(joints):
    def point(name):
        p = joints.get(name)
        return p if p and p.get("confidence", 0) >= 0.4 else None

    labels = []
    for side in ("left", "right"):
        wrist = point(side + "Wrist")
        shoulder = point(side + "Shoulder")
        if wrist and shoulder and wrist["y"] < shoulder["y"] - 0.07:
            labels.append("raised_arm")
            break
    knees = []
    hip_near_knee = []
    for side in ("left", "right"):
        h = point(side + "Hip")
        k = point(side + "Knee")
        a = point(side + "Ankle")
        if h and k and a:
            v = angle(h, k, a)
            if v is not None:
                knees.append(v)
                hip_near_knee.append(abs(h["y"] - k["y"]) < 0.14)
    if knees:
        if min(knees) > 150:
            labels.append("standing")
        elif max(knees) < 140 and any(hip_near_knee):
            labels.append("sitting")
    return labels or ["pose_unknown"]


class Tracks:
    def __init__(self):
        self.items = {}
        self.next_id = 1

    def update(self, detection, now):
        self.items = {k: v for k, v in self.items.items() if now - v["last"] < 2}
        unused = set(self.items)
        people = []
        used_poses = set()
        for box in detection.get("boxes", [])[:10]:
            if box.get("confidence", 0) < 0.65:
                continue
            matches = [(iou(box, self.items[k]["box"]), k) for k in unused]
            score, track_id = max(matches, default=(0, None))
            if score < 0.15:
                track_id = self.next_id
                self.next_id += 1
            else:
                unused.remove(track_id)
            previous = self.items.get(track_id)
            joints = {}
            candidates = []
            for index, pose in enumerate(detection.get("poses", [])):
                if index in used_poses:
                    continue
                pts = pose.get("joints", {})
                centre = pts.get("root") or pts.get("neck")
                if (
                    centre
                    and box["x"] - 0.05 <= centre["x"] <= box["x"] + box["w"] + 0.05
                    and box["y"] - 0.05 <= centre["y"] <= box["y"] + box["h"] + 0.05
                ):
                    candidates.append((len(pts), index, pts))
            if candidates:
                _, index, joints = max(candidates, key=lambda v: v[0])
                used_poses.add(index)
            labels = classify(joints)
            # Require three consecutive observations to display a geometric pose.
            candidate = tuple(labels)
            streak = previous["streak"] + 1 if previous and previous["candidate"] == candidate else 1
            stable = labels if streak >= 3 else ["pose_unknown"]
            centre = (box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
            motion = "unknown"
            if previous:
                dt = now - previous["first"]
                delta = math.dist(centre, previous["anchor"])
                if dt >= 0.75:
                    motion = "moving" if delta / dt > 0.035 else "stationary"
            first = previous["first"] if previous and now - previous["first"] < 1.5 else now
            anchor = previous["anchor"] if previous and first == previous["first"] else centre
            self.items[track_id] = {
                "box": box,
                "last": now,
                "first": first,
                "anchor": anchor,
                "candidate": candidate,
                "streak": streak,
            }
            people.append({"id": track_id, "box": box, "joints": joints, "actions": stable, "motion": motion})
        return people
