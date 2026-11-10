"""Small local exercise circuit; geometric observations, not form coaching."""

PLAN = (
    ("squat", "深蹲", 3, "屈膝下蹲后站直，完整往返计一次"),
    ("jack", "开合动作", 4, "双脚打开、双手上举，再收脚放手；可用开合步"),
    ("high_knee", "交替抬膝", 6, "左右交替抬膝，每次放回后计一次"),
    ("lunge", "侧弓步", 4, "左右交替侧弓步，再回到中间"),
    ("side_step", "左右侧迈", 4, "向一侧迈步并回到中间，左右交替"),
    ("side_bend", "站姿侧倾", 4, "双脚站稳，上身向一侧轻倾再回正，左右交替"),
    ("reach_up", "交替上伸", 4, "一只手向上伸展再放下，左右交替，每次放回计一次"),
    ("arm_open", "胸前开合", 4, "双手在胸前靠拢，平举打开，再合回胸前"),
)

EXERCISE_GESTURES = {
    'exercise.squat': 'squat', 'exercise.jack': 'jack',
    'exercise.high_knee': 'high_knee', 'exercise.side_lunge': 'lunge',
    'exercise.side_step': 'side_step', 'exercise.side_bend': 'side_bend',
    'exercise.reach_up': 'reach_up', 'exercise.arm_open': 'arm_open',
}


def signals(j, quality, depth, standing_span, knee_origin):
    def q(k):
        p = j.get(k)
        return p if p and p.get("confidence", 0) >= 0.4 else None

    from homeplane.embodied.pose_rules import angle

    names = [s + n for s in ("left", "right") for n in ("Hip", "Knee", "Ankle", "Shoulder")]
    if not all(q(n) for n in names):
        return {"visible": False, "jack": None, "high_knee": None, "lunge": None,
                "side_step": None, "side_bend": None, "reach_up": None, "arm_open": None, "squat": depth}
    shoulder = abs(q("leftShoulder")["x"] - q("rightShoulder")["x"]) * 16 / 9
    spread = abs(q("leftAnkle")["x"] - q("rightAnkle")["x"]) * 16 / 9 / max(0.025, shoulder)
    hip = sum(q(s + "Hip")["y"] for s in ("left", "right")) / 2
    torso = max(0.03, hip - sum(q(s + "Shoulder")["y"] for s in ("left", "right")) / 2)
    wrists = [q(s + "Wrist") for s in ("left", "right")]
    down = all(
        w and w["y"] > q(s + "Shoulder")["y"] + 0.25 * torso for s, w in zip(("left", "right"), wrists)
    )
    jack = (
        "open"
        if spread >= 1.55 and quality["overhead"] >= 0.55
        else "closed"
        if spread <= 1.3 and down
        else None
    )
    lifts = {
        s: (q(s + "Hip")["y"] + (standing_span or 0.25) * 0.5 - q(s + "Knee")["y"])
        / max(0.04, standing_span or 0.25)
        for s in ("left", "right")
    }
    high = None
    if standing_span:
        if lifts["left"] > 0.23 and lifts["right"] < 0.12:
            high = "left"
        elif lifts["right"] > 0.23 and lifts["left"] < 0.12:
            high = "right"
        elif max(lifts.values()) < 0.12:
            high = "neutral"
    bends = {}
    for s in ("left", "right"):
        a = angle(*[{**q(s + n), "x": q(s + n)["x"] * 16 / 9} for n in ("Hip", "Knee", "Ankle")])
        bends[s] = (knee_origin or 175) - (a or 175)
    lunge = None
    if spread >= 1.5:
        if bends["left"] >= 24 and bends["right"] < 15:
            lunge = "left"
        elif bends["right"] >= 24 and bends["left"] < 15:
            lunge = "right"
    elif spread <= 1.3 and max(bends.values()) < 15:
        lunge = "neutral"
    lean = (sum(q(s + "Shoulder")["x"] for s in ("left", "right"))
            - sum(q(s + "Hip")["x"] for s in ("left", "right"))) / 2 * 16 / 9 / torso
    side_bend = None
    # A lateral bend is distinct from a lunge: legs stay nearly straight and
    # feet remain within a standing-width range. No clinical/form claim.
    if .55 <= spread <= 1.5 and max(bends.values()) < 18:
        side_bend = "left" if lean < -.35 else "right" if lean > .35 else "neutral" if abs(lean) < .12 else None
    reach_up = arm_open = None
    upright = .55 <= spread <= 1.5 and max(bends.values()) < 18 and high == 'neutral' and abs(lean) < .3
    if upright and all(wrists) and all(q(s+'Elbow') for s in ('left', 'right')):
        raised = {s: q(s+'Shoulder')['y']-q(s+'Wrist')['y'] >= .45*torso
                  and q(s+'Elbow')['y'] < q(s+'Shoulder')['y']+.1*torso for s in ('left', 'right')}
        lowered = {s: q(s+'Wrist')['y'] > q(s+'Shoulder')['y']+.25*torso for s in ('left', 'right')}
        reach_up = ('left' if raised['left'] and lowered['right'] else
                    'right' if raised['right'] and lowered['left'] else
                    'neutral' if all(lowered.values()) else None)
        shoulder_y = sum(q(s+'Shoulder')['y'] for s in ('left', 'right'))/2
        in_band = all(abs(w['y']-shoulder_y) <= .8*torso for w in wrists)
        if in_band:
            center_x = sum(q(s+'Shoulder')['x'] for s in ('left', 'right'))/2
            closed = (abs(wrists[0]['x']-wrists[1]['x'])*16/9 < .55*shoulder
                      and abs(sum(w['x'] for w in wrists)/2-center_x)*16/9 < .35*shoulder)
            arm_open = 'open' if quality['horizontal'] >= .6 else 'closed' if closed else None
    return {
        "visible": True,
        "jack": jack,
        "high_knee": high,
        "lunge": lunge,
        "side_bend": side_bend,
        "reach_up": reach_up,
        "arm_open": arm_open,
        # BodyMotion owns the calibrated, background-aligned lateral origin.
        "side_step": None,
        "squat": depth,
        "leg_spread": round(spread, 2),
        "leg_bend": round(max(bends.values()), 2),
        "knee_lift": {k: round(v, 2) for k, v in lifts.items()},
    }


class HIITCircuit:
    def __init__(self, *, plan=PLAN, require_neutral=False):
        self.plan = tuple(plan)
        self.require_neutral = require_neutral
        self.ready = not require_neutral
        self.level = 0
        self.reps = 0
        self.phase = "work"
        self.rest = 0.0
        self.total_reps = 0
        self.pending = None
        self.last_side = None
        self.stable = None
        self.stable_for = 0.0
        self.gap = 0.0
        self.feedback = "站直半秒，记录个人站姿"
        self.history = []

    @property
    def complete(self):
        return self.level >= len(self.plan)

    def invalidate_motion(self, *, keep_completed_side=False):
        """Keep completed work, drop an unfinished cycle across a pause/gap."""
        self.pending = self.stable = None
        if not keep_completed_side:
            self.last_side = None
        self.stable_for = self.gap = 0.0
        self.ready = not self.require_neutral

    def update(self, p, dt):
        if self.complete:
            return
        if self.phase == "rest":
            self.rest = max(0.0, self.rest - dt)
            self.feedback = "休息片刻，下一关准备"
            if self.rest == 0:
                self.phase = "work"
                self.feedback = self.plan[self.level][3]
            return
        key, name, target, instruction = self.plan[self.level]
        f = p.get("exercise_signals", {})
        if not p.get("squat_calibrated"):
            if self.require_neutral:
                self.invalidate_motion(keep_completed_side=True)
            self.feedback = "先自然站直半秒，双膝与脚踝入镜"
            return
        value = f.get(key)
        if key == "squat":
            value = (
                "active"
                if value is not None and value >= 0.65
                else "neutral"
                if value is not None and value <= 0.24
                else None
            )
        elif key in ("jack", "arm_open"):
            value = "active" if value == "open" else "neutral" if value == "closed" else None
        if not f.get("visible", False):
            value = None
        if value is None:
            self.gap += dt
            self.stable_for = 0.0
            self.stable = None
            if self.gap > 0.35:
                self.pending = None
                self.ready = not self.require_neutral
            self.feedback = "让膝盖和脚踝清晰入镜" if not f.get("visible") else instruction
            return
        self.gap = 0.0
        if value != self.stable:
            self.stable = value
            self.stable_for = 0.0
        self.stable_for += dt
        if self.stable_for < 0.09:
            return
        if not self.ready:
            self.ready = value == "neutral"
            self.feedback = instruction if self.ready else "先回到起点，再开始动作"
            return
        alternating = key in ("high_knee", "lunge", "side_step", "side_bend", "reach_up")
        if value != "neutral":
            if alternating and value == self.last_side:
                self.feedback = "换另一侧，再回到中间"
                return
            self.pending = value
            self.feedback = "动作已到位，回到起点计一次"
            return
        if self.pending is None:
            self.feedback = instruction
            return
        self.last_side = self.pending
        self.pending = None
        self.reps += 1
        self.total_reps += 1
        self.feedback = "已完成 " + str(self.reps) + " / " + str(target)
        if self.reps >= target:
            self.history.append({"action": key, "reps": self.reps})
            self.level += 1
            self.reps = 0
            self.last_side = None
            self.stable = None
            self.stable_for = 0.0
            self.ready = not self.require_neutral
            if self.complete:
                self.phase = "complete"
                self.feedback = "本轮训练已完成"
            else:
                self.phase = "rest"
                self.rest = 5.0

    def public(self):
        item = self.plan[min(self.level, len(self.plan) - 1)]
        return {
            "level": self.level,
            "total_levels": len(self.plan),
            "action": item[0],
            "name": item[1],
            "instruction": item[3],
            "required_reps": item[2],
            "reps": self.reps,
            "total_reps": self.total_reps,
            "phase": self.phase,
            "rest_seconds": round(self.rest, 1),
            "feedback": self.feedback,
            "history": list(self.history),
            "live_verified": False,
        }
