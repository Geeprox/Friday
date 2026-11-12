"""山径冒险的路线与站点，由游戏规则、教学和大屏共用。"""

from dataclasses import asdict, dataclass
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
    QuestStage("balance", "单腿站立", "单腿站稳，越过浅滩",
               "轻抬一脚，站稳 1 秒后落地，左右交替，共 4 次。", "exercise.balance", "溪畔步道", reps=4,
               scene="stream", landmark="crossing"),
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
