"""姓名约束、偏好证据和任务排序。"""

import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from math import comb, sqrt

DIRECTIONS = ("girl", "boy")
DEFAULT_PROFILE = "sol-high"
MODEL_PROFILES = {
    "luna-low": ("gpt-6-luna", "low"),
    "sol-high": ("gpt-6.1-sol", "high"),
}
ROLES = ("husband", "wife")
RATINGS = ("like", "later", "dislike")
BRANCHES = {
    "scene": "景物与时节",
    "character": "品格与心境",
    "object": "借物取意",
    "allusion": "原典成词",
    "life": "生活与家族关联",
    "sound": "从全名的读音构思",
}
FORMS = {"phrase": "原文成词", "parallel": "两意相承", "modifier": "一字修饰一字",
         "action": "动作与对象", "image": "借象成意"}
AXES = {
    "image": "更喜欢哪种意象？",
    "sound": "连姓念，哪个更顺口？",
    "form": "哪个搭配更像你会用的名字？",
}
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]")


def beijing_day(now):
    return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()


def full_name(value, surname):
    """保留原字。避字不扩散到同音字、同部首或所有繁简近形。"""
    if not isinstance(value, str):
        raise ValueError("invalid_name")
    name = unicodedata.normalize("NFC", value.strip())
    if not name.startswith(surname) or len(name) != len(surname) + 2:
        raise ValueError("invalid_name")
    if not all(HAN.fullmatch(char) for char in name):
        raise ValueError("invalid_name")
    return name


def exclusion(name, surname, avoided, denied):
    try:
        if name != full_name(name, surname):
            return "invalid_name"
    except ValueError:
        return "invalid_name"
    if any(char in avoided for char in name[len(surname):]):
        return "avoided_character"
    if name in denied:
        return "explicit_veto"
    return None


def combined_rating(first, review):
    """复看只修订一个样本，两个明确相反的判断回到待定。"""
    if review is None:
        return first
    if first in (None, "later") or first == review:
        return review
    if review == "later" or first != review:
        return "later"
    return review


def clusters(examples):
    """同一出处或共享给名用字的变体合并，最多形成一条有效证据。"""
    groups = []
    for name, source, rating in sorted(examples):
        chars = set(name[1:])
        keys = {source} if source else set()
        verdicts = {rating}
        rest = []
        for group_chars, group_keys, group_verdicts in groups:
            if chars & group_chars or keys & group_keys:
                chars.update(group_chars)
                keys.update(group_keys)
                verdicts.update(group_verdicts)
            else:
                rest.append((group_chars, group_keys, group_verdicts))
        groups = rest + [(chars, keys, verdicts)]
    return Counter(next(iter(verdicts)) for _, _, verdicts in groups if len(verdicts) == 1)


def estimate(examples):
    """Beta(1,1) 为无样本先验。用 30% 点的累积分布判断 90% 上界。"""
    counts = clusters([item for item in examples if item[2] in ("like", "dislike")])
    wins, losses = counts["like"], counts["dislike"]
    alpha, beta = 1 + wins, 1 + losses
    trials = alpha + beta - 1
    below_cutoff = sum(comb(trials, k) * .3 ** k * .7 ** (trials - k)
                       for k in range(alpha, trials + 1))
    mean = alpha / (alpha + beta)
    spread = sqrt(mean * (1 - mean) / (alpha + beta + 1))
    return {"positive": wins, "negative": losses, "mean": mean, "spread": spread,
            "reduced": wins + losses >= 6 and below_cutoff >= .90}


def rank(candidates, own, other, history, comparison, sequence):
    """先照顾更犹豫的人，保留约六分之一的探索位置。"""
    own = {branch: estimate(own.get(branch, [])) for branch in BRANCHES}
    other = {branch: estimate(other.get(branch, [])) for branch in BRANCHES}
    explore = sequence % 6 == 5

    def key(candidate):
        branch = candidate["branch"]
        self_view, other_view = own[branch], other[branch]
        recent = history.count(branch)
        weak = min(self_view["mean"], other_view["mean"])
        bonus = comparison.get((branch, candidate["form"]), 0)
        reduced = self_view["reduced"] or other_view["reduced"]
        if explore:
            return (max(self_view["spread"], other_view["spread"]) - .08 * recent,
                    not reduced, candidate["quality"], -candidate["id"])
        return (not reduced, weak + bonus - .12 * recent, candidate["quality"], -candidate["id"])

    normal = [c for c in candidates if not (own[c["branch"]]["reduced"] or other[c["branch"]]["reduced"])]
    return max((candidates if explore else normal) or candidates, key=key, default=None)


def model_task(state):
    """本地基线同时是 Decisions 影子结果的比较对象。"""
    if state["paused"]:
        return "wait"
    if state["review_due"]:
        return "review"
    if state["useful_pair"]:
        return "compare"
    return "flash" if state["unseen"] else "wait"
