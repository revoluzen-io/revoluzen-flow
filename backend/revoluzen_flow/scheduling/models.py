"""Pipeline scheduling models.

A schedule row is anything that starts a pipeline without a person pressing
Run: a time (cron, interval, daily, weekly), a file arriving in a folder, or
another pipeline finishing. The two event kinds live in ``triggers/``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ScheduleType(str, Enum):
    CRON = "cron"
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"
    # A new file in a watched folder starts one run (triggers/file_watch.py).
    FILE_ARRIVAL = "file_arrival"
    # Another pipeline finishing starts a run (triggers/chain.py).
    AFTER_PIPELINE = "after_pipeline"


#: Kinds that fire on something happening rather than on the clock.
EVENT_SCHEDULE_TYPES = frozenset({ScheduleType.FILE_ARRIVAL.value, ScheduleType.AFTER_PIPELINE.value})

#: Where a file-arrival trigger can look. "local" is a folder on the Flow server,
#: "smb" a Windows file share.
FILE_LOCATIONS = ("local", "sftp", "smb", "s3", "gcs", "azure_blob")

#: How the other pipeline has to end for an after-pipeline trigger to fire.
AFTER_OUTCOMES = ("success", "failure", "always")

#: Bounds for the file checks, in seconds. The scheduler ticks every 30.
FILE_POLL_MIN, FILE_POLL_MAX = 30, 24 * 3600
FILE_SETTLE_MAX = 24 * 3600


def schedule_type_value(value: Any) -> str:
    """The plain string of a schedule type, whether it came as the enum or text."""
    return getattr(value, "value", value) or ""


def is_event_schedule(row: dict | Any) -> bool:
    """True for file-arrival and after-pipeline rows, which never fire on the clock."""
    kind = row.get("schedule_type") if isinstance(row, dict) else getattr(row, "schedule_type", "")
    return schedule_type_value(kind) in EVENT_SCHEDULE_TYPES


def upgrade_legacy_row(data: Any) -> Any:
    """Read a row saved before the event triggers existed.

    Until 2026-11 a schedule could be saved as ``schedule_type: "event"`` with
    an ``event_trigger`` and ``event_source_id`` that nothing ever acted on. Such
    a row becomes the real trigger it described, switched off: it never ran, so
    starting it silently now would surprise whoever saved it. A
    ``pipeline_completed`` row names the other pipeline; any other becomes a
    file-arrival trigger with no folder yet.
    """
    if not isinstance(data, dict):
        return data
    if "event_trigger" not in data and "event_source_id" not in data and data.get("schedule_type") != "event":
        return data
    data = dict(data)
    trigger = str(data.pop("event_trigger", "") or "")
    source = str(data.pop("event_source_id", "") or "")
    if data.get("schedule_type") == "event":
        if trigger == "pipeline_completed" and source:
            data["schedule_type"] = ScheduleType.AFTER_PIPELINE.value
            data.setdefault("after_workflow_id", source)
            data.setdefault("after_outcome", "success")
        else:
            data["schedule_type"] = ScheduleType.FILE_ARRIVAL.value
        data["enabled"] = False
    return data


class _TriggerFields(BaseModel):
    """Settings shared by the stored row and the create and update bodies."""

    # ── When a file arrives ──
    # local | sftp | smb | s3 | gcs | azure_blob
    file_location: str = ""
    # The saved connection for SFTP, a Windows file share and the cloud stores.
    # Empty for "local".
    file_connection_id: str = ""
    # A folder on the server, a folder on the SFTP server, a folder on the share
    # (inside the connection's folder; empty for that folder), or a key prefix
    # in the bucket or container. Only files directly inside it count.
    file_folder: str = ""
    # Which file names count, such as ``orders_*.csv``. Case does not matter.
    file_pattern: str = "*"
    # The pipeline parameter that receives the file's path. Empty: none.
    file_parameter: str = ""
    # A file must keep the same size and time for this long before its run
    # starts, so a file still being copied in is not read half-written.
    file_settle_seconds: int = 60
    # How often the folder is listed.
    file_poll_seconds: int = 60
    # Also start runs for files already in the folder when the trigger is
    # created. Off: only files that arrive afterwards count.
    file_include_existing: bool = False

    # ── After another pipeline ──
    after_workflow_id: str = ""
    # success | failure | always
    after_outcome: str = "success"

    @field_validator("file_location", check_fields=False)
    @classmethod
    def _known_location(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip().lower()
        if value and value not in FILE_LOCATIONS:
            raise ValueError(f"file_location must be one of: {', '.join(FILE_LOCATIONS)}")
        return value

    @field_validator("after_outcome", check_fields=False)
    @classmethod
    def _known_outcome(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in AFTER_OUTCOMES:
            raise ValueError(f"after_outcome must be one of: {', '.join(AFTER_OUTCOMES)}")
        return value

    @field_validator("file_pattern", check_fields=False)
    @classmethod
    def _sane_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip() or "*"
        if len(value) > 200 or "/" in value or "\\" in value:
            raise ValueError("file_pattern is a file name pattern such as orders_*.csv, without folders")
        return value

    @field_validator("file_settle_seconds", check_fields=False)
    @classmethod
    def _settle_bounds(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if not 0 <= int(value) <= FILE_SETTLE_MAX:
            raise ValueError(f"file_settle_seconds must be between 0 and {FILE_SETTLE_MAX}")
        return int(value)

    @field_validator("file_poll_seconds", check_fields=False)
    @classmethod
    def _poll_bounds(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if not FILE_POLL_MIN <= int(value) <= FILE_POLL_MAX:
            raise ValueError(f"file_poll_seconds must be between {FILE_POLL_MIN} and {FILE_POLL_MAX}")
        return int(value)


class Schedule(_TriggerFields):
    """A pipeline schedule definition."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str
    project_id: str = "default"
    # Tenant boundary — a schedule always belongs to the workspace of
    # the workflow it triggers. The background scheduler loop iterates
    # every workspace (system-level), but every user-facing API call
    # filters by the caller's workspace_id. Legacy rows back-filled to
    # 'default' by v8.
    workspace_id: str = "default"
    name: str = ""
    schedule_type: ScheduleType = ScheduleType.CRON
    enabled: bool = True

    # Cron. Day of week as in standard cron: 0 and 7 are Sunday, 1 is Monday.
    cron_expression: str = ""  # e.g., "0 */6 * * *"

    # Interval
    interval_minutes: int = 60

    # Daily
    daily_time: str = "00:00"  # HH:MM

    # Weekly
    weekly_days: list[int] = Field(default_factory=list)  # 0=Mon..6=Sun
    weekly_time: str = "00:00"

    # Timezone
    timezone: str = "UTC"

    # Pipeline parameter values — when the scheduler fires this row, the
    # values here are passed to the executor. System placeholders like
    # ${utcnow:%Y-%m-%d} are resolved at fire time, not at schedule-create
    # time, so a daily schedule actually gets today's date every run.
    parameter_values: dict[str, Any] = Field(default_factory=dict)

    # Tracking
    last_run_at: datetime | None = None  # when the last run ended
    last_fired_at: datetime | None = None  # when it started
    last_run_status: str | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    # Event triggers: when the folder was last looked at, and what a person
    # should know about it ("2 files waiting", "Cannot reach the server: ...").
    last_checked_at: datetime | None = None
    status_message: str = ""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy(cls, data: Any) -> Any:
        return upgrade_legacy_row(data)


class ScheduleCreate(_TriggerFields):
    workflow_id: str
    project_id: str = "default"
    name: str = ""
    schedule_type: ScheduleType = ScheduleType.CRON
    enabled: bool = True
    cron_expression: str = ""
    interval_minutes: int = 60
    daily_time: str = "00:00"
    weekly_days: list[int] = Field(default_factory=list)
    weekly_time: str = "00:00"
    timezone: str = "UTC"
    parameter_values: dict[str, Any] = Field(default_factory=dict)
    # 2026-05-26 — Required when the pipeline contains an append_risky
    # or external sink. Scheduled pipelines re-run on a cadence, so an
    # unsafe sink will multiply rows / re-fire side effects on every
    # tick. The API returns 400 with `code: unsafe_for_schedule` and
    # the offending sink list unless this flag is set. Mirrors the
    # `acknowledge_side_effects` guardrail on backfills.
    acknowledge_side_effects: bool = False


class ScheduleUpdate(_TriggerFields):
    name: str | None = None
    schedule_type: ScheduleType | None = None
    enabled: bool | None = None
    cron_expression: str | None = None
    interval_minutes: int | None = None
    daily_time: str | None = None
    weekly_days: list[int] | None = None
    weekly_time: str | None = None
    timezone: str | None = None
    parameter_values: dict[str, Any] | None = None
    # Left out means "keep what is stored", so none of these has a default.
    file_location: str | None = None
    file_connection_id: str | None = None
    file_folder: str | None = None
    file_pattern: str | None = None
    file_parameter: str | None = None
    file_settle_seconds: int | None = None
    file_poll_seconds: int | None = None
    file_include_existing: bool | None = None
    after_workflow_id: str | None = None
    after_outcome: str | None = None
