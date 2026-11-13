"""When a report goes out, and what period a label like "last week" covers.

Pipeline schedules fire only inside a two-minute window, which suits a run but
not a weekly email: a laptop that is shut at nine on Monday would simply skip
that week. So a report works out the send time it last owed. If that has not
gone out and is not too old, it goes now, and the same arithmetic gives the
next send time the page shows. The scheduler's tick still does the calling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import DeliveryState, ReportSchedule, TimeRange

# How long after its send time a report may still go out.
GRACE = {
    "daily": timedelta(hours=12),
    "weekly": timedelta(days=3),
    "monthly": timedelta(days=7),
}
MAX_ATTEMPTS = 3
RETRY_GAP = timedelta(minutes=10)
# A monthly send is at most about 31 days away; looking a little further is
# what makes "no such day" impossible rather than a case to handle.
_LOOK_DAYS = 63


def zone(name: str | None) -> ZoneInfo | timezone:
    """The named time zone, or UTC if the name cannot be loaded."""
    text = (name or "UTC").strip() or "UTC"
    if text.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(text)
    except Exception:
        return timezone.utc


def is_known_zone(name: str | None) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    if text.upper() == "UTC":
        return True
    try:
        ZoneInfo(text)
    except Exception:
        return False
    return True


def _day_matches(schedule: ReportSchedule, day: date) -> bool:
    if schedule.frequency == "daily":
        return True
    if schedule.frequency == "weekly":
        return day.weekday() in (schedule.weekly_days or [])
    if schedule.frequency == "monthly":
        return day.day == schedule.day_of_month
    return False


def _at(schedule: ReportSchedule, day: date, tz) -> datetime:
    hour, minute = (int(p) for p in schedule.time.split(":", 1))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def latest_occurrence(schedule: ReportSchedule, tz_name: str, now: datetime) -> datetime | None:
    """The most recent send time at or before ``now``."""
    tz = zone(tz_name)
    today = now.astimezone(tz).date()
    for back in range(_LOOK_DAYS):
        day = today - timedelta(days=back)
        if _day_matches(schedule, day):
            at = _at(schedule, day, tz)
            if at <= now:
                return at
    return None


def next_occurrence(schedule: ReportSchedule, tz_name: str, now: datetime) -> datetime | None:
    """The first send time after ``now``."""
    tz = zone(tz_name)
    today = now.astimezone(tz).date()
    for ahead in range(_LOOK_DAYS):
        day = today + timedelta(days=ahead)
        if _day_matches(schedule, day):
            at = _at(schedule, day, tz)
            if at > now:
                return at
    return None


def due_occurrence(
    schedule: ReportSchedule,
    tz_name: str,
    state: DeliveryState,
    schedule_changed_at: datetime | None,
    now: datetime,
) -> datetime | None:
    """The send time this report still owes, or None if it owes nothing yet."""
    if not schedule.enabled:
        return None
    occurrence = latest_occurrence(schedule, tz_name, now)
    if occurrence is None:
        return None
    # Turning a schedule on at 10:00 is not a request for today's 09:00 report.
    if schedule_changed_at is not None and occurrence <= schedule_changed_at:
        return None
    if now - occurrence > GRACE.get(schedule.frequency, timedelta(hours=12)):
        return None
    if state.occurrence == occurrence.isoformat():
        # Only a send that reached nobody is tried again: one that reached
        # some people would reach them twice.
        if state.occurrence_status != "failed":
            return None
        if state.attempts >= MAX_ATTEMPTS:
            return None
        if state.last_attempt_at is not None and now - state.last_attempt_at < RETRY_GAP:
            return None
    return occurrence


def describe_schedule(schedule: ReportSchedule, tz_name: str) -> str:
    """The schedule in a sentence, for the page and the email footer."""
    tail = f"at {schedule.time} {tz_name or 'UTC'}"
    if schedule.frequency == "daily":
        return f"Every day {tail}"
    if schedule.frequency == "weekly":
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        days = ", ".join(names[d] for d in sorted(set(schedule.weekly_days or [])) if 0 <= d <= 6)
        return f"Every {days} {tail}"
    return f"Monthly on day {schedule.day_of_month} {tail}"


@dataclass(frozen=True)
class ReportWindow:
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime
    label: str

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400


_RANGE_LABELS = {
    "last_24_hours": "Last 24 hours",
    "last_7_days": "Last 7 days",
    "last_30_days": "Last 30 days",
    "last_90_days": "Last 90 days",
    "today": "Today",
    "yesterday": "Yesterday",
    "this_week": "This week",
    "last_week": "Last week",
    "this_month": "This month",
    "last_month": "Last month",
    "custom": "Custom period",
}
_ROLLING = {
    "last_24_hours": timedelta(hours=24),
    "last_7_days": timedelta(days=7),
    "last_30_days": timedelta(days=30),
    "last_90_days": timedelta(days=90),
}


def range_label(kind: str) -> str:
    return _RANGE_LABELS.get(kind, kind)


def _midnight(day: date, tz) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _previous_month_start(day: date) -> date:
    return _month_start(_month_start(day) - timedelta(days=1))


def _short(value: datetime, tz) -> str:
    local = value.astimezone(tz)
    return f"{local.day} {local.strftime('%b %Y')}"


def resolve_window(time_range: TimeRange, tz_name: str, now: datetime) -> ReportWindow:
    """Turn a chosen period into exact start and end times.

    The period before is the one a reader would compare with: a running week
    against the same stretch of last week, a finished week against the whole
    week before it.
    """
    tz = zone(tz_name)
    local = now.astimezone(tz)
    today = local.date()
    kind = time_range.kind

    if kind in _ROLLING:
        span = _ROLLING[kind]
        start, end = now - span, now
        previous_start, previous_end = start - span, start
    elif kind == "today":
        start = _midnight(today, tz)
        end = now
        previous_start = _midnight(today - timedelta(days=1), tz)
        previous_end = now - timedelta(days=1)
    elif kind == "yesterday":
        end = _midnight(today, tz)
        start = _midnight(today - timedelta(days=1), tz)
        previous_end = start
        previous_start = _midnight(today - timedelta(days=2), tz)
    elif kind == "this_week":
        monday = today - timedelta(days=today.weekday())
        start = _midnight(monday, tz)
        end = now
        previous_start = _midnight(monday - timedelta(days=7), tz)
        previous_end = now - timedelta(days=7)
    elif kind == "last_week":
        monday = today - timedelta(days=today.weekday())
        end = _midnight(monday, tz)
        start = _midnight(monday - timedelta(days=7), tz)
        previous_end = start
        previous_start = _midnight(monday - timedelta(days=14), tz)
    elif kind == "this_month":
        first = _month_start(today)
        start = _midnight(first, tz)
        end = now
        previous_start = _midnight(_previous_month_start(first), tz)
        # The same number of days into last month, without running past it.
        previous_end = min(previous_start + (local - start), start)
    elif kind == "last_month":
        first = _month_start(today)
        end = _midnight(first, tz)
        last = _previous_month_start(first)
        start = _midnight(last, tz)
        previous_end = start
        previous_start = _midnight(_previous_month_start(last), tz)
    else:  # custom: whole days, the end day included
        first = datetime.strptime(time_range.start, "%Y-%m-%d").date()
        last = datetime.strptime(time_range.end, "%Y-%m-%d").date()
        start = _midnight(first, tz)
        end = _midnight(last + timedelta(days=1), tz)
        span = end - start
        previous_end = start
        previous_start = start - span

    utc = timezone.utc
    start_utc = start.astimezone(utc)
    end_utc = end.astimezone(utc)
    shown_end = end_utc - timedelta(seconds=1)
    dates = _short(start_utc, tz)
    if _short(shown_end, tz) != dates:
        dates = f"{dates} to {_short(shown_end, tz)}"
    label = f"{range_label(kind)} ({dates})" if kind != "custom" else dates
    return ReportWindow(
        start=start_utc,
        end=end_utc,
        previous_start=previous_start.astimezone(utc),
        previous_end=previous_end.astimezone(utc),
        label=label,
    )
