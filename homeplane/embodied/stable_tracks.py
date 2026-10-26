"""ByteTrack in a background-aligned coordinate system; session IDs, not identity."""

import math

import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from homeplane.embodied.pose_rules import classify

IDENTITY = np.eye(3)


def warp_box(box, transform):
    x, y, w, h = [box[k] for k in ("x", "y", "w", "h")]
    q = np.array([[x, y, 1], [x + w, y, 1], [x, y + h, 1], [x + w, y + h, 1]], dtype=float) @ transform.T
    if np.any(np.abs(q[:, 2]) < 1e-5):
        return None
    q = q[:, :2] / q[:, 2:]
    if not np.isfinite(q).all() or np.max(np.abs(q)) > 6:
        return None
    a = q.min(axis=0)
    b = q.max(axis=0)
    return [a[0], a[1], b[0], b[1]] if np.all(b - a > 0.001) else None


class StableTracks:
    def __init__(self):
        self._space = None
        self._epoch = -1
        self.tracker = self._new_tracker()
        self.items = {}
        self.last_update = None

    @staticmethod
    def _new_tracker():
        return ByteTrackTracker(
            lost_track_buffer=600,
            frame_rate=20.0,
            track_activation_threshold=0.65,
            high_conf_det_threshold=0.5,
            minimum_consecutive_frames=2,
            minimum_iou_threshold=0.08,
        )

    def update(self, detection, now, transform=IDENTITY, *, camera_moving=False):
        if camera_moving:
            self._space = None
            return []
        # Face/body continuity does not require a calibrated floor map. Moving
        # the camera or changing coordinate space always creates new track IDs.
        space = 'image' if transform is None else 'background_reference'
        if space != self._space:
            self.tracker = self._new_tracker()
            self.items.clear()
            self._epoch += 1
            self._space = space
        if transform is None:
            transform = IDENTITY
        transform = np.asarray(transform)
        boxes = []
        xy = []
        for box in detection.get("boxes", [])[:10]:
            if box.get("confidence", 0) < 0.4:
                continue
            world = warp_box(box, transform)
            if world is not None:
                boxes.append(box)
                xy.append(world)
        ds = sv.Detections(
            xyxy=np.array(xy, dtype=np.float32).reshape(-1, 4),
            confidence=np.array([b["confidence"] for b in boxes], dtype=np.float32),
            class_id=np.zeros(len(boxes), dtype=int),
            data={"source_index": np.arange(len(boxes))},
        )
        tracked = self.tracker.update(ds, timestamp=now)
        people = []
        used = set()
        for n, track_id in enumerate(tracked.tracker_id if tracked.tracker_id is not None else []):
            if int(track_id) < 0:
                continue
            track_id = int(track_id) + 1 + self._epoch * 1000000
            index = int(tracked.data["source_index"][n])
            box = boxes[index]
            joints = {}
            candidates = []
            for i, pose in enumerate(detection.get("poses", [])):
                if i in used:
                    continue
                j = pose.get("joints", {})
                c = j.get("root") or j.get("neck")
                if (
                    c
                    and box["x"] - 0.05 <= c["x"] <= box["x"] + box["w"] + 0.05
                    and box["y"] - 0.05 <= c["y"] <= box["y"] + box["h"] + 0.05
                ):
                    candidates.append((len(j), i, j))
            if candidates:
                _, i, joints = max(candidates, key=lambda x: x[0])
                used.add(i)
            prev = self.items.get(track_id)
            labels = classify(joints)
            streak = prev["streak"] + 1 if prev and prev["candidate"] == tuple(labels) else 1
            b = xy[index]
            center = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            anchor = prev["anchor"] if prev else center
            first = prev["first"] if prev else now
            motion = (
                "unknown"
                if now - first < 0.5
                else "moving"
                if math.dist(center, anchor) / max(0.1, now - first) > 0.035
                else "stationary"
            )
            if now - first > 1.5:
                anchor = center
                first = now
            self.items[track_id] = {
                "last": now,
                "streak": streak,
                "candidate": tuple(labels),
                "anchor": anchor,
                "first": first,
            }
            people.append(
                {
                    "id": track_id,
                    "box": box,
                    "joints": joints,
                    "actions": labels if streak >= 3 else ["pose_unknown"],
                    "motion": motion,
                    "tracking": {"backend": "ByteTrack", "space": space, "session_id": True},
                }
            )
        self.items = {k: v for k, v in self.items.items() if now - v["last"] < 30}
        self.last_update = now
        return people
