"""Local hand geometry and short temporal confirmation. No device gesture claims."""

import math


BINDABLE_HAND_LABELS = frozenset({
    "ok", "yeah", "open_palm", "fist", "thumb_up", "thumb_down", "shaka", "horns",
})


def classify_hand(joints, ratio=16 / 9):
    def q(name):
        p = joints.get(name)
        return p if p and p.get("confidence", 0) >= 0.4 else None

    def dist(a, b):
        return math.hypot((a["x"] - b["x"]) * ratio, a["y"] - b["y"])

    wrist = q("wrist")
    middle = q("middleMCP")
    index = q("indexMCP")
    little = q("littleMCP")
    if not wrist:
        return "hand_visible"
    palm = max(dist(wrist, middle) if middle else 0, dist(index, little) if index and little else 0)
    if palm < 0.008:
        return "hand_visible"
    extended = {}
    folded = {}
    for name in ("index", "middle", "ring", "little"):
        a = q(name + "MCP")
        b = q(name + "PIP")
        c = q(name + "Tip")
        if not a or not b or not c:
            continue
        extended[name] = dist(wrist, c) > dist(wrist, b) * 1.13 and dist(a, c) > dist(a, b) * 1.45
        folded[name] = dist(wrist, c) < dist(wrist, b) * 1.16
    thumb = q("thumbTip")
    tip = q("indexTip")
    # Additional common poses require all four fingers and the thumb joints.
    # Occluded/folded fingers are not interchangeable: missing landmarks must
    # never become a fist, a thumb signal or a device-control gesture.
    thumb_base, thumb_mp, thumb_ip = q("thumbCMC"), q("thumbMP"), q("thumbIP")
    required = ["wrist", "thumbCMC", "thumbMP", "thumbIP", "thumbTip"] + [
        n + part for n in ("index", "middle", "ring", "little") for part in ("MCP", "PIP", "Tip")]
    clear = all((p := joints.get(n)) and p.get("confidence", 0) >= .55
                and all(type(p.get(axis)) in (int, float) and math.isfinite(p[axis]) and 0 <= p[axis] <= 1
                        for axis in ("x", "y")) for n in required)
    all_fingers = clear and len(extended) == 4 and len(folded) == 4
    curled = {n: folded.get(n, False) and not extended.get(n, False)
              for n in ("index", "middle", "ring", "little")}
    thumb_straight = False
    thumb_closed = False
    if thumb and thumb_base and thumb_mp and thumb_ip and index:
        def bend(a, b, c):
            u = ((a["x"] - b["x"]) * ratio, a["y"] - b["y"])
            v = ((c["x"] - b["x"]) * ratio, c["y"] - b["y"])
            length = math.hypot(*u) * math.hypot(*v)
            return math.degrees(math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / length)))) if length > 1e-8 else 0
        thumb_straight = (bend(thumb_mp, thumb_ip, thumb) > 150
                          and dist(thumb_base, thumb) > dist(thumb_base, thumb_mp) * 1.7
                          and dist(thumb, index) > palm * .60)
        thumb_closed = not thumb_straight and dist(thumb, index) < palm * .85
    if all_fingers and all(curled.values()) and thumb:
        if thumb_straight:
            dx, dy = (thumb["x"] - thumb_base["x"]) * ratio, thumb["y"] - thumb_base["y"]
            if abs(dy) > palm * .6 and abs(dy) > abs(dx) * 1.4:
                return "thumb_up" if dy < 0 else "thumb_down"
        elif thumb_closed:
            return "fist"
    if all_fingers and all(curled[n] for n in ("index", "middle", "ring")) and extended["little"] and thumb_straight:
        return "shaka"
    if all_fingers and extended["index"] and extended["little"] and curled["middle"] and curled["ring"] and thumb_closed:
        return "horns"
    if (
        thumb
        and tip
        and dist(thumb, tip) < palm * 0.45
        and sum(extended.get(n, False) for n in ("middle", "ring", "little")) >= 2
    ):
        return "ok"
    if extended.get("index") and extended.get("middle") and all(folded.get(n) for n in ("ring", "little")):
        return "yeah"
    if (
        extended.get("index")
        and all(folded.get(n) for n in ("middle", "ring", "little"))
        and not extended.get("middle", False)
    ):
        return "pointing"
    if sum(extended.values()) >= 3 and sum(folded.values()) <= 1:
        return "open_palm"
    return "hand_visible"


class GestureStabilizer:
    def __init__(self):
        self.items = {}

    def update(self, key, label, wrist, now, sample_id=None):
        self.items = {k: v for k, v in self.items.items() if now - v["last"] < 0.4}
        old = self.items.get(key)
        jump = old and math.hypot(old["wrist"][0] - wrist["x"], old["wrist"][1] - wrist["y"]) > 0.10
        same = old and not jump and old["label"] == label
        repeated = sample_id is not None and old and old.get("sample_id") == sample_id
        count = old["count"] + (0 if repeated else 1) if same else 1
        self.items[key] = {
            "last": now,
            "wrist": [wrist["x"], wrist["y"]],
            "label": label,
            "count": count,
            "sample_id": sample_id,
        }
        return label if count >= 3 else "hand_visible"
