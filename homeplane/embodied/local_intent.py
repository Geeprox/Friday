"""Small, fully anchored household grammar after attention/wake admission.

Only one curtain and two motor actions. Questions, quotes, conditionals,
schedules, contradictions and multi-step requests stay outside this path.
"""

import re

from homeplane.embodied.intent import address

OBJECT = r"(?:客厅(?:的)?(?:梦幻帘|窗帘)|梦幻帘|窗帘)"
VERB = r"(?:打开|拉开|开启|推开|关闭|关上|合上|拉上|关掉|开|关)"
PREFIX = r"(?:(?:请你|请|麻烦你|麻烦|能不能|可以|你帮我|帮我|给我)\s*)*"
SUFFIX = r"(?:(?:一下|吧|谢谢)\s*)*"


def household_plan(text, attending=False):
    a = address(text, attending)
    if not a["admitted"]:
        return None
    body = a["body"].strip().rstrip("。.!！").strip()
    if not body or len(body) > 80:
        return None
    # A prohibition is a request to avoid a new action, never its opposite.
    negative = re.fullmatch(
        PREFIX + r"(?:先|暂时)?(?:别|不要|不用|不必)\s*(?:帮我)?\s*" + VERB + r"\s*" + OBJECT + SUFFIX, body
    )
    if negative:
        return {
            "is_command": True,
            "route": "no_op",
            "device": None,
            "action": None,
            "reply": "好的，保持窗帘当前状态。",
        }
    patterns = [
        PREFIX + r"(?P<verb>" + VERB + r")\s*(?P<object>" + OBJECT + r")" + SUFFIX,
        PREFIX + r"(?:把)?(?P<object>" + OBJECT + r")(?:给我)?\s*(?P<verb>" + VERB + r")" + SUFFIX,
    ]
    m = next((m for pattern in patterns if (m := re.fullmatch(pattern, body))), None)
    if m is None:
        return None
    action = "open" if m["verb"] in ("打开", "拉开", "开启", "推开", "开") else "close"
    return {
        "is_command": True,
        "route": "curtain",
        "device": "selected_cover",
        "action": action,
        "reply": "正在打开窗帘。" if action == "open" else "正在关闭窗帘。",
    }
