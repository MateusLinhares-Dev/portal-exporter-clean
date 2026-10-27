"""Inclusive historical cutoff and half-open 6-month calendar windows."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import re

_PT_BR_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?!\d)")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def parse_report_date(value: str) -> date | None:
    """Parse a visible report date, without assuming it is the process creation date."""
    if not value:
        return None
    match = _ISO_DATE.search(value)
    try:
        if match:
            return date(*map(int, match.groups()))
        match = _PT_BR_DATE.search(value)
        if match:
            day, month, year = map(int, match.groups())
            return date(year, month, day)
    except ValueError:
        return None
    return None


def previous_six_months(end: date) -> date:
    """Subtract six calendar months; clamp to last day if needed."""
    y = end.year - (end.month <= 6)
    m = ((end.month - 7) % 12) + 1
    from calendar import monthrange
    return date(y, m, min(end.day, monthrange(y, m)[1]))


def scheduled_semester(now: datetime, timezone: str) -> tuple[date, date]:
    """At Jan 1 or Jul 1, return the *previous* completed semester [start,end)."""
    local = now.astimezone(ZoneInfo(timezone))
    if local.month == 1:
        return date(local.year - 1, 7, 1), date(local.year, 1, 1)
    if local.month == 7:
        return date(local.year, 1, 1), date(local.year, 7, 1)
    raise ValueError('Scheduled semester expected on January or July')


def eligible_report(mode: str, report_date: str, options: dict) -> tuple[bool, str]:
    if mode == 'test':
        return True, 'test'
    actual = parse_report_date(report_date)
    if actual is None:
        return False, 'missing_or_invalid_date'
    if mode == 'historical':
        cutoff = date.fromisoformat(options['cutoff_date'])
        return (actual <= cutoff), 'after_cutoff' if actual > cutoff else 'in_scope'
    start = date.fromisoformat(options['window_start'])
    end = date.fromisoformat(options['window_end'])
    return start <= actual < end, 'outside_window' if not (start <= actual < end) else 'in_scope'
