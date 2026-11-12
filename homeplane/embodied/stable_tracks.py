"""ByteTrack in a background-aligned coordinate system; session IDs, not identity."""

import math

import numpy as np
import supervision as sv
from trackers import ByteTrackTracker

from homeplane.embodied.pose_rules import classify

IDENTITY = np.eye(3)
TRACK_GAP_SECONDS = 3.0
POSE_GAP_SECONDS = .75
FOLLOW_GAP_SECONDS = 1.5


def valid_point(point):
    return (isinstance(point,dict) and all(type(point.get(k)) in (int,float)
            and math.isfinite(point[k]) and 0 <= point[k] <= 1 for k in ('x','y','confidence'))
            and point['confidence'] >= .4)


def valid_box(box):
    return (isinstance(box,dict) and all(type(box.get(k)) in (int,float)
            and math.isfinite(box[k]) for k in ('x','y','w','h','confidence'))
            and .4 <= box['confidence'] <= 1 and box['x'] >= 0 and box['y'] >= 0
            and box['w'] > 0 and box['h'] > 0 and box['x']+box['w'] <= 1.000001
            and box['y']+box['h'] <= 1.000001)

def _overlapping(a, b):
    width = min(a['x']+a['w'], b['x']+b['w']) - max(a['x'], b['x'])
    height = min(a['y']+a['h'], b['y']+b['h']) - max(a['y'], b['y'])
    # 以较小框为准，保留成人经过婴儿或宠物前方这类大小差异明显的情况。
    return width > 0 and height > 0 and width*height >= .30*min(a['w']*a['h'], b['w']*b['h'])


def assign_poses(detection, boxes):
    """Only a supported pose whose root belongs to one box may supply actions.

    More joints do not resolve two overlapping people. Multiple qualifying
    poses in one box also stay ambiguous; no detector-order choice is made.
    """
    assignments = {i:[] for i in range(len(boxes))}
    ambiguous = set()
    for pose in detection.get('poses',[])[:10]:
        raw = pose.get('joints',{}) if isinstance(pose,dict) else {}
        if not isinstance(raw,dict):
            continue
        joints = {name:p for name,p in raw.items() if valid_point(p)}
        root = joints.get('root') or joints.get('neck')
        if root is None or len(joints)<6:
            continue
        candidates = [i for i,b in enumerate(boxes)
            if b['x']-.05 <= root['x'] <= b['x']+b['w']+.05
            and b['y']-.05 <= root['y'] <= b['y']+b['h']+.05]
        if len(candidates)==1:
            assignments[candidates[0]].append(joints)
        else:
            ambiguous.update(candidates)
    return {i:values[0] for i,values in assignments.items() if len(values)==1 and i not in ambiguous}


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
        self.input_valid = False

    @staticmethod
    def _new_tracker():
        return ByteTrackTracker(
            # The installed tracker converts this budget at30Hz to3 elapsed
            # seconds when timestamp is supplied. Short occlusion still survives.
            lost_track_buffer=90,
            frame_rate=20.0,
            track_activation_threshold=0.65,
            high_conf_det_threshold=0.5,
            minimum_consecutive_frames=2,
            minimum_iou_threshold=0.08,
        )

    def invalidate(self):
        self._space = None
        self.items.clear()
        self.last_update = None
        self.input_valid = False

    def update(self, detection, now, transform=IDENTITY, *, camera_moving=False, occluders=()):
        self.input_valid = False
        if (not isinstance(detection,dict) or not isinstance(detection.get('boxes',[]),list)
                or not isinstance(detection.get('poses',[]),list)):
            self.invalidate()
            return []
        if type(now) not in (int,float) or not math.isfinite(now) or now < 0:
            self.invalidate()
            return []
        if self.last_update is not None:
            if now <= self.last_update:
                if now < self.last_update:
                    self.invalidate()
                return []  # Replayed time is not another pose observation.
            if now-self.last_update > TRACK_GAP_SECONDS:
                self.invalidate()
        if camera_moving:
            self.invalidate()
            self.last_update = now
            return []
        # Face/body continuity does not require a calibrated floor map. Moving
        # the camera or changing coordinate space always creates new track IDs.
        space = 'image' if transform is None else 'background_reference'
        if transform is None:
            transform = IDENTITY
        try:
            transform = np.asarray(transform,dtype=float)
            if transform.shape != (3,3) or not np.isfinite(transform).all() or np.linalg.matrix_rank(transform)<3:
                raise ValueError('registration_invalid')
        except (ValueError,TypeError,np.linalg.LinAlgError):
            self.invalidate()
            return []
        boxes = []
        xy = []
        for box in detection.get("boxes", [])[:10]:
            if not valid_box(box):
                self.invalidate()
                return []
            world = warp_box(box, transform)
            if world is None:
                self.invalidate()
                return []
            boxes.append(box)
            xy.append(world)
        if space != self._space:
            self.tracker = self._new_tracker()
            self.items.clear()
            self._epoch += 1
            self._space = space
        ds = sv.Detections(
            xyxy=np.array(xy, dtype=np.float32).reshape(-1, 4),
            confidence=np.array([b["confidence"] for b in boxes], dtype=np.float32),
            class_id=np.zeros(len(boxes), dtype=int),
            data={"source_index": np.arange(len(boxes))},
        )
        tracked = self.tracker.update(ds, timestamp=now)
        poses = assign_poses(detection,boxes)
        people = []
        for n, track_id in enumerate(tracked.tracker_id if tracked.tracker_id is not None else []):
            if int(track_id) < 0:
                continue
            track_id = int(track_id) + 1 + self._epoch * 1000000
            index = int(tracked.data["source_index"][n])
            box = boxes[index]
            joints = poses.get(index,{})
            prev = self.items.get(track_id)
            # 使用当帧检测框，包括尚未被追踪器确认为新轨迹的框。
            overlapping = (any(i != index and _overlapping(box, other) for i, other in enumerate(boxes))
                           or any(_overlapping(box, other) for other in occluders))
            follow_revision = 0
            follow_changed_at = now if overlapping else None
            if prev:
                broken = (prev['last'] != self.last_update or now-prev['last'] > FOLLOW_GAP_SECONDS
                          or overlapping and not prev['overlapping'])
                follow_revision = prev['follow_revision'] + int(broken)
                follow_changed_at = now if broken or overlapping else prev['follow_changed_at']
            if prev and now-prev['last'] > POSE_GAP_SECONDS:
                prev = None
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
                "overlapping": overlapping,
                "follow_revision": follow_revision,
                "follow_changed_at": follow_changed_at,
            }
            people.append(
                {
                    "id": track_id,
                    "box": box,
                    "joints": joints,
                    "actions": labels if streak >= 3 else ["pose_unknown"],
                    "motion": motion,
                    "tracking": {"backend": "ByteTrack", "space": space, "session_id": True,
                                 "overlapping": overlapping, "follow_revision": follow_revision,
                                 "follow_changed_at": follow_changed_at},
                }
            )
        self.items = {k: v for k, v in self.items.items() if now - v["last"] <= TRACK_GAP_SECONDS}
        self.last_update = now
        self.input_valid = True
        return people
