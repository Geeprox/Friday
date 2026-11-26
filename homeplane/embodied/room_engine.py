"""Local relative floor projection and conservative 2D seat pointing. No identity inference."""

import copy
import json
import math

import cv2
import numpy as np

from homeplane.embodied.gestures import GestureStabilizer, classify_hand
from homeplane.embodied.pose_rules import iou
from homeplane.embodied.calibration_store import CalibrationStore

DEFAULT = {
    "mode": "unconfigured",
    "metric": False,
    "dynamic_seats": True,
    "camera": None,
    "anchors": [],
    "seats": [],
    "furniture": [],
    "outline": [],
    "note": "尚未标定空间",
}

VIEW_SETTLE_SECONDS = 0.8


def matching_finger_side(body, hand, aspect):
    """只在两套检测的指尖对应唯一且可分辨时返回骨架侧别。"""
    def distance(a, b):
        return math.hypot((a['x'] - b['x']) * aspect, a['y'] - b['y'])

    candidates = []
    for side in ('left', 'right'):
        pairs = []
        for name in ('indexTip', 'littleTip', 'thumbTip'):
            anchor = body.get(side + name[0].upper() + name[1:])
            tip = hand.get(name)
            if anchor and tip and min(anchor.get('confidence', 0), tip.get('confidence', 0)) >= .55:
                pairs.append((anchor, tip))
        if len(pairs) < 2 or any(distance(a, b) > .05 for a, b in pairs):
            continue
        if any(min(distance(a, c), distance(b, d)) > .06
               for i, (a, b) in enumerate(pairs) for c, d in pairs[i+1:]):
            candidates.append(side + 'Wrist')
    return candidates[0] if len(candidates) == 1 else None


def calibration_draft(config):
    """Editing handles on a unit canvas, never an observed household layout."""
    draft = copy.deepcopy(config)
    if not draft.get("anchors"):
        draft["anchors"] = [
            dict(name=f"A{i + 1}", image=list(p), map=list(p))
            for i, p in enumerate(((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)))
        ]
    if not draft.get("seats"):
        draft["seats"] = [
            dict(id=i + 1, image=[x, 0.5], map=[x, 0.5]) for i, x in enumerate((0.25, 0.75))
        ]
    return draft


def load_room(folder, picture, *, factory=None):
    """Load existing calibration offloop; an ordinary frame never saves one."""
    config_path, reference_path = folder / "room-calibration.json", folder / "room-reference.jpg"
    make = factory or Room

    def read(path, budget):
        with path.open("rb") as stream:
            data = stream.read(budget + 1)
        if len(data) > budget:
            raise ValueError("calibration_size_invalid")
        return data

    try:
        try:
            config, reference, _ = CalibrationStore(folder).read()
        except FileNotFoundError:
            pass
        else:
            return make(config, reference)
        config = json.loads(read(config_path, 64 * 1024)) if config_path.exists() else copy.deepcopy(DEFAULT)
        reference = read(reference_path, 2_000_000) if reference_path.exists() else picture
        if not reference_path.exists():
            config = dict(config, mode="unconfigured")
        return make(config, reference)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, cv2.error):
        # Calibration is optional. Preserve its files, and keep the actual body
        # and gesture path available on an unprojected, current reference.
        room = make(copy.deepcopy(DEFAULT), picture)
        room.calibration_error = "calibration_unavailable"
        return room


def project(matrix, p):
    q = matrix @ np.array([p[0], p[1], 1.0], dtype=float)
    if not np.all(np.isfinite(q)) or abs(q[2]) < 1e-7:
        return None
    return [float(q[0] / q[2]), float(q[1] / q[2])]


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("invalid_config")
    anchors = config.get("anchors", [])
    seats = config.get("seats", [])
    if not 4 <= len(anchors) <= 12 or not (0 if config.get("dynamic_seats", True) else 2) <= len(seats) <= 12:
        raise ValueError("invalid_count")
    for obj in anchors + seats:
        for key in ("image", "map"):
            p = obj.get(key)
            if (
                not isinstance(p, list)
                or len(p) != 2
                or any(type(v) not in (int, float) or not 0 <= v <= 1 or not math.isfinite(v) for v in p)
            ):
                raise ValueError("invalid_point")
    if sorted(s.get("id") for s in seats) != list(range(1, len(seats) + 1)):
        raise ValueError("invalid_seats")
    src = np.float32([a["image"] for a in anchors])
    dst = np.float32([a["map"] for a in anchors])
    for pts in (src, dst):
        if cv2.contourArea(cv2.convexHull(pts)) < 0.003:
            raise ValueError("anchors_too_close")
    matrix, _ = cv2.findHomography(src, dst, 0)
    if matrix is None or not np.isfinite(matrix).all() or np.linalg.cond(matrix) > 1e6:
        raise ValueError("invalid_projection")
    error = np.mean([np.linalg.norm(np.array(project(matrix, p)) - q) for p, q in zip(src, dst)])
    if error > 0.06:
        raise ValueError("inconsistent_anchors")
    return matrix


def decode_luma(picture):
    return cv2.imdecode(np.frombuffer(picture, np.uint8), cv2.IMREAD_GRAYSCALE)


class Room:
    def __init__(self, config, reference):
        if not isinstance(config, dict):
            raise ValueError("invalid_config")
        self.calibration_candidate = dict(copy.deepcopy(DEFAULT), **copy.deepcopy(config))
        self.config = copy.deepcopy(self.calibration_candidate)
        # Older automatic "approximate" files carry no manual calibration
        # evidence. Preserve them for the editor, but do not publish their map.
        calibrated = (config.get("mode") == "relative_calibrated"
                      or config.get("mode") == "approximate" and config.get("calibration_source") == "manual")
        self.floor = validate(config) if calibrated and config.get("mode") != "unconfigured" else None
        if self.floor is None:
            self.config.update(copy.deepcopy(DEFAULT))
        self.reference = reference
        self.view_reference = self.room_coverage = None
        self.orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.ref_points, self.ref_desc = self.features(decode_luma(reference), [])
        self.registration = None
        self.reg_checked = 0.0
        self.reg_info = {"status": "checking"}
        self.holds = {}
        self.gestures = GestureStabilizer()
        self.players = {}
        self.pending = {}
        self.next_seat = 1
        self.keyframes = []
        self.keyframe_at = 0
        self.keyframe_center = [0.5, 0.5]
        self.previous_features = None
        self.previous_at = 0.0
        self.previous_visible = False
        self.motion_at = -10.0
        self.command_until = 0.0
        self.reference_generation = 0
        self.reference_stable_since = None

    def expect_motion(self, now, seconds=1.5):
        self.command_until = now + seconds
        self.motion_at = now
        self.registration = None
        self.previous_visible = False
        self.reference_stable_since = None
        self.room_coverage = None
        self.holds.clear()
        self.gestures = GestureStabilizer()

    def estimate(self, pts, desc, ref_pts, ref_desc):
        if desc is None or ref_desc is None or min(len(desc), len(ref_desc)) < 16:
            return None
        pairs = self.matcher.knnMatch(desc, ref_desc, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.72 * p[1].distance]
        if len(good) < 16:
            return None
        src = np.float32([pts[m.queryIdx] for m in good])
        dst = np.float32([ref_pts[m.trainIdx] for m in good])
        h, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 0.006)
        count = int(inliers.sum()) if inliers is not None else 0
        if h is None or count < 14 or count / len(good) < 0.4:
            return None
        return count, h, inliers, src, dst

    def player_seats(self, people, inverse, now):
        for seat in self.players.values():
            seat["active"] = False
        for p in people:
            j = p["joints"]
            anchor = j.get("root") or j.get("neck")
            if anchor is None:
                b = p["box"]
                anchor = {"x": b["x"] + b["w"] / 2, "y": b["y"] + b["h"] * 0.4}
            image = [anchor["x"], anchor["y"]]
            reference = project(self.registration, image) if self.registration is not None else None
            existing = next((s for s in self.players.values() if s["track"] == p["id"]), None)
            if existing is None and reference is not None:
                near = [
                    (math.dist(s["reference"], reference), s)
                    for s in self.players.values()
                    if not s["active"] and s.get("reference") is not None and now - s["last"] < 30
                ]
                dist, seat = min(near, key=lambda x: x[0], default=(1, None))
                if dist < 0.10:
                    existing = seat
            if existing is None:
                old = self.pending.get(p["id"])
                if old is None or now - old["last"] > 0.4:
                    old = {"since": now, "count": 0}
                old.update(last=now, count=old["count"] + 1)
                self.pending[p["id"]] = old
                if now - old["since"] < 0.45 or old["count"] < 3 or self.next_seat > 12:
                    continue
                existing = {"id": self.next_seat, "map": None, "reference": None}
                self.players[self.next_seat] = existing
                self.next_seat += 1
            existing.update(track=p["id"], last=now, active=True, current_image=image)
            if reference is not None:
                existing["reference"] = reference
            if p.get("position"):
                q = p["position"]
                existing["map"] = [q["x"], q["y"]]
                existing["located"] = True
                existing["position_method"] = q["method"]
            else:
                existing["located"] = False
            p["seat"] = existing["id"]
        self.pending = {k: v for k, v in self.pending.items() if now - v["last"] < 1}
        result = []
        for s in self.players.values():
            if now - s["last"] > 60:
                continue
            if not s["active"]:
                s["current_image"] = (
                    project(inverse, s["reference"])
                    if inverse is not None and s.get("reference") is not None
                    else None
                )
            result.append(dict(
                ((k, v) for k, v in s.items() if k not in ("reference", "track", "last")),
                track=s["track"] if s["active"] else None,
            ))
        return result

    def features(self, image, people):
        if image is None:
            raise ValueError("invalid_reference")
        image = cv2.resize(image, (640, 360))
        mask = np.full(image.shape, 255, np.uint8)
        for p in people:
            b = p["box"]
            x1 = int(max(0, b["x"] - 0.04) * 640)
            y1 = int(max(0, b["y"] - 0.05) * 360)
            x2 = int(min(1, b["x"] + b["w"] + 0.04) * 640)
            y2 = int(min(1, b["y"] + b["h"] + 0.05) * 360)
            cv2.rectangle(mask, (x1, y1), (x2, y2), 0, -1)
        kp, desc = self.orb.detectAndCompute(image, mask)
        return np.float32([[q.pt[0] / 640, q.pt[1] / 360] for q in kp]), desc

    def register(self, picture, people, now, *, check_visibility=False):
        due = now - self.reg_checked >= 0.3
        if not due and not check_visibility:
            return
        from .camera_director import visible_scene_luma

        image = decode_luma(picture)
        visible = visible_scene_luma(image) if check_visibility or self.floor is None else False
        if due:
            self._register_luma(image, people, now, visible)
        elif not visible:
            self.previous_visible = False
            self.reference_stable_since = None
        if check_visibility:
            return visible

    def _recover_transient_reference(self, pts, desc, now, still_since):
        from .camera_director import CameraDirector

        if (self.floor is not None or still_since is None
                or now - self.motion_at < VIEW_SETTLE_SECONDS
                or self.command_until and now < self.command_until + CameraDirector.PATROL_CONFIRM_SECONDS):
            self.reference_stable_since = None
            return
        if self.reference_stable_since is None:
            self.reference_stable_since = still_since
        if now - self.reference_stable_since < VIEW_SETTLE_SECONDS:
            return

        # 只更新当前进程中的参考。下一次背景匹配完成后才允许发布新配准。
        self.ref_points, self.ref_desc = pts, desc
        self.reference_generation += 1
        self.reference_stable_since = None
        self.keyframes.clear()
        self.keyframe_at, self.keyframe_center = now, [0.5, 0.5]
        self.holds.clear()
        self.gestures = GestureStabilizer()
        self.players.clear()
        self.pending.clear()
        self.next_seat = 1
        self.reg_info = {"status": "settling"}

    def _register_luma(self, image, people, now, visible):
        self.reg_checked = now
        self.registration = None
        pts, desc = self.features(image, people)
        motion = 0.0
        still_since = None
        if self.previous_features is not None and 0 < now - self.previous_at < 1:
            previous = self.estimate(pts, desc, *self.previous_features)
            if previous is not None:
                _, _, inside, src, dst = previous
                selected = inside.ravel().astype(bool)
                if cv2.contourArea(cv2.convexHull(src[selected])) > 0.035:
                    motion = float(np.median(np.linalg.norm(src[selected] - dst[selected], axis=1)))
                    if motion > 0.004:
                        self.motion_at = now
                    elif visible and self.previous_visible:
                        still_since = self.previous_at
        self.previous_features = (pts, desc)
        self.previous_at = now
        self.previous_visible = visible
        if self.view_reference is not None:
            self.room_coverage = self.view_reference.observe(self, pts, desc, visible, now,
                stable=now >= self.command_until and now - self.motion_at >= VIEW_SETTLE_SECONDS)
        if desc is None or self.ref_desc is None or len(desc) < 16:
            self.reg_info = {"status": "background_unavailable"}
            self._recover_transient_reference(pts, desc, now, still_since)
            return
        best = None
        for ref_pts, ref_desc in [(self.ref_points, self.ref_desc)] + list(reversed(self.keyframes)):
            candidate = self.estimate(pts, desc, ref_pts, ref_desc)
            if candidate is not None and (best is None or candidate[0] > best[0]):
                best = candidate
                n, m, inside, a, b = candidate
                selected = inside.ravel().astype(bool)
                # A distributed, low-residual fit is sufficient. Avoid searching every stored
                # view on each static frame, which otherwise depresses the live frame rate.
                if (
                    n >= 100
                    and n / len(a) > 0.75
                    and cv2.contourArea(cv2.convexHull(a[selected])) > 0.08
                    and np.linalg.cond(m) < 1e5
                ):
                    residuals = [
                        math.dist(project(m, p), q)
                        for p, q in zip(a[selected], b[selected])
                        if project(m, p) is not None
                    ]
                    if residuals and np.median(residuals) < 0.002:
                        break
        if best is None:
            self.reg_info = {
                "status": "moving"
                if motion > 0.004 or now < self.command_until
                else "background_unavailable",
                "motion": round(motion, 5),
            }
            self._recover_transient_reference(pts, desc, now, still_since)
            return
        count, h, inliers, src, dst = best
        selected = inliers.ravel().astype(bool)
        residual = float(
            np.median(
                [np.linalg.norm(np.array(project(h, p)) - q) for p, q in zip(src[selected], dst[selected])]
            )
        )
        spread = cv2.contourArea(cv2.convexHull(src[selected]))
        drift = max(np.linalg.norm(np.array(project(h, p)) - p) for p in ([0, 0], [1, 0], [1, 1], [0, 1]))
        # Rotation of a fixed PTZ can be compensated with background correspondences.
        # Require distributed inliers; a small patch or person cannot define the room.
        if drift > 2.5 or residual > 0.004 or spread < 0.035 or np.linalg.cond(h) > 1e5:
            self.reg_info = {
                "status": "camera_moved",
                "matches": count,
                "drift": round(float(drift), 4),
                "residual": round(residual, 5),
                "spread": round(float(spread), 4),
            }
            self._recover_transient_reference(pts, desc, now, still_since)
            return
        self.reference_stable_since = None
        status = (
            "moving"
            if motion > 0.004 or now < self.command_until
            else "settling"
            if now - self.motion_at < VIEW_SETTLE_SECONDS
            else "locked"
        )
        self.reg_info = {
            "status": status,
            "matches": count,
            "drift": round(float(drift), 4),
            "residual": round(residual, 5),
            "motion": round(motion, 5),
            "compensated": bool(drift > 0.02),
            "keyframes": len(self.keyframes),
        }
        if status == "locked":
            self.registration = h
        else:
            self.holds.clear()
        center = project(h, [0.5, 0.5])
        if (
            drift > 0.08
            and center is not None
            and math.dist(center, self.keyframe_center) > 0.035
            and now - self.keyframe_at > 0.6
        ):
            canonical = [project(h, p) for p in pts]
            keep = [i for i, q in enumerate(canonical) if q is not None and all(-3 < v < 4 for v in q)]
            if len(keep) > 80:
                self.keyframes.append((np.float32([canonical[i] for i in keep]), desc[keep]))
                self.keyframes = self.keyframes[-16:]
                self.keyframe_at = now
                self.keyframe_center = center

    def update(self, picture, people, hands, now, width=1920, height=1080, objects=None, registered=False,
               *, hands_sampled=True):
        if not registered:
            self.register(picture, people, now)
        moving = self.reg_info.get("status") in ("moving", "settling")
        if moving:
            objects = []
        inverse = np.linalg.inv(self.registration) if self.registration is not None else None
        ground = self.floor @ self.registration if self.floor is not None and self.registration is not None else None
        ratio = width / max(1, height)
        observed = []
        for o in sorted(objects or [], key=lambda o: o["confidence"], reverse=True):
            if o["class"] == "person":
                continue
            o = dict(o)
            if o["class"] == "soft_furniture":
                o.update(name="沙发", role="sofa", source="detector_with_living_room_prior")
            else:
                o.update(role=o["class"], source="detector")
            if any(a["role"] == o["role"] and iou(a["box"], o["box"]) > 0.5 for a in observed):
                continue
            b = o["box"]
            left = (
                project(ground, [b["x"], b["y"] + b["h"]])
                if ground is not None
                else None
            )
            right = (
                project(ground, [b["x"] + b["w"], b["y"] + b["h"]])
                if ground is not None
                else None
            )
            o["ground_edge"] = (
                [left, right]
                if left and right and all(-0.15 < v < 1.15 for q in (left, right) for v in q)
                else None
            )
            o["footprint_quality"] = "partial_estimate" if o.get("partial") else "bbox_contact_estimate"
            observed.append(o)
        seats = []
        for s in self.config["seats"]:
            seats.append(dict(s, current_image=project(inverse, s["image"]) if inverse is not None else None))
        for person in people:
            person["position"] = None
            person["hands"] = []
            person["seat"] = None
            person["motion_anchor"] = None
            person["motion_torso"] = None
            j = person["joints"]
            feet = [j[n] for n in ("leftAnkle", "rightAnkle") if n in j and j[n].get("confidence", 0) >= 0.5]
            support = None
            root = j.get("root")
            if root:
                supports = [
                    o
                    for o in observed
                    if o["role"] in ("sofa", "chair")
                    and o["box"]["x"] <= root["x"] <= o["box"]["x"] + o["box"]["w"]
                    and o["box"]["y"] <= root["y"] <= o["box"]["y"] + o["box"]["h"]
                ]
                if supports:
                    support = max(supports, key=lambda o: o["confidence"])
            floor_feet = (
                feet
                if support is None
                else [p for p in feet if p["y"] >= support["box"]["y"] + support["box"]["h"] - 0.025]
            )
            # Gameplay uses observed ankles independently of approximate furniture boxes.
            # A standing player's feet can overlap a sofa box without being occluded.
            motion_feet = [
                j[n] for n in ("leftAnkle", "rightAnkle") if n in j and j[n].get("confidence", 0) >= 0.4
            ]
            if len(motion_feet) == 2 and self.registration is not None:
                center = [sum(p[k] for p in motion_feet) / 2 for k in ("x", "y")]
                reference = project(self.registration, center)
                floor_point = project(ground, center) if ground is not None else None
                if reference and all(-2 <= v <= 3 for v in reference):
                    person["motion_anchor"] = {
                        "reference": reference,
                        "floor": floor_point,
                        "method": "two_visible_ankles",
                    }
            hips = [j[n] for n in ("leftHip", "rightHip") if n in j and j[n].get("confidence", 0) >= 0.4]
            if len(hips) == 2 and self.registration is not None:
                person["motion_torso"] = project(
                    self.registration, [sum(q[k] for q in hips) / 2 for k in ("x", "y")]
                )
            if floor_feet and ground is not None:
                foot = max(floor_feet, key=lambda p: p["y"])
                p = project(ground, [foot["x"], foot["y"]])
                if p and -0.05 <= p[0] <= 1.05 and -0.05 <= p[1] <= 1.05:
                    person["position"] = {
                        "x": p[0],
                        "y": p[1],
                        "method": "visible_ankle",
                        "quality": self.config["mode"],
                    }
            elif support and support["ground_edge"]:
                b = support["box"]
                fraction = max(0.05, min(0.95, (root["x"] - b["x"]) / b["w"]))
                a, z = support["ground_edge"]
                p = [a[k] + fraction * (z[k] - a[k]) for k in (0, 1)]
                if all(0 <= v <= 1 for v in p):
                    person["position"] = {
                        "x": p[0],
                        "y": p[1],
                        "method": "sofa_support_estimate"
                        if support["role"] == "sofa"
                        else "chair_support_estimate",
                        "quality": "estimated",
                        "uncertainty": 0.15,
                    }
            # Seat proximity is a geometric hint, never a persistent player identity.
            center = j.get("root") or j.get("neck")
            if center and inverse is not None and not self.config.get("dynamic_seats", True):
                d = [
                    (
                        math.hypot(
                            (s["current_image"][0] - center["x"]) * ratio, s["current_image"][1] - center["y"]
                        ),
                        s["id"],
                    )
                    for s in seats
                    if s["current_image"]
                ]
                distance, seat = min(d, default=(1, None))
                if distance < 0.16:
                    person["seat"] = seat
        if self.config.get("dynamic_seats", True):
            if self.registration is None:
                seats = []
                for s in self.players.values():
                    if now - s["last"] < 60:
                        seats.append(dict(
                            ((k, v) for k, v in s.items() if k not in ("reference", "track", "last")),
                            active=False, located=False, current_image=None, track=None,
                        ))
            else:
                seats = self.player_seats(people, inverse, now)
        pointing = []
        seen = set()
        for index, hand in enumerate(hands):
            hand["person_id"] = None
            hand["gesture"] = "hand_visible"
            hand.pop("ray", None)
            if not hands_sampled:
                continue
            j = hand["joints"]
            wrist = j.get("wrist")
            if not wrist:
                continue
            matches = []
            for p in people:
                for side in ("leftWrist", "rightWrist"):
                    q = p["joints"].get(side)
                    if q:
                        matches.append((math.hypot((q["x"] - wrist["x"]) * ratio, q["y"] - wrist["y"]), p, side))
            matches.sort(key=lambda match: match[0])
            distance, person, body_side = matches[0] if matches else (1, None, None)
            stable_side = hand["side"]
            # 单手与单人骨架的腕点唯一重合时，侧别预测短暂跳变仍属同一只手。
            if (len(hands) == 1 and len(people) == 1 and distance < .06
                    and (len(matches) == 1 or matches[1][0] - distance > .05)
                    and person["joints"][body_side].get("confidence", 0) >= .55):
                stable_side = body_side.removesuffix("Wrist")
            owner_matched = person is not None and distance <= 0.18
            if not moving and len(people) == 1 and len(hands) == 1:
                if not owner_matched and hand.get("mask_owner_box") == people[0]["box"]:
                    person = people[0]
                    owner_matched = True
                if not people[0]["joints"] and wrist.get("confidence", 0) >= .55:
                    partial = sorted((math.hypot((q["x"] - wrist["x"]) * ratio, q["y"] - wrist["y"]), side)
                                     for side, q in people[0].get("arm_wrists", {}).items())
                    if (partial and partial[0][0] <= .045
                            and (len(partial) == 1 or partial[1][0] - partial[0][0] > .05)):
                        person = people[0]
                        stable_side = partial[0][1].removesuffix("Wrist")
                        owner_matched = True
                finger_side = matching_finger_side(people[0]["joints"], j, ratio)
                if finger_side and (not owner_matched or body_side == finger_side):
                    person = people[0]
                    stable_side = finger_side.removesuffix("Wrist")
                    owner_matched = True
            if owner_matched:
                hand["person_id"] = person["id"]
                person["hands"].append(index)
            else:
                person = None
            raw_gesture = classify_hand(j, ratio, world_horns=hand.get("world_horns") is True)
            hand["raw_gesture"] = raw_gesture
            hand["gesture"] = self.gestures.update(
                (hand["person_id"], stable_side), raw_gesture, wrist, now, hand.get("sample_id")
            )
            if hand["gesture"] != "pointing" or person is None:
                continue

            def length(a, b):
                return math.hypot((a["x"] - b["x"]) * ratio, a["y"] - b["y"])

            base = j.get("indexMCP")
            pip = j.get("indexPIP")
            tip = j.get("indexTip")
            if (
                not base
                or not pip
                or not tip
                or min(base["confidence"], pip["confidence"], tip["confidence"]) < 0.4
            ):
                continue
            if (
                length(base, tip) < 0.012
                or length(wrist, tip) < length(wrist, pip) * 1.12
                or length(base, tip) < length(base, pip) * 1.45
            ):
                continue
            dx = (tip["x"] - base["x"]) * ratio
            dy = tip["y"] - base["y"]
            norm = math.hypot(dx, dy)
            hand["ray"] = {"origin": [tip["x"], tip["y"]], "direction": [dx / norm / ratio, dy / norm]}
            candidates = []
            for seat in seats:
                if (
                    seat["id"] == person["seat"]
                    or seat["current_image"] is None
                    or seat.get("active") is False
                ):
                    continue
                tx = (seat["current_image"][0] - tip["x"]) * ratio
                ty = seat["current_image"][1] - tip["y"]
                d = math.hypot(tx, ty)
                if d < 0.045:
                    continue
                cosine = (tx * dx + ty * dy) / max(1e-8, d * norm)
                angle = math.acos(max(-1, min(1, cosine)))
                if angle < math.radians(24):
                    candidates.append((angle, seat))
            candidates.sort(key=lambda q: q[0])
            if not candidates:
                continue
            ambiguous = len(candidates) > 1 and candidates[1][0] - candidates[0][0] < math.radians(7)
            target = candidates[0][1]
            key = (person["id"], stable_side)
            seen.add(key)
            old = self.holds.get(key)
            if ambiguous:
                self.holds.pop(key, None)
                progress = 0.0
                confirmed = False
            else:
                if not old or old["seat"] != target["id"] or now - old["last"] > 0.3:
                    old = {"seat": target["id"], "since": now, "samples": 0}
                old.update(last=now, samples=old["samples"] + 1)
                self.holds[key] = old
                progress = min(1.0, (now - old["since"]) / 1.0)
                confirmed = progress >= 1 and old["samples"] >= 4
            pointing.append(
                {
                    "person_id": person["id"],
                    "hand": index,
                    "target": target["id"],
                    "target_image": target["current_image"],
                    "origin": [tip["x"], tip["y"]],
                    "progress": round(progress, 2),
                    "confirmed": confirmed,
                    "ambiguous": ambiguous,
                    "angle_degrees": round(math.degrees(candidates[0][0]), 1),
                    "approximate": self.config["mode"] == "approximate",
                }
            )
        current_people = {person["id"] for person in people}
        self.holds = {
            k: v for k, v in self.holds.items()
            if 0 <= now - v["last"] <= 0.3
            and (k in seen if hands_sampled else not moving and k[0] in current_people)
        }
        return {
            "mode": self.config["mode"],
            "position_status": self.config["mode"] if self.floor is not None else "unconfigured",
            "metric": False,
            "note": self.config["note"],
            "registration": self.reg_info,
            "seats": seats,
            "furniture": self.config["furniture"],
            "outline": self.config["outline"],
            "pointing": pointing,
            "observed_furniture": observed,
            "seat_mode": "dynamic_players" if self.config.get("dynamic_seats", True) else "manual",
            "camera": self.config.get("camera", DEFAULT["camera"]),
        }
