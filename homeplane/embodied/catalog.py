"""Small App and recognizer contracts, independent of optional ML dependencies."""

from dataclasses import asdict, dataclass


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
    GameApp("ski", "林间滑雪", "双臂摆动前进，横移躲避障碍", (1, 1), "ski"),
    GameApp("tennis", "双人网球", "左右接球，挥拍回击", (2, 2), "tennis"),
    GameApp("quest", "动作闯关", "完成指定姿势，穿过山径", (1, 1), "quest"),
    GameApp("hiit", "运动闯关", "深蹲、开合跳、抬膝与侧弓步", (1, 1), "hiit"),
    GameApp("werewolf", "座位指认", "实时座位、手部骨架与指认目标", (1, 10), None, "seating", "prototype"),
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
