"""Household calendar occurrences, never a device/source or execution authority.

Only the first real instant of a repeated local time is eligible. Missing local
times are skipped. The runtime separately persists consumption before any work.
Fold semantics: https://docs.python.org/3/library/zoneinfo.html#using-zoneinfo
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ClockOccurrence:
    day: str
    at: datetime


def _aware(now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("schedule_requires_aware_time")
    return now.astimezone(UTC)


def _on_date(trigger, day):
    if day.isoweekday() not in trigger.weekdays:
        return None
    wall = datetime.combine(day, time.fromisoformat(trigger.at))
    zone = ZoneInfo(trigger.timezone)
    candidates = []
    for fold in (0, 1):
        stamp = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if stamp.astimezone(zone).replace(tzinfo=None) == wall:
            candidates.append(stamp)
    return ClockOccurrence(day.isoformat(), min(candidates)) if candidates else None


def due_occurrence(trigger, now, *, grace_seconds):
    now = _aware(now)
    due = once_occurrence(trigger) if trigger.type == 'once' else _on_date(
        trigger, now.astimezone(ZoneInfo(trigger.timezone)).date())
    return due if due and 0 <= (now - due.at).total_seconds() <= grace_seconds else None


def next_occurrence(trigger, now, *, after_day=None):
    now = _aware(now)
    if trigger.type == 'once':
        due = once_occurrence(trigger)
        return due if due.at >= now and (after_day is None or due.day > after_day) else None
    day = now.astimezone(ZoneInfo(trigger.timezone)).date()
    for offset in range(8):
        candidate = _on_date(trigger, day + timedelta(days=offset))
        if candidate and candidate.at >= now and (after_day is None or candidate.day > after_day):
            return candidate
    return None


def once_occurrence(trigger):
    at = _aware(datetime.fromisoformat(trigger.at))
    return ClockOccurrence(at.astimezone(ZoneInfo(trigger.timezone)).date().isoformat(), at)


def require_future_once(trigger, now):
    if trigger.type == 'once' and once_occurrence(trigger).at <= _aware(now):
        raise ValueError('schedule_time_unavailable')


def clock_payload(trigger, occurrence):
    from .rule_compiler import digest

    return dict(schedule=digest(trigger), local_date=occurrence.day, scheduled_at=occurrence.at.isoformat())


def clock_event_matches(trigger, event, *, preview=False):
    if event.kind != "source-report" or event.source != (
        "SIMULATED-core-rule-trial" if preview else "friday.core.clock"
    ):
        return False
    stamp = event.payload.get("scheduled_at")
    if not isinstance(stamp, str) or len(stamp) > 40:
        return False
    try:
        at = _aware(datetime.fromisoformat(stamp))
        occurrence = due_occurrence(trigger, at, grace_seconds=0)
    except (TypeError, ValueError):
        return False
    return bool(
        occurrence
        and event.payload == clock_payload(trigger, occurrence)
        and (preview or event.wall_time == occurrence.at)
    )


def schedule_label(trigger):
    if trigger.type == 'once':
        local = once_occurrence(trigger).at.astimezone(ZoneInfo(trigger.timezone))
        clock = local.strftime('%Y-%m-%d %H:%M' if not local.second else '%Y-%m-%d %H:%M:%S')
        repeated = local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
        fold = ('，第二次' if local.fold else '，第一次') if repeated else ''
        return f'{clock}（家庭时间{fold}）· 仅一次'
    days = trigger.weekdays
    period = (
        "每天"
        if days == list(range(1, 8))
        else "周一至周五"
        if days == [1, 2, 3, 4, 5]
        else "周末"
        if days == [6, 7]
        else "每周" + "、".join("一二三四五六日"[n - 1] for n in days)
    )
    return f"{period} {trigger.at}（家庭时间）"
