"""Small full-string Why grammar; interpreting text never grants read scope.

No model, clock inference or history reader lives here. The flow owner supplies
the household timezone; ambiguous dates/times require an explicit selection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .archive_contracts import ArchiveTimeRange

_NUMBER = r"[零〇一二三四五六七八九十两0-9]{1,3}"
_CLOCK = rf"(?:[0-2]?\d:[0-5]\d(?::[0-5]\d)?|{_NUMBER}点(?:半|{_NUMBER}分)?)"
_DAY = r"(?:\d{4}-\d{2}-\d{2}|今天|昨天|前天|昨晚)"
_RANGE = re.compile(
    rf"(?P<day>{_DAY})\s*(?:凌晨)?(?P<start>{_CLOCK})\s*(?:到|至|—|-)\s*"
    rf"(?:(?P<end_day>{_DAY})\s*)?(?:凌晨)?(?P<end>{_CLOCK})\s*(?P<target>.+)"
)
_INSTANT = re.compile(rf"(?:(?:{_DAY})\s*)?(?:凌晨)?{_CLOCK}\s*(?P<target>.+)")
_VAGUE = re.compile(r"(?:(?:今天|昨天|前天|昨晚|凌晨)\s*)+(?P<target>.+)")
_QUESTION = (
    re.compile(r"(?P<body>.+?)(?:为什么|怎么)(?:自己|自动)?(?:亮了|打开了|开了|还亮着|亮着|是亮的)"),
    re.compile(r"为什么(?P<body>.+?)(?:自己|自动)?(?:亮了|打开了|开了|还亮着|亮着|是亮的)"),
)
_MIXED = re.compile(
    r"[\n\r\t,，;；。!！]|然后|顺便|同时|以及|并且|并把|并打开|并开|帮我|请把|"
    r"关掉|关闭|删除|修改|调亮|设为|暂停|不要"
)
_TARGET = re.compile(r"[\w\u3400-\u9fff .:-]{1,128}")
_SENTENCE = re.compile(
    r"^(?:开|关)|为什么|怎么|亮了|打开|开启|开灯|开一下|开起来|启动|开了|"
    r"执行|运行|设置|操作|重启|重置|请|分析|发生|查询|告诉|所有|以及|然后|和|与"
)


@dataclass(frozen=True)
class HomeReadIntent:
    """Local parse outcome, not a second Query or permission contract."""

    phase: str
    code: str
    target_text: str = ""
    time_range: ArchiveTimeRange | None = None


def _number(value: str) -> int:
    if value.isascii() and value.isdecimal():
        return int(value)
    digits = dict(zip("零〇一二三四五六七八九两", (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    if value in digits:
        return digits[value]
    if value.count("十") == 1:
        first, second = value.split("十")
        if (not first or first in digits) and (not second or second in digits):
            return digits.get(first, 1) * 10 + digits.get(second, 0)
    raise ValueError("invalid_local_time")


def _clock(value: str) -> time:
    if ":" in value:
        return time(*(int(part) for part in value.split(":")))
    hour, minute = value.split("点")
    return time(_number(hour), 30 if minute == "半" else _number(minute[:-1]) if minute else 0)


def _day(value: str, today: date) -> date:
    offsets = {"今天": 0, "昨天": 1, "前天": 2, "昨晚": 1}
    return today - timedelta(days=offsets[value]) if value in offsets else date.fromisoformat(value)


def _unique_local(day: date, value: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(day, value)
    candidates = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates[utc.isoformat()] = utc
    if len(candidates) != 1:
        raise ValueError("ambiguous_or_nonexistent_local_time")
    return next(iter(candidates.values()))


def parse_home_query_intent(
    text: str, *, household_timezone: str | None, now: datetime,
    start: datetime | None = None, end: datetime | None = None,
) -> HomeReadIntent:
    """Read the entire question, or refuse/clarify without executing any part.

    Accepted examples: '凌晨卧室灯为什么亮了' (clarifies time),
    '昨天凌晨一点到一点十分卧室灯为什么亮了', and a simple Why
    question with an explicitly selected aware start/end. No guessed night span.
    """
    try:
        invalid = type(text) is not str or len(text.encode("utf-8")) > 4096
    except UnicodeError:
        invalid = True
    if invalid or not text.strip():
        return HomeReadIntent("refused", "unsupported_why_text")
    text = text.strip()
    if text.endswith(("?", "？")):
        text = text[:-1].strip()
    if _MIXED.search(text) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text):
        return HomeReadIntent("refused", "mixed_or_unsupported_why_text")
    matched = next((m for pattern in _QUESTION if (m := pattern.fullmatch(text))), None)
    if matched is None:
        return HomeReadIntent("refused", "unsupported_why_text")
    body = matched["body"].strip()
    range_match = _RANGE.fullmatch(body)
    time_match = range_match or _INSTANT.fullmatch(body) or _VAGUE.fullmatch(body)
    target = (time_match["target"] if time_match else body).strip()
    if (_SENTENCE.search(target) or not _TARGET.fullmatch(target)
            or not (target.endswith("灯") or target.startswith("light."))):
        return HomeReadIntent("refused", "unsupported_why_target")
    if not household_timezone:
        return HomeReadIntent("clarify", "household_timezone_required", target)
    try:
        zone = ZoneInfo(household_timezone)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return HomeReadIntent("clarify", "household_timezone_required", target)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return HomeReadIntent("refused", "trusted_clock_unavailable", target)
    try:
        if start is not None or end is not None:
            if (not isinstance(start, datetime) or not isinstance(end, datetime)
                    or start.tzinfo is None or end.tzinfo is None
                    or start.utcoffset() is None or end.utcoffset() is None):
                raise ValueError("aware_history_window_required")
            left, right = start.astimezone(UTC), end.astimezone(UTC)
        elif range_match:
            today = now.astimezone(zone).date()
            left_day = _day(range_match["day"], today)
            right_day = _day(range_match["end_day"], today) if range_match["end_day"] else left_day
            left = _unique_local(left_day, _clock(range_match["start"]), zone)
            right = _unique_local(right_day, _clock(range_match["end"]), zone)
        else:
            return HomeReadIntent("clarify", "explicit_history_window_required", target)
        span = ArchiveTimeRange(start=left, end=right)
        if (right - left).total_seconds() > 86400:
            raise ValueError("bounded_history_window_required")
        return HomeReadIntent("read", "bounded_why_intent", target, span)
    except (ValueError, TypeError, OverflowError):
        return HomeReadIntent("clarify", "invalid_or_ambiguous_history_window", target)
