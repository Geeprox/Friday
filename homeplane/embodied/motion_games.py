"""Local, pose-driven game rules. No keyboard input, identity model or calories."""

import collections
import math
import random
import secrets
import time

from homeplane.embodied.game_difficulty import difficulty_options, game_difficulty
from homeplane.embodied.hiit import PLAN as HIIT_PLAN, HIITCircuit, signals
from homeplane.embodied.pose_rules import angle
from homeplane.embodied.quest import STAGES as QUEST_STAGES, TRANSITION_SECONDS
from homeplane.embodied.ski_route import (
    SKI_COURSES, SKI_GATES, SKI_LENGTH, SKI_ROUTE, SKI_TREE_REACH,
    SKI_VISIBLE_AHEAD, SKI_WORLD_TRAVEL, advance, crossing_time,
)
TENNIS_TARGET = 7
TENNIS_CAP = 15
TENNIS_REACH = .20
TENNIS_WINDOW = .10
TENNIS_CURVE = .4
TENNIS_CUE_LEAD = .20


def clamp(x, a=-1.0, b=1.0):
    return min(b, max(a, x))


def tennis_contact(ball, px, swing, dt, now, window):
    """Find a contact inside this step, using only an already observed swing.

    The ball can cross the whole hitting strip between two pose frames. A
    swing later than the baseline crossing must still never rescue the point.
    The x flight is monotone, so its overlap with reach also has one interval.
    """
    speed = ball['speed']
    if speed <= 0:
        return None
    distance = ball['z'] if ball['toward'] == 0 else 1-ball['z']
    first = max(0., (distance-window)/speed, dt-(now-swing))
    last = min(dt, distance/speed)
    if first > last:
        return None
    target, offset = ball['target_x'], ball['x']-ball['target_x']
    x = target+offset*math.exp(-TENNIS_CURVE*first)
    if not abs(x-px) < TENNIS_REACH:
        if not offset:
            return None
        boundary = px+TENNIS_REACH if x > px else px-TENNIS_REACH
        ratio = (boundary-target)/offset
        if ratio <= 0:
            return None
        first = max(first, -math.log(ratio)/TENNIS_CURVE)
        if first > last:
            return None
        # Use the inward side of the mathematical boundary, avoiding a
        # rounding-dependent miss at an exact geometric crossing.
        x = target+offset*math.exp(-TENNIS_CURVE*min(last, first+1e-9))
        if not abs(x-px) < TENNIS_REACH:
            return None
    z = ball['z']+(-1 if ball['toward'] == 0 else 1)*speed*first
    return first, x, z


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

    def invalidate_motion(self, *, keep_stance=False):
        self.samples.clear()
        self.phase = None
        self.pump_extreme = None
        self.pump_direction = 0
        self.arm_effort = 0.0
        self.arm_speed = 0.0
        self.last_swing = -10.0
        self.last_pump = -10.0
        if not keep_stance:
            self.leg_samples.clear()
            self.leg_origin = None
            self.knee_origin = None
        else:
            self.last_result = self.last_body_at = None
            self.pumps = self.swings = 0
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
            x = (anchor.get("reference") or anchor["floor"])[0]
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
            "floor_x": anchor["floor"][0] if located and anchor.get("floor") else None,
            "reference_x": (anchor.get("reference") or anchor["floor"])[0] if located else None,
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
        exercise = result['exercise_signals']
        if (fresh_ankles and self.origin is not None and exercise['visible']
                and .5 <= exercise['leg_spread'] <= 2.2 and exercise['leg_bend'] < 18):
            exercise['side_step'] = ('left' if self.lateral < -.35 else
                                     'right' if self.lateral > .35 else
                                     'neutral' if abs(self.lateral) < .12 else None)
        self.last_result = result
        return result


class MotionGames:
    def __init__(self, *, clock=time.monotonic, require_presentation=False):
        self.clock = clock
        from .game_presentation import GamePresentation
        self.presentation = GamePresentation(clock) if require_presentation else None
        self.difficulty = game_difficulty('standard')
        self.control_context = secrets.token_hex(16)
        self.control_revision = 0
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

    def reset(self, mode, *, difficulty=None):
        if mode not in ("off", "quest", "ski", "tennis", "hiit", "home"):
            raise ValueError("invalid_game")
        chosen = game_difficulty(difficulty) if difficulty is not None else self.difficulty
        stages, plan = chosen.quest_plan(QUEST_STAGES), chosen.training_plan(HIIT_PLAN)
        self.difficulty, self.quest_stages = chosen, stages
        if mode != "off":
            self.selected_mode = mode
        self.mode = mode
        self.control_revision += 1
        self.round_id = getattr(self, 'round_id', 0) + 1
        self.game_input_enabled = mode in {'quest','ski','tennis','hiit'}
        self.readers = {}
        self.slots = []
        self.preferred_track = None
        self.previous = None
        self.missing_since = None
        self.elapsed = 0.0
        self.active = 0.0
        self.status = "waiting"
        self.manually_paused = False
        self.source_resume_required = False
        self.recovery_phase = 'none'
        self.recovery_ready = False
        self.recovery_ready_count = 0
        self.recovery_tracks = ()
        self.recovery_frame_at = None
        self.resume_seconds = 0.0
        self.presentation_blocked = False
        self.presentation_recovery = False
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
        self.quest_counter = None
        self.quest_counter_level = None
        self.quest_transition = 0.0
        self.quest_history = []
        self.down_since = None
        self.up_since = None
        self.speed = 0.0
        self.distance = 0.0
        self.obstacles = []
        self.ski_gates = []
        self.ski_cursor = 0
        self.ski_results = {}
        self.ski_elapsed = 0.0
        self.ski_event = None
        self.ski_event_sequence = 0
        self.gates_passed = self.gates_missed = 0
        self.ski_combo = self.ski_best_combo = self.ski_gate_points = 0
        self.collisions = 0
        self.passed = 0
        self.score = [0, 0]
        self.rally = 0
        self.best_rally = 0
        self.tennis_winner = None
        self.tennis_event = None
        self.tennis_event_sequence = 0
        self.tennis_flight = 0
        self.tennis_used_swings = [-100.0, -100.0]
        self.ball = {"x": 0.5, "z": 0.5, "target_x": 0.5, "toward": 0, "speed": chosen.tennis_speed}
        self.hit_flash = -10.0
        self.round_pause = 2.0 if mode == 'tennis' else 0.0
        self.circuit = HIITCircuit(plan=plan, require_neutral=True)
        self.publish([], "off" if mode == "off" else "waiting", "OK 保持 1 秒开始 · Yeah 保持 1 秒切换")

    @property
    def complete(self):
        return ((self.mode == 'quest' and self.level >= len(self.quest_stages))
                or (self.mode == 'hiit' and self.circuit.complete)
                or (self.mode == 'ski' and self.distance >= SKI_LENGTH)
                or (self.mode == 'tennis' and self.tennis_winner is not None))

    def ski_course(self):
        distance = self.distance
        for index, course in enumerate(SKI_COURSES):
            if distance < course['length']:
                return index, course, distance
            distance -= course['length']
        return len(SKI_COURSES)-1, SKI_COURSES[-1], SKI_COURSES[-1]['length']

    def select(self, mode, *, difficulty=None):
        if mode not in ("quest", "ski", "tennis", "hiit", "home"):
            raise ValueError("invalid_game")
        if mode == 'home' and difficulty is not None:
            raise ValueError('invalid_game_difficulty')
        self.reset("off", difficulty=difficulty)
        self.selected_mode = mode
        self.game_input_enabled = mode != 'home'
        return self.publish([], "off", "OK 保持 1 秒开始 · Yeah 保持 1 秒切换")

    def _drop_source_inputs(self):
        self.readers = {}
        self.slots = []
        self.preferred_track = None
        self.previous = None
        self.missing_since = None
        self.squat_down = False
        self.down_since = self.up_since = self.pose_since = self.last_pose = None
        self.pose_gap = 0.0
        self.hold = 0.0
        self.speed = 0.0
        self.tennis_used_swings = [-100.0, -100.0]
        self.tennis_event = None
        self.hit_flash = -10.0
        self.gesture_key = self.gesture_since = self.gesture_last = self.gesture_release = None
        self.gesture_progress = 0.0
        self.gesture_event = None
        self.gesture_latched = True
        self.circuit.invalidate_motion(keep_completed_side=True)
        self._invalidate_quest_motion()
        self.recovery_ready = False
        self.recovery_ready_count = 0
        self.recovery_tracks = ()
        self.recovery_frame_at = None
        self.resume_seconds = 0.0

    def suspend_source(self):
        """Keep earned work, revoke all player/motion context until explicit resume."""
        if self.mode == 'home':
            self.reset('off')
            return self.public
        self.control_revision += 1
        self._drop_source_inputs()
        self.source_resume_required = self.mode != 'off' and not self.complete
        if self.source_resume_required:
            self.manually_paused = True
            self.recovery_phase = 'camera'
        return self.publish([], 'complete' if self.complete else 'paused' if self.mode != 'off' else 'off',
                            '本局已完成' if self.complete else '等待画面恢复')

    def begin_source(self):
        """A new camera session cannot erase a round or inherit its old stance."""
        if self.mode not in {'off','home'} and not self.complete and (self.readers or self.slots or self.elapsed):
            self.suspend_source()
        else:
            self._drop_source_inputs()
        if self.source_resume_required:
            self.control_revision += 1
            self.recovery_phase = 'camera'
        return self.publish([], 'complete' if self.complete else 'paused' if self.source_resume_required
                            else 'off' if self.mode == 'off' else 'waiting',
                            '本局已完成' if self.complete else '重新确认玩家' if self.source_resume_required else '等待玩家')

    def recovery_public(self):
        ready = (self.source_resume_required and self.recovery_ready
                 and self.recovery_frame_at is not None
                 and 0 <= self.clock()-self.recovery_frame_at <= 1.5)
        return dict(required=self.source_resume_required, ready=bool(ready and not self.presentation_blocked),
                    phase='display' if self.presentation_recovery and self.presentation_blocked else self.recovery_phase if ready or self.recovery_phase != 'ready' else 'players',
                    required_players=2 if self.mode == 'tennis' else 1,
                    ready_players=self.recovery_ready_count if ready else 0)

    def _recover_source(self, people, registration, now, captured_at):
        required = 2 if self.mode == 'tennis' else 1
        if (registration.get('status') != 'locked' or type(captured_at) not in (int,float)
                or not 0 <= now-captured_at <= 1.5):
            self._drop_source_inputs()
            self.recovery_phase = 'camera'
            return self.publish([], 'paused', '等待稳定画面')
        if len(people) != required or len({p['id'] for p in people}) != required:
            self._drop_source_inputs()
            self.recovery_phase = 'players'
            return self.publish([], 'paused', '请让玩家完整入镜')
        tracks = tuple(sorted((p['id'] for p in people),key=str))
        if tracks != self.recovery_tracks:
            self._drop_source_inputs()
            self.recovery_tracks = tracks
        observed = [result for p in people if (result := self.readers.setdefault(p['id'],BodyMotion()).update(p,now))]
        ready = [p for p in observed if p['calibrated'] and p['squat_calibrated']
                 and p['foot_observation'] == 'fresh']
        self.recovery_ready_count = len(ready)
        self.recovery_ready = len(ready) == required
        self.recovery_frame_at = captured_at
        self.recovery_phase = 'ready' if self.recovery_ready else 'calibrating'
        if self.recovery_ready:
            self.slots = [p['id'] for p in sorted(ready,key=lambda p:p['reference_x'],reverse=True)]
            self.preferred_track = self.slots[0]
        else:
            self.slots = []
            self.preferred_track = None
        return self.publish(observed, 'paused', '玩家已就绪' if self.recovery_ready else '请自然站定片刻')

    def set_paused(self, value):
        if type(value) is not bool or self.mode not in {'quest','ski','tennis','hiit'} or self.complete:
            raise ValueError('game_not_playing')
        if not value:
            self.require_game_view(self.mode)
        recovered = not value and self.source_resume_required
        if recovered and not self.recovery_public()['ready']:
            raise ValueError('game_input_not_ready')
        self.manually_paused = value
        if recovered:
            self.source_resume_required = False
            self.presentation_recovery = False
            self.recovery_phase = 'none'
            self.recovery_ready = False
            self.resume_seconds = 3.0
        self.control_revision += 1
        self.previous = None
        self.squat_down = False
        self.down_since = self.up_since = None
        self.circuit.invalidate_motion(keep_completed_side=recovered or self.source_resume_required)
        self._invalidate_quest_motion()
        for reader in self.readers.values():
            reader.invalidate_motion(keep_stance=recovered)
        if value and self.source_resume_required:
            self.recovery_ready = False
            self.recovery_frame_at = None
            self.recovery_phase = 'calibrating'
        return self.publish([], 'paused' if value else 'calibrating' if recovered else 'waiting',
                            '休息一下，准备好后继续' if value else '准备继续本局' if recovered else '请回到镜头前')

    def control_current(self, context, revision):
        return (type(context) is str and context == self.control_context
                and type(revision) is int and revision == self.control_revision)

    def presentation_scope(self):
        step = self.level if self.selected_mode == 'quest' else self.circuit.public()['level'] if self.selected_mode == 'hiit' else None
        return self.control_context, self.round_id, self.selected_mode, step

    def viewer_state(self):
        return dict(self.public, source_recovery=self.recovery_public(),
                    presentation=self.presentation.public(self.presentation_scope()) if self.presentation and self.selected_mode != 'home' else dict(required=False, ready=True))

    def require_game_view(self, mode):
        if self.presentation and mode in {'quest','ski','tennis','hiit'}:
            if mode != self.selected_mode or not self.presentation.ready(self.presentation_scope()):
                raise ValueError('game_presentation_not_ready')

    def observe_presentation(self, data):
        from .game_presentation import GameViewReport
        report = GameViewReport.model_validate(data)
        if not self.presentation or (report.expected_context, report.expected_round, report.mode) != self.presentation_scope()[:3]:
            raise ValueError('game_state_changed')
        self.presentation.synchronize(self.presentation_scope())
        if report.expected_view != self.presentation.context:
            raise ValueError('game_view_changed')
        return self.presentation.observe(self.presentation_scope(), report)

    def _hold_for_presentation(self):
        if not self.presentation or self.selected_mode == 'home' or not self.game_input_enabled or self.complete:
            return False
        if self.presentation.ready(self.presentation_scope()):
            if self.presentation_blocked:
                self.presentation_blocked = False
                self.previous = None
            return False
        between_stages = self.quest_transition > 0 or self.mode == 'hiit' and self.circuit.public()['phase'] == 'rest'
        loading_transition = between_stages and self.presentation.visible(self.presentation_scope())
        if self.elapsed > 0 and not self.source_resume_required and not loading_transition:
            self.suspend_source()
            self.presentation_recovery = True
        elif not self.presentation_blocked:
            slots, preferred = self.slots[:], self.preferred_track
            self._drop_source_inputs()
            if loading_transition:
                self.slots, self.preferred_track = slots, preferred
        if not self.presentation_blocked:
            self.presentation_blocked = True
        self.previous = None
        self.publish([], 'paused' if self.source_resume_required else 'off' if self.mode == 'off' else 'waiting', '等待游戏画面')
        return True

    def gesture_control(self, people, hands, registration, now):
        if not self.game_input_enabled or self.manually_paused:
            return
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
            modes = ("quest", "ski", "tennis", "hiit")
            current = modes.index(self.selected_mode) if self.selected_mode in modes else -1
            self.select(modes[(current + 1) % len(modes)])
            self.preferred_track = candidate[0]
            self.gesture_event = "switched"
        elif (
            self.mode in ("off", "home")
            or self.status == "waiting"
            or self.complete
        ):
            self.reset(self.selected_mode)
            self.preferred_track = candidate[0]
            self.gesture_event = "started"

    def update(self, people, registration, now, hands=(), *, captured_at=None):
        if self._hold_for_presentation():
            return self.public
        dt = clamp(now - self.previous, 0, 0.12) if self.previous is not None else 0.0
        self.previous = now
        if self.source_resume_required:
            return self._recover_source(people, registration, now, now if captured_at is None else captured_at)
        if not self.manually_paused and self.resume_seconds <= 0:
            self.gesture_control(people, hands, registration, now)
        if self._hold_for_presentation():
            return self.public
        if self.mode == "off":
            return self.publish([], 'off', 'OK 保持 1 秒开始 · Yeah 保持 1 秒切换' if self.game_input_enabled else '从游戏库开始一局')
        if self.mode == "home":
            return self.publish([], "playing", "智能家居已启用 · 双臂水平开合")
        if self.complete:
            return self.publish([], 'complete', '本局已完成')
        if self.manually_paused:
            return self.publish([], 'paused', '休息一下，准备好后继续')
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
                self.slots = [p["id"] for p in sorted(eligible, key=lambda p: p["reference_x"], reverse=True)]
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
        if self.resume_seconds > 0:
            self.resume_seconds = max(0.0, self.resume_seconds-dt)
            if self.resume_seconds < 1e-6:
                self.resume_seconds = 0.0
                self.previous = None
                for reader in self.readers.values():
                    reader.invalidate_motion(keep_stance=True)
            return self.publish(selected, 'calibrating' if self.resume_seconds > 0 else 'playing', '准备继续本局')
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
            if self.complete
            else "playing",
            "动作驱动中",
        )

    def quest(self, p, dt):
        if self.level >= len(self.quest_stages):
            return
        if self.quest_transition > 0:
            self.quest_transition = max(0.0, self.quest_transition - dt)
            if self.quest_transition < 1e-6:
                self.quest_transition = 0.0
            self.pose_feedback = "准备下一站"
            return
        stage = self.quest_stages[self.level]
        if stage.hold:
            quality = p.get("pose_quality", {}).get(stage.action, 0.0)
            if quality >= 0.60:
                self.hold += dt
                self.active += dt
                self.pose_gap = 0.0
                self.pose_feedback = "动作已到位，继续保持"
            else:
                self.pose_gap += dt
                self.pose_feedback = "双手抬高到肩部附近" if stage.action == "horizontal" else "双手举过肩部"
                if self.pose_gap > 0.30:
                    self.hold = max(0.0, self.hold - dt * 1.5)
            if self.hold >= stage.hold:
                self._advance_quest()
        else:
            if self.quest_counter_level != self.level:
                self.quest_counter_level = self.level
                self.quest_counter = HIITCircuit(
                    plan=((stage.action, stage.name, stage.reps, stage.instruction),), require_neutral=True)
            self.quest_counter.update(p, dt)
            self.reps = self.quest_counter.total_reps
            self.pose_feedback = self.quest_counter.feedback
            if self.quest_counter.pending is not None:
                self.active += dt
            if self.quest_counter.complete:
                self._advance_quest()

    def _invalidate_quest_motion(self):
        self.hold = self.pose_gap = 0.0
        if self.quest_counter is not None:
            self.quest_counter.invalidate_motion(keep_completed_side=True)

    def _advance_quest(self):
        stage = self.quest_stages[self.level]
        self.quest_history.append(dict(action=stage.action, reps=stage.reps, hold=stage.hold))
        self.level += 1
        self.hold = self.pose_gap = 0.0
        self.reps = 0
        self.quest_counter = self.quest_counter_level = None
        self.quest_transition = TRANSITION_SECONDS if self.level < len(self.quest_stages) else 0.0
        self.pose_feedback = "准备下一站" if self.quest_transition else "山径已通关"

    def quest_public(self, players):
        complete = self.level >= len(self.quest_stages)
        stage = self.quest_stages[self.level] if not complete else None
        fraction = (self.hold / stage.hold if stage and stage.hold else
                    self.reps / stage.reps if stage and stage.reps else 0.0)
        fraction = min(1.0, max(0.0, fraction))
        return dict(
            schema_version=1, level=self.level, total_levels=len(self.quest_stages),
            hold=round(self.hold, 2), reps=self.reps,
            targets=[s.action for s in self.quest_stages], stages=[s.public() for s in self.quest_stages],
            stage=stage.public() if stage else None,
            phase="complete" if complete else "transition" if self.quest_transition else "work",
            transition_seconds=round(self.quest_transition, 2), transition_duration=TRANSITION_SECONDS,
            required_hold=stage.hold if stage else 0, required_reps=stage.reps if stage else 0,
            stage_progress=round(fraction, 3),
            progress=round(min(1.0, (self.level + fraction) / len(self.quest_stages)), 4),
            total_reps=sum(s["reps"] for s in self.quest_history) + self.reps,
            history=[dict(s) for s in self.quest_history], feedback=self.pose_feedback,
            depth=players[0].get("squat_depth") if players else None)

    def ski(self, p, dt, now):
        if self.complete:
            return
        effort = clamp(p.get("arm_effort", 0), 0, 1)
        if effort > 0.03:
            self.active += dt
        remaining = dt
        while remaining > 1e-10 and self.distance < SKI_LENGTH:
            _, course, local = self.ski_course()
            # Only tests/recovery migrations may begin partway through a route.
            # Never award work for features already behind the observed position.
            while self.ski_cursor < len(SKI_ROUTE) and SKI_ROUTE[self.ski_cursor].at < self.distance - 1e-8:
                self.ski_cursor += 1
            feature = SKI_ROUTE[self.ski_cursor] if self.ski_cursor < len(SKI_ROUTE) else None
            boundary = min(self.distance + course['length'] - local, feature.at if feature else SKI_LENGTH)
            target = effort * course['speed'] * self.difficulty.ski_speed
            speed, travel = advance(self.speed, target, remaining)
            if travel < boundary - self.distance - 1e-10:
                self.speed, self.distance = speed, self.distance + travel
                remaining = 0
            else:
                used = crossing_time(self.speed, target, max(0, boundary - self.distance), remaining)
                self.speed = advance(self.speed, target, used)[0]
                self.distance = boundary
                remaining -= used
                if feature and abs(feature.at - boundary) < 1e-8:
                    self._cross_ski_feature(feature, p['lateral'], self.ski_elapsed + dt - remaining)
                    self.ski_cursor += 1
        self.ski_elapsed += dt - remaining
        self.obstacles, self.ski_gates = [], []
        for feature in SKI_ROUTE:
            z = (feature.at - self.distance) * SKI_WORLD_TRAVEL
            if -.3 < z <= SKI_VISIBLE_AHEAD:
                result = self.ski_results.get(feature.id)
                item = dict(id=feature.id, x=feature.x, z=z, hit=result is not None)
                if feature.kind == 'tree':
                    self.obstacles.append(item)
                else:
                    self.ski_gates.append(dict(item, width=feature.width * self.difficulty.ski_gate, result=result))
        if self.distance >= SKI_LENGTH:
            self.speed = 0.0
            self.obstacles = self.ski_gates = []

    def _cross_ski_feature(self, feature, lateral, at):
        if feature.kind == 'gate':
            passed = abs(feature.x - lateral) <= feature.width * self.difficulty.ski_gate + 1e-9
            if passed:
                self.gates_passed += 1
                self.ski_combo += 1
                self.ski_best_combo = max(self.ski_best_combo, self.ski_combo)
                points = 250 + min(4, self.ski_combo - 1) * 50
                self.ski_gate_points += points
            else:
                self.gates_missed += 1
                self.ski_combo, points = 0, 0
            kind = 'gate' if passed else 'miss'
        elif abs(feature.x - lateral) < SKI_TREE_REACH:
            self.collisions += 1
            self.speed *= .4
            self.ski_combo, kind, points = 0, 'collision', -25
        else:
            self.passed += 1
            self.ski_results[feature.id] = 'avoided'
            return
        self.ski_results[feature.id] = kind
        self.ski_event_sequence += 1
        self.ski_event = dict(sequence=self.ski_event_sequence, kind=kind, feature=feature.id,
                              elapsed=at, combo=self.ski_combo, points=points)

    def ski_public(self, players, status):
        course_index, course, course_distance = self.ski_course()
        gate = next((f for f in SKI_GATES if f.at >= self.distance and f.id not in self.ski_results), None)
        active = self.mode == 'ski' and status == 'playing' and bool(players) and gate is not None
        lateral = players[0].get('lateral', 0) if players else 0
        delta = gate.x - lateral if gate else 0
        gate_width = gate.width * self.difficulty.ski_gate if gate else 0
        cue = ('left' if delta < -gate_width + .08 else
               'right' if delta > gate_width - .08 else 'center') if gate else 'finish'
        event = dict(self.ski_event, age=round(max(0, self.ski_elapsed - self.ski_event['elapsed']), 3)) if self.ski_event else None
        stars = (3 if self.gates_passed >= len(SKI_GATES) - 2 and self.collisions <= 1 else
                 2 if self.gates_passed >= len(SKI_GATES) * 2 / 3 and self.collisions <= 4 else 1)
        return dict(
            schema_version=2, speed=round(self.speed, 3), distance=round(self.distance, 2),
            obstacles=[dict(o) for o in self.obstacles], gates=[dict(g) for g in self.ski_gates],
            collisions=self.collisions, passed=self.passed, course=course_index,
            course_name=course['name'], course_count=len(SKI_COURSES),
            course_progress=round(min(1, course_distance / course['length']), 3),
            total_distance=SKI_LENGTH, progress=round(min(1, self.distance / SKI_LENGTH), 3),
            gate_count=len(SKI_GATES), gates_passed=self.gates_passed, gates_missed=self.gates_missed,
            combo=self.ski_combo, best_combo=self.ski_best_combo,
            points=max(0, self.passed * 100 - self.collisions * 25 + self.ski_gate_points),
            stars=stars if self.mode == 'ski' and self.complete else None,
            guide=dict(active=active, cue=cue,
                       next_gate=dict(id=gate.id, x=gate.x, width=gate_width,
                                      distance=round(gate.at - self.distance, 3)) if gate else None),
            event=event)

    def tennis(self, players, dt, now):
        if self.tennis_winner is not None:
            return
        if self.round_pause > 0:
            self.round_pause = max(0, self.round_pause-dt)
            return
        b = self.ball
        start_x, start_z = b['x'], b['z']
        direction = -1 if b["toward"] == 0 else 1
        receiver = b["toward"]
        p = players[receiver]
        px = 0.5 + p["lateral"] * 0.4
        window = TENNIS_WINDOW * self.difficulty.tennis_window
        swing = p.get('last_swing')
        fresh_swing = (type(swing) in (int, float) and math.isfinite(swing)
                       and 0 <= now-swing < .35 and swing > self.tennis_used_swings[receiver]
                       and p.get('observation', 'fresh') == 'fresh')
        contact = tennis_contact(b, px, swing, dt, now, window) if fresh_swing else None
        if contact and now-dt+contact[0]-self.hit_flash > .4:
            at, hit_x, hit_z = contact
            quality = 'perfect' if abs(hit_x-px)<.08 and abs(hit_z-receiver)<.07 else 'good'
            self.tennis_used_swings[receiver] = swing
            self.tennis_event_sequence += 1
            self.tennis_event = dict(sequence=self.tennis_event_sequence, kind='hit', player=receiver,
                                     quality=quality, elapsed=self.elapsed)
            self.tennis_flight += 1
            b['x'] = hit_x
            b["toward"] = 1 - receiver
            b["z"] = 0.12 if receiver == 0 else 0.88
            b["target_x"] = clamp(px + p["swing_direction"] * 0.27, 0.12, 0.88)
            b["speed"] = min(self.difficulty.tennis_max_speed, b["speed"] + 0.015)
            remaining = max(0., dt-at)
            b['z'] += (1 if receiver == 0 else -1)*b['speed']*remaining
            b['x'] = b['target_x']+(b['x']-b['target_x'])*math.exp(-TENNIS_CURVE*remaining)
            self.rally += 1
            self.best_rally = max(self.best_rally, self.rally)
            self.hit_flash = now-dt+at
            self.active += 0.3
            return
        b["z"] += direction * b["speed"] * dt
        b['x'] = b['target_x'] + (b['x']-b['target_x'])*math.exp(-TENNIS_CURVE*dt)
        if b["z"] < 0 or b["z"] > 1:
            self.score[1 - receiver] += 1
            self.tennis_event_sequence += 1
            self.tennis_event = dict(sequence=self.tennis_event_sequence, kind='point', player=1-receiver,
                                     elapsed=self.elapsed)
            if p.get('observation', 'fresh') == 'fresh':
                distance = start_z if receiver == 0 else 1-start_z
                arrival = max(0., min(dt, distance/max(.01, b['speed'])))
                baseline_x = b['target_x']+(start_x-b['target_x'])*math.exp(-TENNIS_CURVE*arrival)
                # Coach the next attempt; this is not a claim about why a
                # physical person missed or how accurately a model saw them.
                self.tennis_event['next_focus'] = 'position' if abs(baseline_x-px) >= TENNIS_REACH else 'timing'
            self.tennis_flight += 1
            self.rally = 0
            leading = max(self.score)
            if leading >= TENNIS_TARGET and (abs(self.score[0]-self.score[1]) >= 2 or leading >= TENNIS_CAP):
                self.tennis_winner = self.score.index(leading)
                b['speed'] = 0
                return
            b.update(x=0.5, z=0.5, target_x=self.rng.uniform(0.15, 0.85), toward=receiver, speed=self.difficulty.tennis_speed)
            self.round_pause = 2.0

    def tennis_public(self, players, status):
        """Guidance uses the exact flight equation and reach used by tennis()."""
        phase = ('complete' if self.tennis_winner is not None else
                 'point' if self.round_pause > 0 and self.tennis_event and self.tennis_event['kind']=='point' else
                 'ready' if self.round_pause > 0 else 'rally')
        receiver = self.ball['toward']
        active = self.mode == 'tennis' and status == 'playing' and len(players) == 2 and self.tennis_winner is None
        guide = dict(active=active, receiver=receiver, target_x=None, seconds=None,
                     in_position=False, cue=None, reach=TENNIS_REACH)
        if active:
            z = self.ball['z']
            window = TENNIS_WINDOW * self.difficulty.tennis_window
            seconds = max(0, (z-window if receiver == 0 else 1-window-z)
                          / max(.01, self.ball['speed']))
            target = self.ball['target_x'] + (self.ball['x']-self.ball['target_x'])*math.exp(-TENNIS_CURVE*seconds)
            delta = target-(.5+players[receiver]['lateral']*.4)
            positioned = abs(delta) < TENNIS_REACH
            cue = ('prepare' if phase in {'ready','point'} else
                   ('right' if delta > 0 else 'left') if not positioned else
                   'swing' if seconds <= TENNIS_CUE_LEAD else 'wait')
            guide.update(target_x=target, seconds=seconds, in_position=positioned, cue=cue)
        event = dict(self.tennis_event, age=round(max(0, self.elapsed-self.tennis_event['elapsed']), 3)) if self.tennis_event else None
        return dict(ball=dict(self.ball), score=list(self.score), rally=self.rally, best_rally=self.best_rally,
                    winner=self.tennis_winner, target=TENNIS_TARGET, cap=TENNIS_CAP,
                    phase=phase, ready_seconds=round(self.round_pause, 3), guide=guide,
                    event=event, flight=self.tennis_flight)

    def publish(self, players, status, reason):
        if self.mode == 'quest' and status not in {'playing', 'complete'}:
            self._invalidate_quest_motion()
        self.status = status
        self.reason = reason
        self.public = {
            "mode": self.mode,
            "round_id": self.round_id,
            "control_context": self.control_context,
            "control_revision": self.control_revision,
            "selected_mode": self.selected_mode,
            "difficulty": self.difficulty.id,
            "difficulty_options": difficulty_options(),
            "owner_track": self.preferred_track,
            "input_mirrored": True,
            "version": "0.13.0",
            "gesture_control": {
                "gesture": self.gesture_key[1] if self.gesture_key else None,
                "progress": round(self.gesture_progress, 2),
                "latched": self.gesture_latched,
                "last_event": self.gesture_event,
            },
            "status": status,
            "manually_paused": self.manually_paused,
            "source_recovery": self.recovery_public(),
            "resume_seconds": round(self.resume_seconds, 2),
            "presentation": self.presentation.public(self.presentation_scope()) if self.presentation and self.selected_mode != 'home' else dict(required=False, ready=True),
            "game_input_enabled": self.game_input_enabled,
            "reason": reason,
            "players": players,
            "elapsed": round(self.elapsed, 2),
            "active_seconds": round(self.active, 2),
            "hiit": self.circuit.public(),
            "quest": self.quest_public(players),
            "ski": self.ski_public(players, status),
            "tennis": self.tennis_public(players, status),
        }
        return self.public
