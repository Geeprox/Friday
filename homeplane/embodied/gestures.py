"""Local hand geometry and short temporal confirmation. No device gesture claims."""

import math


BINDABLE_HAND_LABELS = frozenset({
    "ok", "yeah", "open_palm", "fist", "thumb_up", "thumb_down", "shaka", "horns",
})


HAND_MAX_GAP = .85
HAND_HOLD_SECONDS = .5


def classify_hand(joints, ratio=16 / 9, *, world_horns=False):
    def q(name):
        p = joints.get(name)
        return p if p and p.get("confidence", 0) >= 0.4 else None

    def dist(a, b):
        return math.hypot((a["x"] - b["x"]) * ratio, a["y"] - b["y"])

    def bend(a, b, c):
        u = ((a["x"] - b["x"]) * ratio, a["y"] - b["y"])
        v = ((c["x"] - b["x"]) * ratio, c["y"] - b["y"])
        length = math.hypot(*u) * math.hypot(*v)
        return math.degrees(math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / length)))) if length > 1e-8 else 0

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
    clear_curl = set()
    for name in ("index", "middle", "ring", "little"):
        a = q(name + "MCP")
        b = q(name + "PIP")
        c = q(name + "Tip")
        # 指尖明确折回到近端关节以内时，已足以排除伸直，指根可被遮住。
        if not a and b and c and dist(wrist, c) < .95 * dist(wrist, b):
            clear_curl.add(name + "MCP")
            extended[name] = False
            folded[name] = True
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
    def reliable(n):
        p = joints.get(n)
        return p and p.get("confidence", 0) >= .55 and all(
            type(p.get(axis)) in (int, float) and math.isfinite(p[axis]) and 0 <= p[axis] <= 1
            for axis in ("x", "y"))

    # 拇指根靠近手腕，侧掌时可能不可见。独立的远端判据仍要求指节和指尖清晰。
    distal_clear = all(reliable(n) for n in required if n != "thumbCMC" and n not in clear_curl)
    clear = distal_clear and reliable("thumbCMC")
    all_fingers = clear and len(extended) == 4 and len(folded) == 4
    curled = {n: folded.get(n, False) and not extended.get(n, False)
              for n in ("index", "middle", "ring", "little")}
    thumb_straight = False
    thumb_closed = False
    if thumb and thumb_base and thumb_mp and thumb_ip and index:
        thumb_straight = (bend(thumb_mp, thumb_ip, thumb) > 150
                          and dist(thumb_base, thumb) > dist(thumb_base, thumb_mp) * 1.7
                          and dist(thumb, index) > palm * .60)
        thumb_closed = not thumb_straight and dist(thumb, index) < palm * .85
    upward_thumb = False
    fist_thumb_tucked = False
    if all_fingers and all(curled.values()) and thumb_straight:
        dx = (thumb["x"] - thumb_base["x"]) * ratio
        dy = thumb["y"] - thumb_base["y"]
        if abs(dy) > palm * .6 and abs(dy) > abs(dx) * 1.2:
            if dy > 0:
                return "thumb_down"
            upward_thumb = True
    if all_fingers and all(curled[n] for n in ("index", "middle", "ring")) and extended["little"] and thumb_straight:
        return "shaka"
    if all_fingers and extended["index"] and extended["little"] and curled["middle"] and curled["ring"] and thumb_closed:
        return "horns"
    if distal_clear and index:
        thumb_angle = bend(thumb_mp, thumb_ip, thumb)
        thumb_reach = dist(thumb_mp, thumb)
        far_thumb = dist(thumb, index) > palm * .7
        # 方向取拇指掌指关节到指尖，小幅弯曲或腕部旋转时仍可判别向下。
        dx = (thumb["x"] - thumb_mp["x"]) * ratio
        dy = thumb["y"] - thumb_mp["y"]
        # 投影缩短的拇指可能靠近食指根，同时其指尖已明显高出拳面。
        if (all_fingers and all(curled.values()) and middle and thumb_angle > 135
                and -dy > max(palm * .36, abs(dx) * 1.2)
                and thumb["y"] < min(index["y"], middle["y"]) - palm * .15):
            upward_thumb = True
        # 相对水平线向下至少约 25 度。要求拇指突出拳面，侧伸或贴拳的拇指不计入。
        fist_center_y = sum(q(n + "Tip")["y"] for n in curled) / 4
        if (all(curled.values()) and far_thumb and thumb_angle > 130
                and thumb_reach > palm * .58 and dy > max(palm * .25, abs(dx) * .47)
                and thumb["y"] > fist_center_y + palm * .08):
            return "thumb_down"
        pinky_open = False
        little_tip = q("littleTip")
        if little:
            # 侧伸的小指用局部角度和长度确认，电话与摇滚使用同一组手指证据。
            little_pip = q("littlePIP")
            pinky_open = (bend(little, little_pip, little_tip) > 135
                          and dist(little, little_tip) > max(palm * .48, dist(little, little_pip) * 1.48))
        if curled["index"] and curled["middle"] and curled["ring"] and little:
            spread = dist(thumb, little_tip)
            opposed = spread > max(dist(thumb_mp, little_tip), dist(thumb, little)) + palm * .12
            if (extended["little"] and far_thumb and thumb_angle > 125 and thumb_reach > palm * .6
                    and spread > palm * 1.15):
                return "shaka"
            if (pinky_open and opposed and spread > palm * 1.15 and far_thumb
                    and thumb_angle > 110 and thumb_reach > palm * .5):
                return "shaka"
        # 拇指可横压中指或无名指，不能仅按它与食指根的距离判断收拢。
        tucked = any(
            q(n) and dist(thumb, q(n)) < palm * .5
            and dist(thumb_mp, q(n)) - dist(thumb, q(n)) > palm * .12
            for n in ("indexMCP", "middleMCP", "ringMCP")
        )
        # 握拳需要看到拇指向拳面靠拢或弯折收紧；位置靠近食指根本身不足以确认。
        fist_thumb_tucked = (tucked or (thumb_angle < 135 and dist(thumb, index) < palm * .6))
        if extended["index"] and curled["middle"] and curled["ring"]:
            compact_thumb = tucked and thumb_reach < palm * .85
            # 拇指压在弯曲的中指或无名指近端关节上时，侧视可能远离指根。
            over_folded_knuckle = any(
                dist(thumb, q(n + "PIP")) < .4 * palm
                and dist(thumb_mp, q(n + "PIP")) - dist(thumb, q(n + "PIP")) > .3 * palm
                for n in ("middle", "ring")
            )
            if extended["little"] and (compact_thumb or (over_folded_knuckle and thumb_reach < palm * .85)):
                return "horns"
            if (pinky_open and (thumb_closed or compact_thumb)
                    and dist(little_tip, q("ringTip")) > palm * .45
                    and dist(little_tip, tip) > palm * .65):
                return "horns"
    if upward_thumb:
        # 小指横向伸开时也可能靠近腕点。先检查电话手势，再用局部折回证据确认点赞。
        pip, end = q("littlePIP"), q("littleTip")
        if little:
            tucked_pinky = (bend(little, pip, end) < 110
                            or dist(little, end) < .85 * dist(little, pip))
        else:
            tucked_pinky = ("littleMCP" in clear_curl and dist(end, q("ringTip")) < .45 * palm
                            and dist(wrist, end) < .8 * dist(wrist, pip))
        return "thumb_up" if tucked_pinky else "hand_visible"
    if all_fingers and all(curled.values()) and thumb_closed and fist_thumb_tucked:
        return "fist"
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
        return "horns" if clear and world_horns else "pointing"
    if sum(extended.values()) >= 3 and sum(folded.values()) <= 1:
        return "open_palm"
    return "horns" if clear and world_horns else "hand_visible"


class GestureStabilizer:
    def __init__(self):
        self.items = {}

    def update(self, key, label, wrist, now, sample_id=None):
        self.items = {k: v for k, v in self.items.items() if 0 <= now - v["last"] <= HAND_MAX_GAP}
        old = self.items.get(key)
        if old and sample_id is not None and old["sample_id"] is not None and sample_id <= old["sample_id"]:
            if (sample_id == old["sample_id"] and now - old["last"] < .28
                    and old["label"] == label and old["count"] >= 3):
                return label
            return "hand_visible"
        count = 1
        next_gap = HAND_MAX_GAP
        if old and old["label"] == label:
            elapsed = now - old["last"]
            displacement = math.hypot(old["wrist"][0] - wrist["x"], old["wrist"][1] - wrist["y"])
            if 0 < elapsed <= old["gap"] and displacement <= .10:
                count = old["count"] + 1
                # 快速采样后失联应重新确认，较慢的连续观测则按实际间隔衔接。
                next_gap = min(HAND_MAX_GAP, max(.4, 2 * elapsed))
        self.items[key] = dict(last=now, wrist=(wrist["x"], wrist["y"]), label=label,
                               count=count, sample_id=sample_id, gap=next_gap)
        return label if count >= 3 else "hand_visible"
