"""山径冒险的路线与站点，由游戏规则、教学和大屏共用。"""

from dataclasses import asdict, dataclass
from math import hypot
from typing import Literal


@dataclass(frozen=True)
class QuestStage:
    action: str
    name: str
    title: str
    instruction: str
    teaching: str
    chapter: str
    hold: float = 0.0
    reps: int = 0
    scene: str = "forest"
    landmark: str = ""
    step_pattern: tuple[str, ...] = ()

    def public(self):
        stage = asdict(self)
        if self.step_pattern:
            stage["step_pattern"] = list(self.step_pattern)
        else:
            stage.pop("step_pattern")
        return stage


STAGES = (
    QuestStage("horizontal", "双臂平举", "双臂平举，打开林间入口",
               "双臂抬至肩高，保持 3 秒。", "pose.horizontal", "林间入口", hold=3.0, landmark="gate"),
    QuestStage("overhead", "双手上举", "双手举高，穿过树冠",
               "双手举过头顶，保持 3 秒。", "pose.overhead", "林间入口", hold=3.0, landmark="canopy"),
    QuestStage("squat", "浅蹲起立", "浅蹲再站起，穿过矮枝",
               "浅蹲后站直，重复 3 次。", "exercise.squat", "林间入口", reps=3, landmark="branches"),
    QuestStage("side_tap", "侧点步", "踏过溪石",
               "向亮起的溪石点一步，再收脚，共 4 次。", "exercise.side_tap", "溪畔步道", reps=4,
               scene="stream", landmark="stones", step_pattern=("left", "right", "right", "left")),
    QuestStage("high_knee", "交替抬膝", "交替抬膝，登上木阶",
               "抬起一侧膝盖再放下，左右交替，共 6 次。", "exercise.high_knee", "溪畔步道", reps=6,
               scene="stream", landmark="steps"),
    QuestStage("jack", "开合步", "打开再合拢，走出花径",
               "迈开双脚并举手，再收脚放手，重复 4 次。", "exercise.jack", "溪畔步道", reps=4,
               scene="stream", landmark="flowers"),
    QuestStage("balance", "单腿站立", "搭桥过溪",
               "抬起提示的脚，站稳 1 秒后落回原位，共 4 次。", "exercise.balance", "溪畔步道", reps=4,
               scene="stream", landmark="bridge", step_pattern=("left", "right", "right", "left")),
    QuestStage("side_bend", "站姿侧倾", "轻轻侧倾，越过枝叶",
               "双脚站稳，向一侧轻倾再回正，左右交替，共 4 次。", "exercise.side_bend", "山顶营地", reps=4,
               scene="camp", landmark="boughs"),
    QuestStage("lunge", "侧弓步", "左右侧弓步，到达营地",
               "向一侧迈开，屈膝后回到中间，左右交替，共 4 次。", "exercise.side_lunge", "山顶营地", reps=4,
               scene="camp", landmark="trail"),
    QuestStage("reach_up", "交替上伸", "交替上伸，挂好营地彩旗",
               "一只手向上伸展再放下，左右交替，共 4 次。", "exercise.reach_up", "山顶营地", reps=4,
               scene="camp", landmark="flags"),
    QuestStage("arm_open", "胸前开合", "双臂开合，迎接山顶微风",
               "双手在胸前靠拢，平举打开，再合回胸前，重复 4 次。", "exercise.arm_open", "山顶营地", reps=4,
               scene="camp", landmark="breeze"),
    QuestStage("cross_knee", "交叉触膝", "交叉触膝，铺好营地踏板",
               "抬膝，用对侧手靠近膝盖，再放下换侧，共 4 次。", "exercise.cross_knee", "山顶营地", reps=4,
               scene="camp", landmark="landing"),
    QuestStage("side_raise", "交替侧抬腿", "侧抬腿，种下营地野花",
               "向侧面轻抬一只脚，停一下再落地，左右交替，共 4 次。", "exercise.side_raise", "山顶营地", reps=4,
               scene="camp", landmark="camp_flowers"),
    QuestStage("overhead_clap", "头顶合掌", "头顶合掌，点亮营地",
               "举起并打开双臂，头顶合掌后再打开，重复 4 次。", "exercise.overhead_clap", "山顶营地", reps=4,
               scene="camp", landmark="lanterns"),
)

TRANSITION_SECONDS = 3.0


class QuestBalanceBridge:
    """每块桥板由指定脚的站稳与原地落脚共同确认。"""

    HOLD_SECONDS = 1.0
    STANCE_SECONDS = .12
    FRAME_AGE = .60
    FRAME_GAP = .30
    LAND_SECONDS = 1.8

    def __init__(self, pattern):
        self.pattern = pattern
        self.count = 0
        self.last_at = None
        self.invalidate_motion()

    @property
    def complete(self):
        return self.count == len(self.pattern)

    def invalidate_motion(self, phase="center"):
        self.phase = phase
        self.origin = None
        self.neutral_since = self.hold_since = self.land_since = None
        self.progress = 0.0

    def update(self, player, now, captured_at):
        if self.complete:
            return
        if type(captured_at) not in (int, float) or not -.05 <= now - captured_at <= self.FRAME_AGE:
            self.invalidate_motion("frame")
            return
        if self.last_at is not None:
            if captured_at <= self.last_at:
                return
            if captured_at - self.last_at > self.FRAME_GAP:
                self.invalidate_motion()
        self.last_at = captured_at
        pose = player.get("exercise_signals", {}).get("bridge_balance")
        if pose is None or not player["squat_calibrated"]:
            self.invalidate_motion("frame")
            return
        j = player["joints"]
        feet = tuple(j[side + "Ankle"] for side in ("left", "right"))
        side = self.pattern[self.count]
        left_support = 0 if j["leftHip"]["x"] < j["rightHip"]["x"] else 1
        if self.origin is not None:
            if left_support != self.left_support:
                self.invalidate_motion()
                return
            if pose not in ("neutral", "transition", side):
                self.invalidate_motion("wrong")
                return
            drift = tuple(hypot((foot["x"] - start["x"]) * 16 / 9, foot["y"] - start["y"])
                          for foot, start in zip(feet, self.origin))
            support = self.left_support if side == "left" else 1 - self.left_support
            moved = max(drift) if pose == "neutral" else drift[support]
            if moved > self.stance_limit:
                self.invalidate_motion()
                if pose != "neutral":
                    return
        if (self.phase == "land" and self.land_since is not None
                and captured_at - self.land_since > self.LAND_SECONDS):
            self.invalidate_motion()
        if pose == "neutral":
            self._stand(j, feet, left_support, captured_at)
            return
        self.neutral_since = None
        if self.origin is None:
            return
        if pose == "transition":
            if self.phase != "land":
                self.phase, self.hold_since, self.progress = "lift", None, 0.0
            return
        if self.hold_since is None:
            self.hold_since = captured_at
        self.progress = min(1.0, (captured_at - self.hold_since + 1e-6) / self.HOLD_SECONDS)
        self.phase = "land" if self.progress >= 1.0 else "hold"
        self.land_since = captured_at if self.phase == "land" else None

    def _stand(self, joints, feet, left_support, captured_at):
        if self.neutral_since is None:
            self.neutral_since = captured_at
        if self.phase == "hold":
            self.invalidate_motion()
            self.neutral_since = captured_at
        if captured_at - self.neutral_since + 1e-6 < self.STANCE_SECONDS:
            return
        if self.phase == "land":
            self.count += 1
        self.phase = "lift"
        self.hold_since = self.land_since = None
        self.progress = 0.0
        self.origin = feet
        self.left_support = left_support
        self.stance_limit = abs(joints["leftShoulder"]["x"] - joints["rightShoulder"]["x"]) * 16 / 9 * .32


QuestRouteId = Literal["full", "forest", "stream", "camp"]


@dataclass(frozen=True)
class QuestRoute:
    id: QuestRouteId
    name: str
    stages: tuple[QuestStage, ...]


def _chapter(scene: QuestRouteId) -> QuestRoute:
    stages = tuple(stage for stage in STAGES if stage.scene == scene)
    return QuestRoute(scene, stages[0].chapter, stages)


DEFAULT_QUEST_ROUTE = QuestRoute("full", "完整路线", STAGES)
QUEST_ROUTES = (DEFAULT_QUEST_ROUTE, _chapter("forest"), _chapter("stream"), _chapter("camp"))


def get_quest_route(route_id: QuestRouteId) -> QuestRoute:
    for route in QUEST_ROUTES:
        if route.id == route_id:
            return route
    raise ValueError("invalid_quest_route")


def quest_route_options(*, teaching=False):
    options = []
    for route in QUEST_ROUTES:
        option = dict(id=route.id, name=route.name, stations=len(route.stages))
        if teaching:
            option["teaching"] = [stage.teaching for stage in route.stages]
        options.append(option)
    return options
