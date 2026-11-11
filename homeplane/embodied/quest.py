"""The local adventure route, shared by rules, teaching and the public HUD."""

from dataclasses import asdict, dataclass


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

    def public(self):
        return asdict(self)


STAGES = (
    QuestStage("horizontal", "双臂平举", "双臂平举，打开林间入口",
               "双臂抬至肩高，保持 3 秒。", "pose.horizontal", "林间入口", hold=3.0, landmark="gate"),
    QuestStage("overhead", "双手上举", "双手举高，穿过树冠",
               "双手举过头顶，保持 3 秒。", "pose.overhead", "林间入口", hold=3.0, landmark="canopy"),
    QuestStage("squat", "浅蹲起立", "浅蹲再站起，穿过矮枝",
               "浅蹲后站直，重复 3 次。", "exercise.squat", "林间入口", reps=3, landmark="branches"),
    QuestStage("side_step", "左右侧迈", "左右侧迈，跨过溪石",
               "向一侧迈步再回到中间，左右交替，共 4 次。", "exercise.side_step", "溪畔步道", reps=4,
               scene="stream", landmark="stones"),
    QuestStage("high_knee", "交替抬膝", "交替抬膝，登上木阶",
               "抬起一侧膝盖再放下，左右交替，共 6 次。", "exercise.high_knee", "溪畔步道", reps=6,
               scene="stream", landmark="steps"),
    QuestStage("jack", "开合步", "打开再合拢，走出花径",
               "迈开双脚并举手，再收脚放手，重复 4 次。", "exercise.jack", "溪畔步道", reps=4,
               scene="stream", landmark="flowers"),
    QuestStage("balance", "单腿站立", "单腿站稳，踏过溪石",
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
)

TRANSITION_SECONDS = 3.0
