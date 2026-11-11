"""Small App and recognizer contracts, independent of optional ML dependencies."""

from dataclasses import asdict, dataclass
from typing import Literal, get_args


TennisVariant = Literal["match", "practice"]
TENNIS_VARIANTS = get_args(TennisVariant)


@dataclass(frozen=True)
class GameApp:
    id: str
    name: str
    description: str
    players: tuple[int, int]
    mode: str | None
    kind: str = "game"
    status: str = "available"


APPS = (
    GameApp("ski", "林间滑雪", "摆臂滑行，左右移动穿过木门", (1, 1), "ski"),
    GameApp("tennis", "体感网球", "独自练回球，或和家人对打", (1, 2), "tennis"),
    GameApp("quest", "山径冒险", "十站山径，平举、举手、浅蹲、侧迈、抬膝、开合步、侧倾、侧弓步、上伸与胸前开合", (1, 1), "quest"),
    GameApp("hiit", "运动闯关", "深蹲、开合步、抬膝、侧弓步、侧迈、侧倾、上伸与胸前开合", (1, 1), "hiit"),
    GameApp("werewolf", "狼人杀", "手机私下行动，Friday 主持，和家人一起推理", (6, 12), None, "party"),
    GameApp("home", "全屋互动", "动作绑定、指向设备与注视语音", (1, 1), "home", "interaction"),
)


def catalog():
    return [asdict(app) for app in APPS]


def get_app(app_id):
    result = next((app for app in APPS if app.id == app_id), None)
    if result is None:
        raise ValueError("game_not_found")
    return result


# Add a recognizer here, then emit the same camera/session/track scoped event.
# Extensions have no access to raw HA services, shell commands or device URLs.
RECOGNIZERS = {
    "hand.ok": {"name": "OK", "kind": "hold", "actions": ["game.start", "player.bind"]},
    "hand.yeah": {"name": "Yeah", "kind": "hold", "actions": ["game.next"]},
    "hand.point": {"name": "指认", "kind": "ray", "actions": ["seat.point"]},
    "hand.fist": {"name": "握拳", "kind": "hold", "actions": ["home.binding"]},
    "hand.thumb_up": {"name": "点赞", "kind": "hold", "actions": ["home.binding"]},
    "hand.thumb_down": {"name": "拇指向下", "kind": "hold", "actions": ["home.binding"]},
    "hand.shaka": {"name": "电话手势", "kind": "hold", "actions": ["home.binding"]},
    "hand.horns": {"name": "摇滚手势", "kind": "hold", "actions": ["home.binding"]},
    "body.arms_open": {"name": "双臂打开", "kind": "transition", "actions": ["home.cover.open"]},
    "body.arms_close": {"name": "双臂合上", "kind": "transition", "actions": ["home.cover.close"]},
    "body.lateral": {"name": "横移", "kind": "continuous", "actions": ["game.steer"]},
    "body.arm_effort": {"name": "摆臂", "kind": "continuous", "actions": ["game.accelerate"]},
    "body.swing": {"name": "挥拍", "kind": "impulse", "actions": ["game.hit"]},
    "body.squat": {"name": "深蹲", "kind": "cycle", "actions": ["exercise.rep"]},
    "body.jack": {"name": "开合跳", "kind": "cycle", "actions": ["exercise.rep"]},
    "body.high_knee": {"name": "抬膝", "kind": "cycle", "actions": ["exercise.rep"]},
    "body.side_lunge": {"name": "侧弓步", "kind": "cycle", "actions": ["exercise.rep"]},
}
