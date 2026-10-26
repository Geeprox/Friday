"""Local, pose-driven game rules. No keyboard input, identity model or calories."""

import collections
import math
import random

from homeplane.embodied.hiit import HIITCircuit, signals
from homeplane.embodied.pose_rules import angle


def clamp(x, a=-1.0, b=1.0):
    return min(b, max(a, x))


def point(j, k):
    p = j.get(k)
    return p if p and p.get("confidence", 0) >= 0.4 else None


def average(a, b):
    return {"x": (a["x"] + b["x"]) / 2, "y": (a["y"] + b["y"]) / 2}


class BodyMotion:
    """Continuous body-relative motion. Mirrored video becomes player-relative input."""

    def __init__(self, mirrored=True):
        self.mirrored = mirrored
        self.samples = collections.deque(maxlen=24)
        self.baseline = []
        self.origin = None
        self.lateral = 0.0
        self.last_anchor = None
        self.anchor_at = -10.0
        self.last_pump = -10.0
        self.pumps = 0
        self.last_swing = -10.0
        self.swings = 0
        self.swing_direction = 1
        self.phase = None
        self.pump_extreme = None
        self.pump_direction = 0
        self.arm_effort = 0.0
        self.arm_speed = 0.0
        self.last_body_at = None
        self.last_result = None
        self.leg_samples = collections.deque(maxlen=70)
        self.leg_origin = None
        self.knee_origin = None
        self.torso_at_anchor = None

    def invalidate_motion(self):
        self.samples.clear()
        self.phase = None
        self.pump_extreme = None
        self.pump_direction = 0
        self.arm_effort = 0.0
        self.arm_speed = 0.0
        self.last_swing = -10.0
        self.last_pump = -10.0
        self.leg_samples.clear()
        self.leg_origin = None
        self.knee_origin = None
        self.anchor_at = -10.0
        self.torso_at_anchor = None

    def update(self, p, now):
        j = p["joints"]
        ls = point(j, "leftShoulder")
        rs = point(j, "rightShoulder")
        lh = point(j, "leftHip")
        rh = point(j, "rightHip")
        if not all((ls, rs, lh, rh)):
            if self.last_result and self.last_body_at is not None and now - self.last_body_at <= 0.18:
                result = dict(self.last_result)
                age = now - self.last_body_at
                result.update(
                    joints=j,
                    pose="unknown",
                    pose_quality={"horizontal": 0.0, "overhead": 0.0},
                    squat_depth=None,
                    arm_effort=round(self.arm_effort * math.exp(-age / 0.10), 3),
                    arm_speed=0.0,
                    observation="brief_joint_gap",
                    exercise_signals={"visible": False},
                )
                return result
            return None
        dt = clamp(now - self.last_body_at, 0.015, 0.25) if self.last_body_at is not None else 0.05
        if self.last_body_at is not None and now - self.last_body_at > 0.3:
            self.invalidate_motion()
        self.last_body_at = now
        shoulder = average(ls, rs)
        hip = average(lh, rh)
        torso = math.hypot((shoulder["x"] - hip["x"]) * 16 / 9, shoulder["y"] - hip["y"])
        if torso < 0.025:
            return None
        anchor = p.get("motion_anchor")
        fresh_ankles = bool(anchor and anchor.get("method") == "two_visible_ankles")
        torso_reference = p.get("motion_torso")
        torso_follow = False
        if fresh_ankles:
            self.last_anchor = anchor
            self.anchor_at = now
            self.torso_at_anchor = torso_reference
        located = fresh_ankles or (self.last_anchor is not None and now - self.anchor_at <= 0.18)
        anchor = anchor if fresh_ankles else self.last_anchor if located else None
        if (
            not fresh_ankles
            and self.origin is not None
            and self.last_anchor
            and self.last_anchor.get("reference")
            and torso_reference
            and self.torso_at_anchor
            and now - self.anchor_at <= 1.0
        ):
            shift = [torso_reference[i] - self.torso_at_anchor[i] for i in (0, 1)]
            if abs(shift[0]) < 0.22 and abs(shift[1]) < 0.3:
                anchor = dict(
                    self.last_anchor, reference=[self.last_anchor["reference"][i] + shift[i] for i in (0, 1)]
                )
                located = True
                torso_follow = True
        sign = -1 if self.mirrored else 1
        if located:
            # The floorplan remains an approximate rotated projection. Steering follows
            # the background-aligned camera axis so its boundaries cannot pause a game.
            x = anchor.get("reference", anchor["floor"])[0]
            if self.origin is None:
                self.baseline.append((now, x))
                self.baseline = self.baseline[-30:]
                if (
                    len(self.baseline) >= 8
                    and now - self.baseline[0][0] >= 0.5
                    and max(v for _, v in self.baseline) - min(v for _, v in self.baseline) < 0.035
                ):
                    self.origin = sum(v for _, v in self.baseline) / len(self.baseline)
            if self.origin is not None:
                gain = 1 - math.exp(-dt / 0.09)
                self.lateral += (clamp(sign * (x - self.origin) / 0.12) - self.lateral) * gain
        wrists = {side: point(j, side + "Wrist") for side in ("left", "right")}
        local = {
            side: (sign * (w["x"] - shoulder["x"]) * 16 / 9 / torso, (w["y"] - shoulder["y"]) / torso)
            for side, w in wrists.items()
            if w
        }
        self.samples.append({"at": now, "wrists": local})
        recent = [s for s in self.samples if now - s["at"] <= 0.20]
        speeds = {}
        velocities = {}
        travels = {}
        for side, w in local.items():
            series = [s for s in recent if side in s["wrists"]]
            if len(series) < 2:
                continue
            distance = 0.0
            duration = 0.0
            for a, b in zip(series, series[1:]):
                step = b["at"] - a["at"]
                if 0.012 <= step <= 0.20:
                    # Remove small frame-to-frame joint jitter; do not cancel a complete fast
                    # outward/return motion just because its net displacement approaches zero.
                    distance += min(1.8, max(0.0, math.dist(a["wrists"][side], b["wrists"][side]) - 0.025))
                    duration += step
            if duration < 0.025:
                continue
            speed = min(12.0, distance / duration)
            speeds[side] = speed
            travels[side] = distance
            a = series[-2]
            delta = max(0.015, now - a["at"])
            velocities[side] = ((w[0] - a["wrists"][side][0]) / delta, (w[1] - a["wrists"][side][1]) / delta)
        # Both same-direction and alternating hands contribute positive motion energy.
        raw_speed = sum(speeds.values()) / 2.0
        self.arm_speed = raw_speed
        raw_effort = clamp((raw_speed - 0.32) / 4.5, 0, 1)
        gain = 1 - math.exp(-dt / (0.10 if raw_effort > self.arm_effort else 0.16))
        self.arm_effort += (raw_effort - self.arm_effort) * gain
        moving = [s for s in speeds if speeds[s] > 2.0 and travels.get(s, 0) > 0.16]
        if moving and now - self.last_swing > 0.18:
            side = max(moving, key=lambda s: speeds[s])
            self.last_swing = now
            self.swings += 1
            self.swing_direction = 1 if velocities[side][0] >= 0 else -1
        # Rhythm is diagnostic only; it never gates the forward speed.
        rhythm_side = "left" if "left" in local else "right" if "right" in local else None
        if rhythm_side:
            y = local[rhythm_side][1]
            if self.pump_extreme is None:
                self.pump_extreme = y
            delta = y - self.pump_extreme
            direction = 1 if delta > 0.16 else -1 if delta < -0.16 else 0
            if direction and direction != self.pump_direction:
                if self.pump_direction and now - self.last_pump > 0.12:
                    self.pumps += 1
                    self.last_pump = now
                self.pump_direction = direction
                self.pump_extreme = y
            elif self.pump_direction > 0:
                self.pump_extreme = max(self.pump_extreme, y)
            elif self.pump_direction < 0:
                self.pump_extreme = min(self.pump_extreme, y)
        width = max(0.01, abs(ls["x"] - rs["x"]) * 16 / 9)
        horizontal = []
        overhead = []
        for side, s in [("left", ls), ("right", rs)]:
            w = wrists[side]
            if not w:
                horizontal.append(0.0)
                overhead.append(0.0)
                continue
            outward = (w["x"] - s["x"]) * (s["x"] - shoulder["x"]) > 0
            extension = clamp(abs(w["x"] - s["x"]) * 16 / 9 / (width * 0.65), 0, 1) if outward else 0.0
            horizontal.append(clamp(1 - abs(w["y"] - s["y"]) / (torso * 0.95), 0, 1) * extension)
            overhead.append(clamp((s["y"] - w["y"]) / torso / 0.38, 0, 1))
        quality = {"horizontal": min(horizontal), "overhead": min(overhead)}
        knees = []
        ankles = []
        for side in ("left", "right"):
            h, k, a = (point(j, side + n) for n in ("Hip", "Knee", "Ankle"))
            if h and k and a:
                v = angle(*[{**q, "x": q["x"] * 16 / 9} for q in (h, k, a)])
                if v is not None:
                    knees.append(v)
            if a:
                ankles.append(a)
        # Do not divide by the changing torso projection: leaning into a deep squat
        # foreshortens the torso and can otherwise cancel the measured leg lowering.
        leg = (sum(a["y"] for a in ankles) / 2 - hip["y"]) if len(ankles) == 2 else None
        mean_knee = sum(knees) / 2 if len(knees) == 2 else None
        if leg is not None and leg > 0.025 and mean_knee is not None and mean_knee > 140:
            self.leg_samples.append((now, leg, mean_knee))
            if len(self.leg_samples) >= 6 and now - self.leg_samples[0][0] >= 0.35:
                # Use the upper quartile of observed upright extension, not the deepest
                # crouch, as each view's standing reference.
                ranked = sorted(self.leg_samples, key=lambda q: q[1])
                upper = ranked[len(ranked) // 2 :]
                candidate = sum(v[1] for v in upper) / len(upper)
                knee = sum(v[2] for v in upper) / len(upper)
                if self.leg_origin is None:
                    self.leg_origin = candidate
                    self.knee_origin = knee
                elif candidate > self.leg_origin:
                    self.leg_origin = self.leg_origin * 0.97 + candidate * 0.03
                    self.knee_origin = max(self.knee_origin * 0.97 + knee * 0.03, self.knee_origin - 0.05)
        depth = None
        if leg is not None and mean_knee is not None and self.leg_origin:
            compression = clamp((1 - leg / self.leg_origin) / 0.12, 0, 1)
            bend = clamp((self.knee_origin - mean_knee) / 32, 0, 1)
            depth = min(compression, bend / 0.65)  # Require both lowering and bilateral knee flexion.
        pose = (
            "overhead"
            if quality["overhead"] >= 0.65
            else "horizontal"
            if quality["horizontal"] >= 0.60
            else "squat"
            if depth is not None and depth >= 0.65
            else "standing"
            if depth is not None and depth <= 0.24
            else "standing"
            if mean_knee is not None and mean_knee > 160
            else "neutral"
        )
        result = {
            "id": p["id"],
            "seat": p.get("seat"),
            "track": p["id"],
            "lateral": round(self.lateral, 3),
            "calibrated": self.origin is not None,
            "floor_x": anchor["floor"][0] if located else None,
            "located": located,
            "foot_observation": "fresh"
            if fresh_ankles
            else "torso_follow"
            if torso_follow
            else "brief_hold"
            if located
            else "missing",
            "pose": pose,
            "pose_quality": {k: round(v, 3) for k, v in quality.items()},
            "squat_depth": round(depth, 3) if depth is not None else None,
            "squat_calibrated": self.leg_origin is not None,
            "squat_metrics": {
                "leg_span": round(leg, 4) if leg is not None else None,
                "standing_span": round(self.leg_origin, 4) if self.leg_origin else None,
                "knee_angle": round(mean_knee, 1) if mean_knee is not None else None,
                "standing_knee": round(self.knee_origin, 1) if self.knee_origin else None,
            },
            "arm_speed": round(raw_speed, 3),
            "arm_effort": round(self.arm_effort, 3),
            "observed_wrists": len(local),
            "pumps": self.pumps,
            "last_pump": self.last_pump,
            "swings": self.swings,
            "last_swing": self.last_swing,
            "swing_direction": self.swing_direction,
            "swing_speed": round(max(speeds.values(), default=0), 2),
            "joints": j,
            "observation": "fresh",
            "exercise_signals": signals(j, quality, depth, self.leg_origin, self.knee_origin),
        }
        self.last_result = result
        return result


class MotionGames:
    def __init__(self):
        self.mode = "off"
        self.selected_mode = "quest"
        self.gesture_key = None
        self.gesture_since = None
        self.gesture_last = None
        self.gesture_latched = False
        self.gesture_release = None
        self.gesture_fired = -10.0
        self.gesture_progress = 0.0
        self.gesture_event = None
        self.readers = {}
        self.slots = []
        self.previous = None
        self.reset("off")

    def reset(self, mode):
        if mode not in ("off", "quest", "ski", "tennis", "hiit", "home"):
            raise ValueError("invalid_game")
        if mode != "off":
            self.selected_mode = mode
        self.mode = mode
        self.readers = {}
        self.slots = []
        self.preferred_track = None
        self.previous = None
        self.missing_since = None
        self.elapsed = 0.0
        self.active = 0.0
        self.status = "waiting"
        self.reason = "等待玩家"
        self.rng = random.Random(7)
        self.level = 0
        self.hold = 0.0
        self.squat_down = False
        self.reps = 0
        self.pose_since = None
        self.last_pose = None
        self.pose_gap = 0.0
        self.pose_feedback = ""
        self.down_since = None
        self.up_since = None
        self.speed = 0.0
        self.distance = 0.0
        self.obstacles = []
        self.spawn_distance = 0.0
        self.collisions = 0
        self.passed = 0
        self.score = [0, 0]
        self.rally = 0
        self.ball = {"x": 0.5, "z": 0.5, "target_x": 0.5, "toward": 0, "speed": 0.42}
        self.hit_flash = -10.0
        self.round_pause = 0.0
        self.circuit = HIITCircuit()
        self.publish([], "off" if mode == "off" else "waiting", "OK 保持 1 秒开始 · Yeah 保持 1 秒切换")

    def select(self, mode):
        if mode not in ("quest", "ski", "tennis", "hiit", "home"):
            raise ValueError("invalid_game")
        self.reset("off")
        self.selected_mode = mode
        return self.publish([], "off", "OK 保持 1 秒开始 · Yeah 保持 1 秒切换")

    def gesture_control(self, people, hands, registration, now):
        visible = {p["id"] for p in people if len(p.get("joints", {})) >= 8}
        candidates = {
            (h.get("person_id"), h.get("gesture"))
            for h in hands
            if h.get("person_id") in visible
            and h.get("gesture") in ("ok", "yeah")
            and (self.preferred_track is None or h.get("person_id") == self.preferred_track)
        }
        candidate = (
            next(iter(candidates))
            if len(candidates) == 1 and registration.get("status") == "locked"
            else None
        )
        if self.gesture_last is not None and now - self.gesture_last > 0.25:
            self.gesture_since = None
            self.gesture_key = None
        self.gesture_last = now
        self.gesture_progress = 0.0
        if candidate is None:
            self.gesture_since = None
            self.gesture_key = None
            self.gesture_release = now if self.gesture_release is None else self.gesture_release
            if now - self.gesture_release >= 0.5:
                self.gesture_latched = False
            return
        self.gesture_release = None
        if self.gesture_latched or now - self.gesture_fired < 2:
            return
        if candidate != self.gesture_key:
            self.gesture_key = candidate
            self.gesture_since = now
        self.gesture_progress = min(1.0, max(0.0, now - self.gesture_since))
        if self.gesture_progress < 1:
            return
        self.gesture_latched = True
        self.gesture_fired = now
        if candidate[1] == "yeah":
            modes = ("quest", "ski", "tennis", "hiit", "home")
            self.select(modes[(modes.index(self.selected_mode) + 1) % len(modes)])
            self.preferred_track = candidate[0]
            self.gesture_event = "switched"
        elif (
            self.mode in ("off", "home")
            or self.status == "waiting"
            or (self.mode == "quest" and self.level >= 3)
            or (self.mode == "hiit" and self.circuit.complete)
        ):
            self.reset(self.selected_mode)
            self.preferred_track = candidate[0]
            self.gesture_event = "started"

    def update(self, people, registration, now, hands=()):
        dt = clamp(now - self.previous, 0, 0.12) if self.previous is not None else 0.0
        self.previous = now
        self.gesture_control(people, hands, registration, now)
        if self.mode == "off":
            return self.publish([], "off", "OK 保持 1 秒开始 · Yeah 保持 1 秒切换")
        if self.mode == "home":
            return self.publish([], "playing", "智能家居已启用 · 双臂水平开合")
        required = 2 if self.mode == "tennis" else 1
        if registration.get("status") != "locked":
            for reader in self.readers.values():
                reader.invalidate_motion()
            return self.publish([], "paused", "镜头转动或背景未对齐")
        observed = []
        for p in people:
            key = p["id"]
            reader = self.readers.setdefault(key, BodyMotion())
            result = reader.update(p, now)
            if result:
                observed.append(result)
        by_id = {p["id"]: p for p in observed}
        eligible = [p for p in observed if p["located"]]
        if not self.slots:
            if required == 1 and eligible:
                choice = (
                    next((p for p in eligible if p["id"] == self.preferred_track), None)
                    if self.preferred_track is not None
                    else max(
                        eligible,
                        key=lambda p: (
                            max(q["y"] for q in p["joints"].values())
                            - min(q["y"] for q in p["joints"].values())
                        ),
                    )
                )
                if choice:
                    self.slots = [choice["id"]]
            elif required == 2 and len(eligible) == 2:
                self.slots = [p["id"] for p in sorted(eligible, key=lambda p: p["floor_x"], reverse=True)]
        selected = [by_id[k] for k in self.slots if k in by_id]
        if len(selected) != required:
            self.missing_since = now if self.missing_since is None else self.missing_since
            # Stable tracking owns reassociation. A different sole bystander must never
            # inherit the active player slot; an explicit OK can choose a new player.
        else:
            self.missing_since = None
        if len(selected) != required:
            return self.publish(selected, "waiting", f"需要 {required} 位可见玩家；当前 {len(selected)} 位")
        if any(not p["located"] for p in selected):
            return self.publish(selected, "paused", "请让双脚完整入镜")
        if any(not p["calibrated"] for p in selected):
            return self.publish(selected, "calibrating", "站定约 1 秒，记录左右移动原点")
        self.elapsed += dt
        if self.mode == "quest":
            self.quest(selected[0], dt)
        elif self.mode == "ski":
            self.ski(selected[0], dt, now)
        elif self.mode == "tennis":
            self.tennis(selected, dt, now)
        elif self.mode == "hiit":
            self.circuit.update(selected[0], dt)
        return self.publish(
            selected,
            "complete"
            if (self.mode == "quest" and self.level >= 3) or (self.mode == "hiit" and self.circuit.complete)
            else "playing",
            "动作驱动中",
        )

    def quest(self, p, dt):
        if self.level >= 3:
            return
        target = ("horizontal", "overhead", "squat")[self.level]
        if self.level < 2:
            quality = p.get("pose_quality", {}).get(target, 1.0 if p["pose"] == target else 0.0)
            if quality >= 0.60:
                self.hold += dt
                self.active += dt
                self.pose_gap = 0.0
                self.pose_feedback = "动作已到位，继续保持"
            else:
                self.pose_gap += dt
                self.pose_feedback = "双手抬高到肩部附近" if self.level == 0 else "双手举过肩部"
                if self.pose_gap > 0.30:
                    self.hold = max(0.0, self.hold - dt * 1.5)
            if self.hold >= 3:
                self.level += 1
                self.hold = 0.0
                self.pose_gap = 0.0
        else:
            depth = p.get("squat_depth")
            if depth is None:
                self.pose_feedback = "先站直约半秒，记录你的浅蹲起点"
                return
            if depth >= 0.65:
                self.down_since = (self.down_since or 0) + dt
                self.up_since = None
                self.active += dt
                if self.down_since >= 0.09:
                    self.squat_down = True
            elif depth <= 0.24:
                self.up_since = (self.up_since or 0) + dt
                self.down_since = None
                if self.up_since >= 0.10 and self.squat_down:
                    self.reps += 1
                    self.squat_down = False
            else:
                self.down_since = None
                self.up_since = None
            self.pose_feedback = "已蹲下，站起完成这一次" if self.squat_down else "屈膝浅蹲，再站直"
            if self.reps >= 3:
                self.level = 3

    def ski(self, p, dt, now):
        effort = clamp(p.get("arm_effort", 0), 0, 1)
        target = effort * 3.2
        gain = 1 - math.exp(-dt / (0.18 if target > self.speed else 0.48))
        self.speed += (target - self.speed) * gain
        if self.speed < 0.005:
            self.speed = 0.0
        if effort > 0.03:
            self.active += dt
        travel = self.speed * dt
        self.distance += travel
        self.spawn_distance += travel
        if self.spawn_distance > 3.2:
            self.spawn_distance = 0.0
            self.obstacles.append({"x": self.rng.choice([-0.65, 0, 0.65]), "z": 12.0, "hit": False})
        for o in self.obstacles:
            o["z"] -= travel * 3
            if not o["hit"] and o["z"] < 0.6:
                o["hit"] = True
                if abs(o["x"] - p["lateral"]) < 0.3:
                    self.collisions += 1
                    self.speed *= 0.4
                else:
                    self.passed += 1
        self.obstacles = [o for o in self.obstacles if o["z"] > -0.3]

    def tennis(self, players, dt, now):
        if self.round_pause > 0:
            self.round_pause -= dt
            return
        b = self.ball
        direction = -1 if b["toward"] == 0 else 1
        b["z"] += direction * b["speed"] * dt
        b["x"] += (b["target_x"] - b["x"]) * dt * 0.4
        receiver = b["toward"]
        p = players[receiver]
        px = 0.5 + p["lateral"] * 0.4
        near = b["z"] < 0.10 if receiver == 0 else b["z"] > 0.90
        if near and abs(b["x"] - px) < 0.20 and now - p["last_swing"] < 0.35 and now - self.hit_flash > 0.4:
            b["toward"] = 1 - receiver
            b["z"] = 0.12 if receiver == 0 else 0.88
            b["target_x"] = clamp(px + p["swing_direction"] * 0.27, 0.12, 0.88)
            b["speed"] = min(0.65, b["speed"] + 0.015)
            self.rally += 1
            self.hit_flash = now
            self.active += 0.3
        elif b["z"] < 0 or b["z"] > 1:
            self.score[1 - receiver] += 1
            self.rally = 0
            b.update(x=0.5, z=0.5, target_x=self.rng.uniform(0.15, 0.85), toward=receiver, speed=0.42)
            self.round_pause = 1.2

    def publish(self, players, status, reason):
        self.status = status
        self.reason = reason
        self.public = {
            "mode": self.mode,
            "selected_mode": self.selected_mode,
            "owner_track": self.preferred_track,
            "input_mirrored": True,
            "version": "0.12.1",
            "gesture_control": {
                "gesture": self.gesture_key[1] if self.gesture_key else None,
                "progress": round(self.gesture_progress, 2),
                "latched": self.gesture_latched,
                "last_event": self.gesture_event,
            },
            "status": status,
            "reason": reason,
            "players": players,
            "elapsed": round(self.elapsed, 2),
            "active_seconds": round(self.active, 2),
            "hiit": self.circuit.public(),
            "quest": {
                "level": self.level,
                "hold": round(self.hold, 2),
                "reps": self.reps,
                "targets": ["horizontal", "overhead", "squat"],
                "required_hold": 3,
                "required_reps": 3,
                "feedback": self.pose_feedback,
                "depth": players[0].get("squat_depth") if players else None,
            },
            "ski": {
                "speed": round(self.speed, 3),
                "distance": round(self.distance, 2),
                "obstacles": [dict(o) for o in self.obstacles],
                "collisions": self.collisions,
                "passed": self.passed,
            },
            "tennis": {"ball": dict(self.ball), "score": list(self.score), "rally": self.rally},
        }
        return self.public
