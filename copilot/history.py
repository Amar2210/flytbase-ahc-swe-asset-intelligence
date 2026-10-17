"""Private-chat calendar windows, independent of the field-data analysis date."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def field_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Set COPILOT_FIELD_TIMEZONE to a valid IANA timezone, for example UTC or America/Chicago, in .env.") from None


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_us(value: str) -> int | None:
    """Compare instants exactly, including offset timestamps and microseconds.

    Persisted chats have UTC offsets. Invalid or timezone-free legacy timestamps
    are excluded rather than silently assigned a timezone.
    """
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            return None
        elapsed = instant.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def resolve_history_window(*, period: str | None = None, start_date: str | None = None,
                           end_date: str | None = None, timezone_name: str = "UTC",
                           now: datetime | None = None) -> dict:
    """Resolve inclusive local dates into [start, end) UTC instants.

    Weeks are Monday-based calendar weeks. This week is Monday through Sunday;
    yesterday is the complete preceding local day, including DST transitions.
    An omitted date bound remains unbounded. The analysis-as-of date is unused.
    """
    zone = field_zone(timezone_name)
    now = now if now is not None else datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("The history clock must include a timezone.")
    today = now.astimezone(zone).date()
    if period is not None and (start_date is not None or end_date is not None):
        raise ValueError("Use period or explicit date bounds, not both.")
    if period is not None:
        if not isinstance(period, str):
            raise ValueError("Use today, yesterday, this week, or last week.")
        period = " ".join(period.lower().replace("_", " ").split())
        monday = today - timedelta(days=today.weekday())
        if period == "today":
            start, end = today, today + timedelta(days=1)
        elif period == "yesterday":
            start, end = today - timedelta(days=1), today
        elif period == "this week":
            start, end = monday, monday + timedelta(days=7)
        elif period == "last week":
            start, end = monday - timedelta(days=7), monday
        else:
            raise ValueError("Use today, yesterday, this week, or last week; use date bounds for other periods.")
    else:
        def parse(value):
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError("History date bounds must use YYYY-MM-DD.")
            return date.fromisoformat(value)
        start = parse(start_date) if start_date is not None else None
        inclusive_end = parse(end_date) if end_date is not None else None
        if start and inclusive_end and inclusive_end < start:
            raise ValueError("end_date cannot precede start_date.")
        end = inclusive_end + timedelta(days=1) if inclusive_end else None
    return {
        "period": period, "timezone": timezone_name, "reference_today": today.isoformat(),
        "week_starts": "Monday", "local_start_date": start.isoformat() if start else None,
        "local_end_date_exclusive": end.isoformat() if end else None,
        "start_at_utc": utc_text(datetime.combine(start, time.min, zone)) if start else None,
        "end_at_utc_exclusive": utc_text(datetime.combine(end, time.min, zone)) if end else None,
        "interval": "start inclusive, end exclusive",
    }
