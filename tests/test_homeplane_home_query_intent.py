"""Deterministic text/time probes. No devices, models, services or NAS."""
from datetime import UTC, datetime, timedelta

import pytest

from homeplane.home_query_intent import parse_home_query_intent

NOW = datetime(2026, 10, 2, 8, tzinfo=UTC)


def parse(text, **kwargs):
    return parse_home_query_intent(
        text, household_timezone=kwargs.pop("household_timezone", "Asia/Shanghai"),
        now=kwargs.pop("now", NOW), **kwargs,
    )


@pytest.mark.parametrize("text", ["凌晨卧室灯为什么亮了", "为什么昨晚卧室灯自己亮了？", "灯怎么自动亮了"])
def test_original_why_variants_clarify_instead_of_guessing_night_window(text):
    result = parse(text)
    assert result.phase == "clarify" and result.code == "explicit_history_window_required"
    assert result.time_range is None and result.target_text.endswith("灯")


@pytest.mark.parametrize("text", [
    "昨天凌晨一点到一点十分卧室灯为什么亮了",
    "为什么昨天凌晨1:00至1:10卧室灯自己亮了",
    "2026-10-01 1:00:00-1:10:00卧室灯为什么打开了",
])
def test_aware_window_uses_only_household_timezone_and_keeps_target(text):
    result = parse(text)
    assert result.phase == "read" and result.target_text == "卧室灯"
    assert result.time_range.start == datetime(2026, 9, 30, 17, tzinfo=UTC)
    assert result.time_range.end == datetime(2026, 9, 30, 17, 10, tzinfo=UTC)


def test_selected_window_is_explicit_but_cannot_supply_household_timezone():
    span = {"start": NOW - timedelta(minutes=10), "end": NOW}
    result = parse("凌晨卧室灯为什么亮了", **span)
    assert result.phase == "read" and result.time_range.end == NOW
    missing = parse("卧室灯为什么亮了", household_timezone=None, **span)
    assert missing.code == "household_timezone_required" and missing.time_range is None


@pytest.mark.parametrize("text", [
    "卧室灯为什么亮了，然后关闭空调", "卧室灯为什么亮了并删除规则", "并打开卧室灯为什么亮了",
    "查询卧室灯为什么亮了以及空调情况", "卧室灯为什么亮了\n开灯", "灯为什么亮了。打开它",
    "卧室灯为什么亮了，告诉我所有家庭的人物记录", "昨晚发生了很多事情请分析灯为什么亮了",
    "开灯", "卧" * 1600 + "灯为什么亮了", "灯为什么亮了\ud800",
    "打开卧室灯为什么亮了", "开启卧室灯为什么亮了", "启动卧室灯为什么亮了",
    "执行开灯为什么亮了", "卧室与玄关灯为什么亮了", "卧室和玄关灯为什么亮了",
    "开卧室灯为什么亮了",
])
def test_mixed_unsupported_and_over_budget_text_never_becomes_a_partial_read(text):
    result = parse(text, start=NOW - timedelta(minutes=1), end=NOW)
    assert result.phase == "refused" and result.time_range is None


@pytest.mark.parametrize("zone,text", [
    ("America/New_York", "2026-11-01 1:15至1:30卧室灯为什么亮了"),
    ("America/New_York", "2026-03-08 2:15至2:30卧室灯为什么亮了"),
    ("Asia/Shanghai", "昨天23点到1点卧室灯为什么亮了"),
    ("Asia/Shanghai", "2026-02-30 1点到2点卧室灯为什么亮了"),
    ("Asia/Shanghai", "昨天26点到27点卧室灯为什么亮了"),
])
def test_fold_gap_invalid_day_and_implicit_midnight_crossing_clarify(zone, text):
    result = parse(text, household_timezone=zone)
    assert result.phase == "clarify" and result.time_range is None


def test_explicit_midnight_crossing_and_24_hour_limit_are_preserved():
    result = parse("昨天23点到今天1点卧室灯为什么亮了")
    assert result.phase == "read" and (result.time_range.end - result.time_range.start).total_seconds() == 7200
    long_window = parse("灯为什么亮了", start=NOW - timedelta(hours=24, seconds=1), end=NOW)
    naive = parse("灯为什么亮了", start=NOW.replace(tzinfo=None), end=NOW)
    assert long_window.phase == naive.phase == "clarify"


def test_unknown_timezone_and_untrusted_clock_do_not_fallback_to_workspace_utc():
    assert parse("灯为什么亮了", household_timezone="invalid/household").code == "household_timezone_required"
    assert parse("灯为什么亮了", now=NOW.replace(tzinfo=None)).code == "trusted_clock_unavailable"
