"""Local relative floor projection and conservative 2D seat pointing. No identity inference."""

import math

import cv2
import numpy as np

from homeplane.embodied.gestures import GestureStabilizer, classify_hand
from homeplane.embodied.pose_rules import iou

DEFAULT = {
    "mode": "approximate",
    "metric": False,
    "dynamic_seats": True,
    "camera": {"map": [0.18, 0.96], "mount": "电视墙右上角吊顶（面向电视）"},
    "anchors": [
        {"name": "前侧地面", "image": [0.40, 0.98], "map": [0.08, 0.94]},
        {"name": "沙发左侧地面", "image": [0.53, 0.65], "map": [0.12, 0.54]},
        {"name": "餐桌左侧地面", "image": [0.665, 0.515], "map": [0.31, 0.24]},
        {"name": "餐桌右侧地面", "image": [0.96, 0.505], "map": [0.92, 0.24]},
    ],
    "seats": [
        {"id": 1, "image": [0.70, 0.36], "map": [0.40, 0.18]},
        {"id": 2, "image": [0.78, 0.36], "map": [0.60, 0.18]},
        {"id": 3, "image": [0.86, 0.40], "map": [0.78, 0.28]},
        {"id": 4, "image": [0.82, 0.49], "map": [0.60, 0.43]},
        {"id": 5, "image": [0.73, 0.49], "map": [0.40, 0.43]},
        {"id": 6, "image": [0.65, 0.41], "map": [0.25, 0.28]},
    ],
    "furniture": [
        {"name": "餐桌", "rect": [0.35, 0.23, 0.30, 0.14]},
        {"name": "沙发", "rect": [0.30, 0.55, 0.46, 0.14]},
        {"name": "电视", "rect": [0.30, 0.92, 0.45, 0.025]},
    ],
    "outline": [[0.05, 0.04], [0.95, 0.04], [0.95, 0.97], [0.05, 0.97]],
    "note": "按米家户型粗标 · 相对坐标 · 座位待校准",
}


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
    if not 4 <= len(anchors) <= 12 or not 2 <= len(seats) <= 12:
        raise ValueError("invalid_count")
    for obj in anchors + seats:
        for key in ("image", "map"):
            p = obj.get(key)
            if (
                not isinstance(p, list)
                or len(p) != 2
                or any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in p)
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


class Room:
    def __init__(self, config, reference):
        self.config = config
        self.floor = validate(config)
        self.reference = reference
        self.orb = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.ref_points, self.ref_desc = self.features(reference, [])
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
        self.motion_at = -10.0
        self.command_until = 0.0

    def expect_motion(self, now, seconds=1.5):
        self.command_until = now + seconds
        self.motion_at = now
        self.registration = None
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
                existing = {"id": self.next_seat, "map": [-1, -1], "reference": None}
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
            result.append({k: v for k, v in s.items() if k not in ("reference", "track", "last")})
        return result

    def features(self, picture, people):
        image = cv2.imdecode(np.frombuffer(picture, np.uint8), cv2.IMREAD_GRAYSCALE)
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

    def register(self, picture, people, now):
        if now - self.reg_checked < 0.3:
            return
        self.reg_checked = now
        self.registration = None
        pts, desc = self.features(picture, people)
        motion = 0.0
        if self.previous_features is not None and now - self.previous_at < 1:
            previous = self.estimate(pts, desc, *self.previous_features)
            if previous is not None:
                _, _, inside, src, dst = previous
                selected = inside.ravel().astype(bool)
                if cv2.contourArea(cv2.convexHull(src[selected])) > 0.035:
                    motion = float(np.median(np.linalg.norm(src[selected] - dst[selected], axis=1)))
                    if motion > 0.004:
                        self.motion_at = now
        self.previous_features = (pts, desc)
        self.previous_at = now
        if desc is None or self.ref_desc is None or len(desc) < 16:
            self.reg_info = {"status": "background_unavailable"}
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
            return
        status = (
            "moving"
            if motion > 0.004 or now < self.command_until
            else "settling"
            if now - self.motion_at < 0.8
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

    def update(self, picture, people, hands, now, width=1920, height=1080, objects=None, registered=False):
        if not registered:
            self.register(picture, people, now)
        moving = self.reg_info.get("status") in ("moving", "settling")
        if moving:
            objects = []
        inverse = np.linalg.inv(self.registration) if self.registration is not None else None
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
                project(self.floor @ self.registration, [b["x"], b["y"] + b["h"]])
                if self.registration is not None
                else None
            )
            right = (
                project(self.floor @ self.registration, [b["x"] + b["w"], b["y"] + b["h"]])
                if self.registration is not None
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
                ground = project(self.floor @ self.registration, center)
                if reference and ground and all(-2 <= v <= 3 for v in ground):
                    person["motion_anchor"] = {
                        "reference": reference,
                        "floor": ground,
                        "method": "two_visible_ankles",
                    }
            hips = [j[n] for n in ("leftHip", "rightHip") if n in j and j[n].get("confidence", 0) >= 0.4]
            if len(hips) == 2 and self.registration is not None:
                person["motion_torso"] = project(
                    self.registration, [sum(q[k] for q in hips) / 2 for k in ("x", "y")]
                )
            if floor_feet and self.registration is not None:
                foot = max(floor_feet, key=lambda p: p["y"])
                p = project(self.floor @ self.registration, [foot["x"], foot["y"]])
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
                        seats.append(dict(s, active=False, located=False, current_image=None))
            else:
                seats = self.player_seats(people, inverse, now)
        pointing = []
        seen = set()
        for index, hand in enumerate(hands):
            hand["person_id"] = None
            hand["gesture"] = "hand_visible"
            j = hand["joints"]
            wrist = j.get("wrist")
            if not wrist:
                continue
            matches = []
            for p in people:
                for side in ("leftWrist", "rightWrist"):
                    q = p["joints"].get(side)
                    if q:
                        matches.append((math.hypot((q["x"] - wrist["x"]) * ratio, q["y"] - wrist["y"]), p))
            distance, person = min(matches, key=lambda a: a[0], default=(1, None))
            if person is not None and distance <= 0.18:
                hand["person_id"] = person["id"]
                person["hands"].append(index)
            else:
                person = None
            raw_gesture = classify_hand(j, ratio)
            hand["raw_gesture"] = raw_gesture
            hand["gesture"] = self.gestures.update(
                (hand["person_id"], hand["side"]), raw_gesture, wrist, now, hand.get("sample_id")
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
            key = (person["id"], hand["side"])
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
        self.holds = {k: v for k, v in self.holds.items() if k in seen and now - v["last"] <= 0.3}
        return {
            "mode": self.config["mode"],
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
