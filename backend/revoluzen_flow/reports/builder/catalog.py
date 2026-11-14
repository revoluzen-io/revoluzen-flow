"""The sections a report can hold, and the reports people can start from.

Every section here reads something Flow really records. If a section is added,
it needs a builder in ``collect.py``; ``tests/test_report_builder.py`` fails if
the two lists drift apart.
"""

from __future__ import annotations

from typing import Any

from .models import MAX_LIMIT, MAX_NOTE, ReportSection

# Where the numbers come from is shown beside each section in the designer.
SECTIONS: list[dict[str, Any]] = [
    {
        "type": "run_summary", "group": "Runs", "title": "Run summary",
        "description": "How many runs there were, how many succeeded or failed, and how that compares with the period before.",
        "source": "Run history",
    },
    {
        "type": "run_trend", "group": "Runs", "title": "Runs over time",
        "description": "Successes and failures per hour, day or week, depending on the period.",
        "source": "Run history",
    },
    {
        "type": "failures", "group": "Runs", "title": "Failures",
        "description": "Which pipelines failed, how often, where, and their most common error.",
        "source": "Run history", "has_limit": True, "default_limit": 10,
    },
    {
        "type": "successes", "group": "Runs", "title": "Successful pipelines",
        "description": "Pipelines that succeeded, with their success rate, run time and rows.",
        "source": "Run history", "has_limit": True, "default_limit": 10,
    },
    {
        "type": "slowest", "group": "Runs", "title": "Slowest pipelines",
        "description": "Average and longest run time, and how the average has moved.",
        "source": "Run history", "has_limit": True, "default_limit": 10,
    },
    {
        "type": "triggers", "group": "Runs", "title": "How runs were started",
        "description": "Runs by schedule, by hand, by API and by other triggers.",
        "source": "Run history",
    },
    {
        "type": "failed_steps", "group": "Runs", "title": "Steps that fail most",
        "description": "The individual steps behind the failures.",
        "source": "Step results", "has_limit": True, "default_limit": 10,
    },
    {
        "type": "usage", "group": "Usage", "title": "Usage",
        "description": "Pipelines run, rows processed and delivered, run time, CPU time and highest memory.",
        "source": "Run history and step results",
    },
    {
        "type": "busiest", "group": "Usage", "title": "Busiest pipelines",
        "description": "Which pipelines take the most run time, rows and memory.",
        "source": "Run history and step results", "has_limit": True, "default_limit": 10,
    },
    {
        "type": "storage", "group": "Usage", "title": "Storage",
        "description": "Files, managed tables and pipeline outputs, and how much space they take.",
        "source": "Storage", "ignores_filters": True,
    },
    {
        "type": "ai_usage", "group": "Usage", "title": "AI use",
        "description": "AI requests and tokens in the period, by model.",
        "source": "AI activity", "ignores_filters": True,
    },
    {
        "type": "environment", "group": "Environment", "title": "Environment",
        "description": "Flow version, scheduler, workers, memory, disk, and connections or credentials that need attention.",
        "source": "This server, at the moment the report is made", "ignores_filters": True,
    },
    {
        "type": "workspace", "group": "Environment", "title": "What's in the workspace",
        "description": "Counts of pipelines, projects, connections, schedules and alert rules.",
        "source": "Workspace contents",
    },
    {
        "type": "new_builds", "group": "Changes", "title": "New builds",
        "description": "New pipelines, new saved versions, and anything published or deployed.",
        "source": "Pipeline versions and lifecycle events", "has_limit": True, "default_limit": 20,
    },
    {
        "type": "schedules", "group": "Schedules and alerts", "title": "Schedules",
        "description": "Which schedules are on, when they last ran and how that went.",
        "source": "Schedules and run history", "has_limit": True, "default_limit": 20,
    },
    {
        "type": "alerts", "group": "Schedules and alerts", "title": "Alerts sent",
        "description": "Which alert rules fired, and any that could not be delivered.",
        "source": "Alert log", "has_limit": True, "default_limit": 20,
    },
    {
        "type": "custom_table", "group": "Your own", "title": "A table you design",
        "description": "Choose the data, what to group it by, and the columns.",
        "source": "Run history, step results, pipeline versions or the alert log",
    },
    {
        "type": "note", "group": "Your own", "title": "A note",
        "description": "Your own words, such as who the report is for or what to look out for.",
        "source": "Typed by you",
    },
]

SECTION_BY_TYPE = {s["type"]: s for s in SECTIONS}

RUN_STATUSES = ["Succeeded", "Failed", "Stopped", "Running", "Waiting", "Skipped"]
RUN_TRIGGERS = ["Schedule", "Manual", "Test", "API", "Webhook", "Replay", "Resume", "Backfill", "Another pipeline", "Event"]

# fmt is one of: int, percent, duration, memory, seconds, text, datetime.
DATASETS: dict[str, dict[str, Any]] = {
    "runs": {
        "label": "Runs",
        "row": "run",
        "dimensions": [
            ("pipeline", "Pipeline"), ("project", "Project"), ("status", "Status"),
            ("trigger", "Started by"), ("day", "Day"), ("week", "Week"),
            ("version", "Pipeline version"), ("failed_step", "Failed step"),
        ],
        "measures": [
            ("runs", "Runs", "int"), ("succeeded", "Succeeded", "int"),
            ("failed", "Failed", "int"), ("stopped", "Stopped", "int"),
            ("success_rate", "Success rate", "percent"),
            ("avg_duration", "Average run time", "duration"),
            ("max_duration", "Longest run", "duration"),
            ("total_duration", "Total run time", "duration"),
            ("rows", "Rows processed", "int"),
            ("max_memory", "Highest memory", "memory"),
            ("cpu", "CPU time", "seconds"),
        ],
        "list_columns": [
            ("started", "Started", "datetime"), ("pipeline", "Pipeline", "text"),
            ("project", "Project", "text"), ("status", "Status", "text"),
            ("trigger", "Started by", "text"), ("duration", "Run time", "duration"),
            ("rows", "Rows processed", "int"), ("version", "Pipeline version", "text"),
            ("failed_step", "Failed step", "text"), ("error", "Error", "text"),
        ],
        "filters": [("status", "Status", RUN_STATUSES), ("trigger", "Started by", RUN_TRIGGERS)],
        "default_group": ["pipeline"],
        "default_columns": ["runs", "failed", "success_rate", "avg_duration"],
    },
    "steps": {
        "label": "Steps",
        "row": "step",
        "dimensions": [
            ("step", "Step"), ("pipeline", "Pipeline"), ("step_type", "Step type"),
            ("status", "Status"), ("day", "Day"),
        ],
        "measures": [
            ("runs", "Times run", "int"), ("failed", "Failed", "int"),
            ("rows", "Rows", "int"),
            ("avg_duration", "Average time", "duration"),
            ("total_duration", "Total time", "duration"),
        ],
        "list_columns": [
            ("started", "Started", "datetime"), ("pipeline", "Pipeline", "text"),
            ("step", "Step", "text"), ("step_type", "Step type", "text"),
            ("status", "Status", "text"), ("duration", "Time", "duration"),
            ("rows", "Rows", "int"), ("error", "Error", "text"),
        ],
        "filters": [("status", "Status", ["Succeeded", "Failed", "Skipped"])],
        "default_group": ["step"],
        "default_columns": ["runs", "failed", "avg_duration"],
    },
    "builds": {
        "label": "Pipeline changes",
        "row": "change",
        "dimensions": [
            ("pipeline", "Pipeline"), ("project", "Project"), ("change", "Change"),
            ("by", "Changed by"), ("day", "Day"),
        ],
        "measures": [("changes", "Changes", "int")],
        "list_columns": [
            ("when", "When", "datetime"), ("pipeline", "Pipeline", "text"),
            ("change", "Change", "text"), ("version", "Version", "text"),
            ("by", "Changed by", "text"), ("note", "Note", "text"),
        ],
        "filters": [("change", "Change", ["New pipeline", "New version", "Restored version", "Published", "Unpublished", "Deployed", "Rolled back"])],
        "default_group": ["pipeline"],
        "default_columns": ["changes"],
    },
    "alerts": {
        "label": "Alerts",
        "row": "alert",
        "dimensions": [
            ("rule", "Rule"), ("pipeline", "Pipeline"), ("channel", "Channel"),
            ("condition", "Condition"), ("status", "Result"), ("day", "Day"),
        ],
        "measures": [("fired", "Fired", "int"), ("sent", "Sent", "int"), ("failed", "Not delivered", "int")],
        "list_columns": [
            ("when", "When", "datetime"), ("rule", "Rule", "text"),
            ("pipeline", "Pipeline", "text"), ("channel", "Channel", "text"),
            ("condition", "Condition", "text"), ("status", "Result", "text"),
        ],
        "filters": [("status", "Result", ["Sent", "Not delivered"])],
        "default_group": ["rule"],
        "default_columns": ["fired", "failed"],
    },
}


def _section(section_type: str, **options: Any) -> dict[str, Any]:
    return {"type": section_type, "options": options}


TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "weekly_digest",
        "name": "Weekly operations digest",
        "description": "The week at a glance: environment, usage, successes, failures and new builds.",
        "time_range": "last_7_days",
        "frequency": "weekly",
        "sections": [
            _section("run_summary"), _section("run_trend"), _section("environment"),
            _section("usage"), _section("failures"), _section("successes"), _section("new_builds"),
        ],
    },
    {
        "id": "daily_check",
        "name": "Daily run check",
        "description": "Did last night's runs work? Successes, failures and what broke.",
        "time_range": "last_24_hours",
        "frequency": "daily",
        "sections": [_section("run_summary"), _section("failures"), _section("schedules")],
    },
    {
        "id": "failure_review",
        "name": "Failure review",
        "description": "Where things are going wrong, down to the step, and which alerts went out.",
        "time_range": "last_7_days",
        "frequency": "weekly",
        "sections": [_section("run_summary"), _section("failures"), _section("failed_steps"), _section("alerts")],
    },
    {
        "id": "usage_capacity",
        "name": "Usage and capacity",
        "description": "What the platform is doing and whether the machine can carry it.",
        "time_range": "last_30_days",
        "frequency": "monthly",
        "sections": [
            _section("usage"), _section("busiest"), _section("slowest"),
            _section("storage"), _section("ai_usage"), _section("environment"),
        ],
    },
    {
        "id": "whats_changed",
        "name": "What changed",
        "description": "New pipelines, new versions and anything published or deployed.",
        "time_range": "last_7_days",
        "frequency": "weekly",
        "sections": [_section("new_builds"), _section("workspace")],
    },
]


def section_problems(section: ReportSection) -> list[str]:
    spec = SECTION_BY_TYPE.get(section.type)
    if spec is None:
        return [f"Unknown section: {section.type}."]
    options = section.options or {}
    if len(section.title or "") > 120:
        return [f"The title of '{spec['title']}' is too long."]
    limit = options.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT):
        return [f"'{spec['title']}': show between 1 and {MAX_LIMIT} rows."]
    if section.type == "note":
        text = options.get("text")
        if not isinstance(text, str) or not text.strip():
            return ["Write something in the note, or remove it."]
        if len(text) > MAX_NOTE:
            return [f"Keep a note under {MAX_NOTE} characters."]
    if section.type == "custom_table":
        return custom_table_problems(options)
    return []


def custom_table_problems(options: dict[str, Any]) -> list[str]:
    dataset = DATASETS.get(options.get("dataset") or "")
    if dataset is None:
        return ["Choose what the table is about."]
    group_by = options.get("group_by") or []
    columns = options.get("columns") or []
    dims = {key for key, _ in dataset["dimensions"]}
    measures = {key for key, _, _ in dataset["measures"]}
    list_columns = {key for key, _, _ in dataset["list_columns"]}
    if not isinstance(group_by, list) or len(group_by) > 2 or any(g not in dims for g in group_by):
        return ["Group a table by at most two of the offered fields."]
    if len(set(group_by)) != len(group_by):
        return ["Group a table by two different fields."]
    allowed = measures if group_by else list_columns
    if not isinstance(columns, list) or not columns or any(c not in allowed for c in columns):
        return ["Choose at least one column for the table."]
    where = options.get("where") or {}
    known = {key: set(values) for key, _, values in dataset["filters"]}
    if not isinstance(where, dict):
        return ["The table's filter is not valid."]
    for key, values in where.items():
        if key not in known or not isinstance(values, list) or any(v not in known[key] for v in values):
            return ["The table's filter is not valid."]
    sort_by = options.get("sort_by") or ""
    if sort_by and sort_by not in (set(columns) | set(group_by)):
        return ["Sort the table by one of its own columns."]
    if (options.get("sort_dir") or "desc") not in ("asc", "desc"):
        return ["The table's sort order is not valid."]
    return []


def public_catalog() -> dict[str, Any]:
    """The catalog as the designer page reads it."""
    return {
        "sections": SECTIONS,
        "datasets": {
            key: {
                "label": d["label"],
                "row": d["row"],
                "dimensions": [{"key": k, "label": label} for k, label in d["dimensions"]],
                "measures": [{"key": k, "label": label} for k, label, _ in d["measures"]],
                "list_columns": [{"key": k, "label": label} for k, label, _ in d["list_columns"]],
                "filters": [{"key": k, "label": label, "values": values} for k, label, values in d["filters"]],
                "default_group": d["default_group"],
                "default_columns": d["default_columns"],
            }
            for key, d in DATASETS.items()
        },
        "templates": TEMPLATES,
    }
