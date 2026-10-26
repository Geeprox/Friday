"""Local hand geometry and short temporal confirmation. No device gesture claims."""

import math


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
    if (
        thumb
        and tip
        and dist(thumb, tip) < palm * 0.45
        and sum(extended.get(n, False) for n in ("middle", "ring", "little")) >= 2
    ):
        return "ok"
    if extended.get("index") and extended.get("middle") and any(folded.get(n) for n in ("ring", "little")):
        return "yeah"
    if (
        extended.get("index")
        and any(folded.get(n) for n in ("middle", "ring", "little"))
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
