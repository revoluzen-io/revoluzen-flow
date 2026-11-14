"""What a saved report is."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

TIME_RANGE_KINDS = (
    "last_24_hours", "last_7_days", "last_30_days", "last_90_days",
    "today", "yesterday", "this_week", "last_week", "this_month", "last_month",
    "custom",
)
ENVIRONMENTS = ("all", "dev", "prod")
RUN_KINDS = ("all", "automatic")
FREQUENCIES = ("daily", "weekly", "monthly")
SEND_WHEN = ("always", "failures")
RECIPIENT_TYPES = ("email", "slack", "teams", "webhook")

MAX_NAME = 120
MAX_DESCRIPTION = 500
MAX_SECTIONS = 30
MAX_RECIPIENTS = 50
MAX_NOTE = 4000
MAX_LIMIT = 200

_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TimeRange(BaseModel):
    kind: str = "last_7_days"
    # Only for kind == "custom". Dates in the report's own time zone.
    start: str = ""
    end: str = ""


class ReportFilters(BaseModel):
    environment: str = "all"
    project_ids: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    # "automatic" leaves out runs someone started by hand while building.
    run_kinds: str = "all"
    time_range: TimeRange = Field(default_factory=TimeRange)


class ReportSection(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    type: str
    # Blank means the section's own title.
    title: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class ReportSchedule(BaseModel):
    enabled: bool = False
    frequency: str = "weekly"
    time: str = "09:00"
    # 0 is Monday, the same as pipeline schedules.
    weekly_days: list[int] = Field(default_factory=lambda: [0])
    day_of_month: int = 1
    send_when: str = "always"


class ReportRecipient(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    type: str = "email"
    # An email address, or a webhook address. For Slack and Teams, blank means
    # the webhook saved in Settings > Notifications.
    target: str = ""


class ReportDefinition(BaseModel):
    """Everything a person decides about a report."""

    name: str
    description: str = ""
    timezone: str = "UTC"
    filters: ReportFilters = Field(default_factory=ReportFilters)
    sections: list[ReportSection] = Field(default_factory=list)
    schedule: ReportSchedule = Field(default_factory=ReportSchedule)
    recipients: list[ReportRecipient] = Field(default_factory=list)
    attach_pdf: bool = False


class SavedReport(ReportDefinition):
    id: str = Field(default_factory=_short_id)
    workspace_id: str = "default"
    created_by: str = ""
    created_by_name: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    # When the schedule was last turned on or changed. A send time that has
    # already passed by then is not owed to anyone.
    schedule_changed_at: datetime | None = None


class DeliveryState(BaseModel):
    """How the schedule stands. Kept apart from the report itself, so someone
    saving an edit while a send finishes cannot overwrite either."""

    occurrence: str = ""
    occurrence_status: str = ""
    attempts: int = 0
    last_attempt_at: datetime | None = None
    last_sent_at: datetime | None = None
    last_status: str = ""
    last_error: str = ""
    last_trigger: str = ""


class DeliveryTarget(BaseModel):
    type: str
    # An email address, or "Slack" / "Teams" / "Webhook". Never a webhook URL.
    target: str
    status: str
    error: str = ""


class ReportDelivery(BaseModel):
    id: str = Field(default_factory=_short_id)
    report_id: str
    report_name: str = ""
    workspace_id: str = "default"
    trigger: str = "manual"     # schedule | manual | test
    status: str = "failed"      # delivered | partial | failed | skipped
    detail: str = ""
    period: str = ""
    requested_by: str = ""
    targets: list[DeliveryTarget] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


def clean_line(value: str) -> str:
    """One line of text: no control characters, spaces tidied.

    Report names end up in mail headers, where a stray line break would start
    a new header.
    """
    return " ".join(_CONTROL.sub(" ", value or "").split())


def is_email(value: str) -> bool:
    return bool(_EMAIL.match(value or "")) and len(value) <= 254


def schedule_problems(schedule: ReportSchedule) -> list[str]:
    problems: list[str] = []
    if schedule.frequency not in FREQUENCIES:
        problems.append("Choose daily, weekly or monthly.")
    if not _TIME.match(schedule.time or ""):
        problems.append("Enter the send time as HH:MM.")
    if schedule.frequency == "weekly":
        days = schedule.weekly_days or []
        if not days or any(not isinstance(d, int) or d < 0 or d > 6 for d in days):
            problems.append("Choose at least one day of the week.")
    if schedule.frequency == "monthly" and not 1 <= int(schedule.day_of_month or 0) <= 28:
        # 29 to 31 do not happen every month, so a report set for one of them
        # would quietly skip a month.
        problems.append("Choose a day of the month from 1 to 28.")
    if schedule.send_when not in SEND_WHEN:
        problems.append("Choose when the report should be sent.")
    return problems


def time_range_problems(time_range: TimeRange) -> list[str]:
    if time_range.kind not in TIME_RANGE_KINDS:
        return ["Choose a time period."]
    if time_range.kind != "custom":
        return []
    if not (_DATE.match(time_range.start or "") and _DATE.match(time_range.end or "")):
        return ["Enter both dates for a custom period."]
    try:
        start = datetime.strptime(time_range.start, "%Y-%m-%d")
        end = datetime.strptime(time_range.end, "%Y-%m-%d")
    except ValueError:
        return ["Enter real dates for a custom period."]
    if end < start:
        return ["The custom period ends before it starts."]
    return []
