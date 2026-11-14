"""Turns a saved report into real numbers.

Every figure comes from what Flow has recorded and is worked out with the same
rules as the Dashboard and Execution Pool pages: the same status groups, the
same run times, the same "per successful run" average. The output is a list of
sections, each a list of plain blocks, so the page, the email, the chat message
and the PDF all show the same values.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from revoluzen_flow.ir.node_metadata import SIDE_EFFECT_CLASS
from revoluzen_flow.monitoring.status import normalize_status
from revoluzen_flow.monitoring.store import is_production_trigger
from revoluzen_flow.monitoring.timing import execution_timing
from revoluzen_flow.reports.cron_human import cron_to_human

from .catalog import DATASETS, SECTION_BY_TYPE
from .models import ReportDefinition, ReportSection
from .schedule import ReportWindow, resolve_window, zone

logger = logging.getLogger("revoluzen_flow.reports.builder")

# More runs than this in one period and the report says so, rather than
# holding the whole history in memory.
MAX_RUNS = 100_000
MAX_CHANGES = 20_000

_STATUS_LABELS = {
    "success": "Succeeded", "failed": "Failed", "cancelled": "Stopped",
    "running": "Running", "queued": "Waiting", "skipped": "Skipped",
    "unknown": "Unknown",
}
_TRIGGER_LABELS = {
    "schedule": "Schedule", "manual": "Manual", "api": "API", "webhook": "Webhook",
    "pipeline": "Another pipeline", "test": "Test", "backfill": "Backfill",
    "file_arrival": "File arrived", "after_pipeline": "After another pipeline",
    # Started from the Editor; to the person who pressed Run these are manual.
    "websocket": "Manual", "executor": "Manual",
}
_SINK_TYPES = sorted(t for t, kind in SIDE_EFFECT_CLASS.items() if kind == "passthrough")


# ── Formatting ──────────────────────────────────────────────────────────


def fmt_int(value: Any) -> str:
    return f"{int(value or 0):,}"


def fmt_percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    ms = float(ms)
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m {int(seconds % 60):02d}s"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_seconds(value: float | None) -> str:
    return "—" if value is None else fmt_duration(float(value) * 1000)


def fmt_memory(mb: float | None) -> str:
    if mb is None:
        return "—"
    mb = float(mb)
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def fmt_bytes(value: float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "int": fmt_int, "percent": fmt_percent, "duration": fmt_duration,
    "memory": fmt_memory, "seconds": fmt_seconds,
}


def parse_time(value: Any) -> datetime | None:
    """A stored time as an aware UTC datetime; naive values are UTC."""
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def scrub(text: Any, limit: int = 220) -> str:
    """An error message that is safe to send out of the product.

    The same redaction the AI features use before text leaves the server:
    passwords in connection strings, keys, tokens, email addresses.
    """
    if not text:
        return ""
    from revoluzen_flow.ai.sanitize import sanitize_for_llm

    cleaned = sanitize_for_llm(str(text)[:4000], max_chars=4000).payload
    line = " ".join(str(cleaned).split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def trigger_label(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text.startswith("replay"):
        return "Replay"
    if text.startswith("resume"):
        return "Resume"
    return _TRIGGER_LABELS.get(text) or (text.replace("_", " ").capitalize() or "Unknown")


# ── Blocks ──────────────────────────────────────────────────────────────


def kpi(label: str, value: str, tone: str = "neutral", detail: str = "") -> dict:
    return {"label": label, "value": value, "tone": tone, "detail": detail}


def cell(text: str, tone: str = "") -> Any:
    return {"text": text, "tone": tone} if tone else text


def table(
    columns: list[tuple[str, str]],
    rows: list[list[Any]],
    empty: str,
    total: int | None = None,
) -> dict:
    return {
        "kind": "table",
        "columns": [{"label": label, "align": align} for label, align in columns],
        "rows": rows,
        "total": len(rows) if total is None else total,
        "empty": empty,
    }


def kpis(items: list[dict]) -> dict:
    return {"kind": "kpis", "items": items}


def text_block(text: str, tone: str = "") -> dict:
    return {"kind": "text", "text": text, "tone": tone}


def _rate_tone(rate: float | None) -> str:
    if rate is None:
        return "neutral"
    return "good" if rate >= 95 else "warn" if rate >= 80 else "bad"


def _count_tone(value: int, tone: str) -> str:
    return tone if value else "neutral"


def _limit(section: ReportSection) -> int:
    limit = (section.options or {}).get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return limit
    return int(SECTION_BY_TYPE[section.type].get("default_limit", 10))


# ── Reading what Flow recorded ──────────────────────────────────────────


class ReportContext:
    """One report being made: its period, its filters, and the rows it reads."""

    def __init__(self, app_state: dict, definition: ReportDefinition, workspace_id: str, now: datetime):
        self.state = app_state
        self.db = app_state["db"]
        self.definition = definition
        self.filters = definition.filters
        self.workspace_id = workspace_id
        self.now = now
        self.tz = zone(definition.timezone)
        self.window: ReportWindow = resolve_window(self.filters.time_range, definition.timezone, now)
        self.notes: list[str] = []
        self._cache: dict[str, Any] = {}

    def _cached(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    # Names ------------------------------------------------------------

    @property
    def workflows(self) -> dict[str, dict]:
        def make() -> dict[str, dict]:
            store = self.state.get("store")
            rows = store.list_all(workspace_id=self.workspace_id) if store is not None else []
            return {w["id"]: w for w in rows}
        return self._cached("workflows", make)

    @property
    def projects(self) -> dict[str, str]:
        def make() -> dict[str, str]:
            store = self.state.get("project_store")
            rows = store.list_all(workspace_id=self.workspace_id) if store is not None else []
            return {p["id"]: p.get("name") or p["id"] for p in rows}
        return self._cached("projects", make)

    def project_name(self, project_id: str | None) -> str:
        return self.projects.get(project_id or "", "") or "—"

    # Filters ----------------------------------------------------------

    def in_scope(self, workflow_id: str | None, project_id: str | None) -> bool:
        wanted = self.filters.workflow_ids
        if wanted and workflow_id not in wanted:
            return False
        projects = self.filters.project_ids
        return not projects or project_id in projects

    def _run_in_scope(self, row: dict) -> bool:
        if not self.in_scope(row["workflow_id"], row["project_id"]):
            return False
        environment = self.filters.environment
        if environment != "all" and (row.get("environment") or "dev") != environment:
            return False
        if self.filters.run_kinds == "automatic" and not is_production_trigger(row["triggered_by"]):
            return False
        return True

    # Runs -------------------------------------------------------------

    def _read_runs(self, start: datetime, end: datetime) -> list[dict]:
        marks = ",".join("?" for _ in _SINK_TYPES)
        rows = self.db.fetchall(
            "SELECT id, workflow_id, project_id, status, started_at, "
            "json_extract(data, '$.workflow_name') AS workflow_name, "
            "json_extract(data, '$.triggered_by') AS triggered_by, "
            "json_extract(data, '$.completed_at') AS completed_at, "
            "json_extract(data, '$.duration_ms') AS duration_ms, "
            "json_extract(data, '$.error_message') AS error_message, "
            "json_extract(data, '$.failed_step') AS failed_step, "
            "json_extract(data, '$.workflow_version') AS workflow_version, "
            "COALESCE(json_extract(data, '$.environment'), json_extract(data, '$.metadata.environment')) AS environment, "
            "COALESCE(memory_peak_mb, json_extract(data, '$.metadata.peak_memory_mb')) AS peak_memory_mb, "
            "json_extract(data, '$.metadata.cpu_seconds') AS cpu_seconds, "
            "(SELECT json_extract(s.value, '$.step_name') FROM json_each(data, '$.step_logs') s "
            " WHERE json_extract(s.value, '$.status') = 'error' LIMIT 1) AS first_failed_step, "
            "(SELECT COALESCE(SUM(json_extract(s.value, '$.rows_processed')), 0) "
            " FROM json_each(data, '$.step_logs') s) AS rows_processed, "
            f"(SELECT COALESCE(SUM(json_extract(s.value, '$.rows_processed')), 0) "
            f" FROM json_each(data, '$.step_logs') s "
            f" WHERE json_extract(s.value, '$.step_type') IN ({marks})) AS rows_written "
            "FROM executions "
            "WHERE workspace_id = ? AND julianday(started_at) >= julianday(?) "
            "AND julianday(started_at) < julianday(?) "
            "ORDER BY julianday(started_at) DESC LIMIT ?",
            (*_SINK_TYPES, self.workspace_id, start.isoformat(), end.isoformat(), MAX_RUNS),
        )
        if len(rows) >= MAX_RUNS and "runs_capped" not in self._cache:
            self._cache["runs_capped"] = True
            self.notes.append(f"This period has more than {MAX_RUNS:,} runs, so only the newest are counted.")
        out: list[dict] = []
        for row in rows:
            timed = execution_timing(row)
            timed["status_cat"] = normalize_status(timed.get("status"))
            timed["started"] = parse_time(timed.get("started_at"))
            timed["pipeline"] = timed.get("workflow_name") or timed.get("workflow_id") or "—"
            timed["trigger"] = trigger_label(timed.get("triggered_by"))
            timed["duration_ms"] = float(timed.get("duration_ms") or 0)
            timed["rows_processed"] = int(timed.get("rows_processed") or 0)
            timed["rows_written"] = int(timed.get("rows_written") or 0)
            if self._run_in_scope(timed):
                out.append(timed)
        return out

    @property
    def runs(self) -> list[dict]:
        return self._cached("runs", lambda: self._read_runs(self.window.start, self.window.end))

    @property
    def previous_runs(self) -> list[dict]:
        return self._cached(
            "previous_runs",
            lambda: self._read_runs(self.window.previous_start, self.window.previous_end),
        )

    @property
    def steps(self) -> list[dict]:
        """Each step of each run in the period."""
        def make() -> list[dict]:
            ids = {r["id"]: r for r in self.runs}
            if not ids:
                return []
            rows = self.db.fetchall(
                "SELECT id, json_extract(data, '$.step_logs') AS step_logs FROM executions "
                "WHERE workspace_id = ? AND julianday(started_at) >= julianday(?) "
                "AND julianday(started_at) < julianday(?)",
                (self.workspace_id, self.window.start.isoformat(), self.window.end.isoformat()),
            )
            out: list[dict] = []
            for row in rows:
                run = ids.get(row["id"])
                if run is None or not row["step_logs"]:
                    continue
                try:
                    logs = json.loads(row["step_logs"])
                except (TypeError, ValueError):
                    continue
                for log in logs if isinstance(logs, list) else []:
                    out.append({
                        "pipeline": run["pipeline"],
                        "project": self.project_name(run["project_id"]),
                        "step": log.get("step_name") or log.get("step_id") or "—",
                        "step_type": log.get("step_type") or "—",
                        "status": _STATUS_LABELS[normalize_status(log.get("status"))],
                        "rows": int(log.get("rows_processed") or 0),
                        "duration_ms": float(log.get("duration_ms") or 0),
                        "error": log.get("error_message") or "",
                        "started": run["started"],
                    })
            return out
        return self._cached("steps", make)

    # Pipeline changes ---------------------------------------------------

    @property
    def changes(self) -> list[dict]:
        """New pipelines, new versions, publishes and deployments."""
        def make() -> list[dict]:
            start, end = self.window.start.isoformat(), self.window.end.isoformat()
            out: list[dict] = []
            versions = self.db.fetchall(
                "SELECT workflow_id, version, created_by, change_summary, created_at, kind, version_name, "
                "json_extract(data, '$.workflow.name') AS name, "
                "json_extract(data, '$.workflow.project_id') AS project_id "
                "FROM workflow_versions "
                "WHERE workspace_id = ? AND COALESCE(kind, 'saved') != 'auto' "
                "AND julianday(created_at) >= julianday(?) AND julianday(created_at) < julianday(?) "
                "ORDER BY julianday(created_at) DESC LIMIT ?",
                (self.workspace_id, start, end, MAX_CHANGES),
            )
            for row in versions:
                if not self.in_scope(row["workflow_id"], row["project_id"]):
                    continue
                if row["kind"] == "restored":
                    change = "Restored version"
                elif row["version"] == 1:
                    change = "New pipeline"
                else:
                    change = "New version"
                out.append({
                    "workflow_id": row["workflow_id"],
                    "pipeline": row["name"] or row["workflow_id"],
                    "project": self.project_name(row["project_id"]),
                    "change": change,
                    "version": f"v{row['version']}",
                    "version_number": row["version"],
                    "by": row["created_by"] or "—",
                    "note": row["version_name"] or row["change_summary"] or "",
                    "when": parse_time(row["created_at"]),
                })
            events = self.db.fetchall(
                "SELECT workflow_id, event, timestamp, json_extract(data, '$.message') AS message "
                "FROM lifecycle_events "
                "WHERE workspace_id = ? AND event IN ('published', 'revoked', 'deployed', 'rollback') "
                "AND julianday(timestamp) >= julianday(?) AND julianday(timestamp) < julianday(?) "
                "ORDER BY julianday(timestamp) DESC LIMIT ?",
                (self.workspace_id, start, end, MAX_CHANGES),
            )
            names = {"published": "Published", "revoked": "Unpublished", "deployed": "Deployed", "rollback": "Rolled back"}
            for row in events:
                known = self.workflows.get(row["workflow_id"])
                project_id = known.get("project_id") if known else None
                if not self.in_scope(row["workflow_id"], project_id):
                    continue
                out.append({
                    "workflow_id": row["workflow_id"],
                    "pipeline": (known or {}).get("name") or row["workflow_id"],
                    "project": self.project_name(project_id),
                    "change": names[row["event"]],
                    "version": "",
                    "version_number": None,
                    "by": "",
                    "note": row["message"] or "",
                    "when": parse_time(row["timestamp"]),
                })
            out.sort(key=lambda r: r["when"] or self.now, reverse=True)
            return out
        return self._cached("changes", make)

    # Alerts -------------------------------------------------------------

    @property
    def alert_logs(self) -> list[dict]:
        def make() -> list[dict]:
            store = self.state.get("alert_store")
            rules = {r["id"]: r for r in store.list_rules(workspace_id=self.workspace_id)} if store is not None else {}
            rows = self.db.fetchall(
                "SELECT data FROM alert_logs WHERE workspace_id = ? "
                "AND julianday(triggered_at) >= julianday(?) AND julianday(triggered_at) < julianday(?) "
                "ORDER BY julianday(triggered_at) DESC LIMIT ?",
                (self.workspace_id, self.window.start.isoformat(), self.window.end.isoformat(), MAX_CHANGES),
            )
            out: list[dict] = []
            for row in rows:
                log = json.loads(row["data"])
                workflow_id = log.get("workflow_id") or ""
                known = self.workflows.get(workflow_id)
                if not self.in_scope(workflow_id, known.get("project_id") if known else None):
                    continue
                rule = rules.get(log.get("rule_id") or "") or {}
                out.append({
                    "rule": rule.get("name") or "Deleted rule",
                    "pipeline": (known or {}).get("name") or workflow_id or "All pipelines",
                    "channel": str(log.get("channel") or "").capitalize(),
                    "condition": str(log.get("condition") or "").replace("on_", "").replace("_", " ").capitalize(),
                    "status": "Sent" if log.get("status") == "sent" else "Not delivered",
                    "when": parse_time(log.get("triggered_at")),
                })
            return out
        return self._cached("alert_logs", make)

    # Display ------------------------------------------------------------

    def when(self, value: datetime | None) -> str:
        if value is None:
            return "—"
        local = value.astimezone(self.tz)
        return f"{local.day} {local.strftime('%b %H:%M')}"

    def local_day(self, value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.astimezone(self.tz).strftime("%Y-%m-%d")

    def local_week(self, value: datetime | None) -> str:
        if value is None:
            return "—"
        day = value.astimezone(self.tz).date()
        monday = day - timedelta(days=day.weekday())
        return f"Week of {monday.isoformat()}"


# ── Counting runs ───────────────────────────────────────────────────────


class _Tally:
    """Run counts with the Dashboard's definitions."""

    def __init__(self, rows: Iterable[dict]):
        self.rows = list(rows)
        cats = Counter(r["status_cat"] for r in self.rows)
        self.total = len(self.rows)
        self.success = cats["success"]
        self.failed = cats["failed"]
        self.cancelled = cats["cancelled"]
        self.skipped = cats["skipped"]
        self.running = cats["running"] + cats["queued"]
        finished = self.success + self.failed + self.cancelled + self.skipped
        self.rate = round(self.success / finished * 100, 1) if finished else None
        timed = [r["duration_ms"] for r in self.rows if r["status_cat"] == "success" and _timed(r)]
        self.average_ms = sum(timed) / len(timed) if timed else None
        self.longest_ms = max(timed) if timed else None


def _timed(row: dict) -> bool:
    return row["duration_ms"] > 0 or (row.get("metadata") or {}).get("duration_basis") == "recorded_start_to_finish"


def _group_runs(rows: Iterable[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["workflow_id"]].append(row)
    return groups


def _before(value: str) -> str:
    return f"{value} the period before"


# ── Sections ────────────────────────────────────────────────────────────


def build_run_summary(ctx: ReportContext, section: ReportSection) -> dict:
    now, before = _Tally(ctx.runs), _Tally(ctx.previous_runs)
    return {"blocks": [kpis([
        kpi("Runs", fmt_int(now.total), "primary", _before(fmt_int(before.total))),
        kpi("Succeeded", fmt_int(now.success), _count_tone(now.success, "good"), _before(fmt_int(before.success))),
        kpi("Failed", fmt_int(now.failed), _count_tone(now.failed, "bad"), _before(fmt_int(before.failed))),
        kpi("Stopped", fmt_int(now.cancelled), _count_tone(now.cancelled, "warn"), _before(fmt_int(before.cancelled))),
        kpi("Success rate", fmt_percent(now.rate), _rate_tone(now.rate), _before(fmt_percent(before.rate))),
        kpi("Average run time", fmt_duration(now.average_ms), "primary", "Per successful run"),
    ])]}


def _trend_buckets(ctx: ReportContext) -> tuple[list[tuple[str, str]], Callable[[datetime], str]]:
    """Every bucket in the period, including quiet ones, oldest first."""
    days = ctx.window.days
    start = ctx.window.start.astimezone(ctx.tz)
    end = (ctx.window.end - timedelta(seconds=1)).astimezone(ctx.tz)
    out: list[tuple[str, str]] = []

    if days <= 1.1:
        step = start.replace(minute=0, second=0, microsecond=0)
        while step <= end:
            out.append((step.strftime("%Y-%m-%d %H"), f"{step.strftime('%H')}:00"))
            step += timedelta(hours=1)
        return out, lambda at: at.astimezone(ctx.tz).strftime("%Y-%m-%d %H")

    if days <= 35:
        day = start.date()
        while day <= end.date():
            out.append((day.isoformat(), f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"))
            day += timedelta(days=1)
        return out, lambda at: at.astimezone(ctx.tz).date().isoformat()

    monday = start.date() - timedelta(days=start.weekday())
    while monday <= end.date():
        out.append((monday.isoformat(), f"Week of {monday.day} {monday.strftime('%b')}"))
        monday += timedelta(days=7)

    def week(at: datetime) -> str:
        day = at.astimezone(ctx.tz).date()
        return (day - timedelta(days=day.weekday())).isoformat()

    return out, week


def build_run_trend(ctx: ReportContext, section: ReportSection) -> dict:
    buckets, key_of = _trend_buckets(ctx)
    counts: dict[str, Counter] = defaultdict(Counter)
    for row in ctx.runs:
        if row["started"] is None:
            continue
        cat = row["status_cat"]
        counts[key_of(row["started"])]["success" if cat == "success" else "failed" if cat == "failed" else "other"] += 1
    rows = [
        {"label": label, "values": {
            "success": counts[key]["success"], "failed": counts[key]["failed"], "other": counts[key]["other"],
        }}
        for key, label in buckets
    ]
    return {"blocks": [{
        "kind": "bars",
        "series": [
            {"key": "success", "label": "Succeeded", "tone": "good"},
            {"key": "failed", "label": "Failed", "tone": "bad"},
            {"key": "other", "label": "Other", "tone": "neutral"},
        ],
        "rows": rows,
        "empty": "No runs in this period.",
        "has_data": any(sum(r["values"].values()) for r in rows),
    }]}


def build_failures(ctx: ReportContext, section: ReportSection) -> dict:
    failed = [r for r in ctx.runs if r["status_cat"] == "failed"]
    rows: list[tuple[int, list[Any]]] = []
    for group in _group_runs(failed).values():
        newest = max(group, key=lambda r: r["started"] or ctx.now)
        errors = Counter(scrub(r.get("error_message")) for r in group if r.get("error_message"))
        step = next((r.get("failed_step") or r.get("first_failed_step") for r in group
                     if r.get("failed_step") or r.get("first_failed_step")), "")
        rows.append((len(group), [
            newest["pipeline"],
            ctx.project_name(newest["project_id"]),
            cell(fmt_int(len(group)), "bad"),
            ctx.when(newest["started"]),
            step or "—",
            errors.most_common(1)[0][0] if errors else "—",
        ]))
    rows.sort(key=lambda item: (-item[0], str(item[1][0])))
    limit = _limit(section)
    return {"blocks": [table(
        [("Pipeline", "left"), ("Project", "left"), ("Failed runs", "right"),
         ("Last failed", "left"), ("Failing step", "left"), ("Most common error", "left")],
        [r for _, r in rows[:limit]],
        "No failed runs in this period.",
        total=len(rows),
    )]}


def build_successes(ctx: ReportContext, section: ReportSection) -> dict:
    groups = _group_runs(ctx.runs)
    rows: list[tuple[int, list[Any]]] = []
    for group in groups.values():
        tally = _Tally(group)
        if not tally.success:
            continue
        good = [r for r in group if r["status_cat"] == "success"]
        newest = max(good, key=lambda r: r["started"] or ctx.now)
        rows.append((tally.success, [
            newest["pipeline"],
            ctx.project_name(newest["project_id"]),
            cell(fmt_int(tally.success), "good"),
            cell(fmt_percent(tally.rate), _rate_tone(tally.rate)),
            fmt_duration(tally.average_ms),
            fmt_int(sum(r["rows_processed"] for r in good)),
            ctx.when(newest["started"]),
        ]))
    rows.sort(key=lambda item: (-item[0], str(item[1][0])))
    limit = _limit(section)
    return {"blocks": [table(
        [("Pipeline", "left"), ("Project", "left"), ("Succeeded", "right"), ("Success rate", "right"),
         ("Average run time", "right"), ("Rows processed", "right"), ("Last success", "left")],
        [r for _, r in rows[:limit]],
        "No successful runs in this period.",
        total=len(rows),
    )]}


def build_slowest(ctx: ReportContext, section: ReportSection) -> dict:
    previous = {wid: _Tally(group) for wid, group in _group_runs(ctx.previous_runs).items()}
    rows: list[tuple[float, list[Any]]] = []
    for wid, group in _group_runs(ctx.runs).items():
        tally = _Tally(group)
        if tally.average_ms is None:
            continue
        before = previous.get(wid)
        before_ms = before.average_ms if before else None
        if before_ms:
            change = (tally.average_ms - before_ms) / before_ms * 100
            shown = cell(f"{change:+.0f}%", "bad" if change >= 20 else "good" if change <= -20 else "")
        else:
            shown = "—"
        rows.append((tally.average_ms, [
            group[0]["pipeline"], fmt_int(tally.success), fmt_duration(tally.average_ms),
            fmt_duration(tally.longest_ms), fmt_duration(before_ms), shown,
        ]))
    rows.sort(key=lambda item: -item[0])
    limit = _limit(section)
    return {"blocks": [table(
        [("Pipeline", "left"), ("Successful runs", "right"), ("Average", "right"),
         ("Longest", "right"), ("Average, period before", "right"), ("Change", "right")],
        [r for _, r in rows[:limit]],
        "No successful runs to time in this period.",
        total=len(rows),
    )]}


def build_triggers(ctx: ReportContext, section: ReportSection) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in ctx.runs:
        groups[row["trigger"]].append(row)
    rows = []
    for name, group in sorted(groups.items(), key=lambda item: -len(item[1])):
        tally = _Tally(group)
        rows.append([
            name, fmt_int(tally.total), fmt_int(tally.success),
            cell(fmt_int(tally.failed), "bad" if tally.failed else ""),
            cell(fmt_percent(tally.rate), _rate_tone(tally.rate)),
        ])
    return {"blocks": [table(
        [("Started by", "left"), ("Runs", "right"), ("Succeeded", "right"), ("Failed", "right"), ("Success rate", "right")],
        rows, "No runs in this period.",
    )]}


def build_failed_steps(ctx: ReportContext, section: ReportSection) -> dict:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for step in ctx.steps:
        if step["status"] == "Failed":
            groups[(step["pipeline"], step["step"])].append(step)
    rows: list[tuple[int, list[Any]]] = []
    for (pipeline, name), group in groups.items():
        newest = max(group, key=lambda s: s["started"] or ctx.now)
        rows.append((len(group), [
            name, pipeline, newest["step_type"], cell(fmt_int(len(group)), "bad"),
            scrub(newest["error"]) or "—",
        ]))
    rows.sort(key=lambda item: (-item[0], str(item[1][0])))
    limit = _limit(section)
    return {"blocks": [table(
        [("Step", "left"), ("Pipeline", "left"), ("Step type", "left"), ("Failures", "right"), ("Latest error", "left")],
        [r for _, r in rows[:limit]],
        "No steps failed in this period.",
        total=len(rows),
    )]}


def build_usage(ctx: ReportContext, section: ReportSection) -> dict:
    runs = ctx.runs
    memory = [float(r["peak_memory_mb"]) for r in runs if r.get("peak_memory_mb")]
    cpu = [float(r["cpu_seconds"]) for r in runs if r.get("cpu_seconds")]
    return {"blocks": [kpis([
        kpi("Pipelines run", fmt_int(len({r["workflow_id"] for r in runs})), "primary"),
        kpi("Rows processed", fmt_int(sum(r["rows_processed"] for r in runs)), "primary", "Added up across every step"),
        kpi("Rows delivered", fmt_int(sum(r["rows_written"] for r in runs)), "primary", "Written by output steps"),
        kpi("Total run time", fmt_duration(sum(r["duration_ms"] for r in runs)), "primary"),
        kpi("CPU time", fmt_seconds(sum(cpu)) if cpu else "Not recorded", "primary" if cpu else "neutral"),
        kpi("Highest memory", fmt_memory(max(memory)) if memory else "Not recorded", "primary" if memory else "neutral", "In a single run"),
    ])]}


def build_busiest(ctx: ReportContext, section: ReportSection) -> dict:
    total_ms = sum(r["duration_ms"] for r in ctx.runs) or 0
    rows: list[tuple[float, list[Any]]] = []
    for group in _group_runs(ctx.runs).values():
        spent = sum(r["duration_ms"] for r in group)
        memory = [float(r["peak_memory_mb"]) for r in group if r.get("peak_memory_mb")]
        cpu = [float(r["cpu_seconds"]) for r in group if r.get("cpu_seconds")]
        rows.append((spent, [
            group[0]["pipeline"], fmt_int(len(group)), fmt_int(sum(r["rows_processed"] for r in group)),
            fmt_duration(spent), fmt_percent(spent / total_ms * 100 if total_ms else None),
            fmt_memory(max(memory)) if memory else "—", fmt_seconds(sum(cpu)) if cpu else "—",
        ]))
    rows.sort(key=lambda item: -item[0])
    limit = _limit(section)
    return {"blocks": [table(
        [("Pipeline", "left"), ("Runs", "right"), ("Rows processed", "right"), ("Total run time", "right"),
         ("Share of run time", "right"), ("Highest memory", "right"), ("CPU time", "right")],
        [r for _, r in rows[:limit]],
        "No runs in this period.",
        total=len(rows),
    )]}


def build_storage(ctx: ReportContext, section: ReportSection) -> dict:
    store = ctx.state.get("datastore")
    summary = store.workspace_summary(ctx.workspace_id) if store is not None else {}
    return {
        "scope_note": "The whole workspace, as it stands now.",
        "blocks": [kpis([
            kpi("Files", fmt_int(summary.get("file_count")), "primary", fmt_bytes(summary.get("file_size_bytes"))),
            kpi("Managed tables", fmt_int(summary.get("table_count")), "primary", fmt_bytes(summary.get("table_size_bytes"))),
            kpi("Pipeline outputs", fmt_int(summary.get("output_count")), "primary", fmt_bytes(summary.get("output_size_bytes"))),
            kpi("Total size", fmt_bytes(summary.get("total_size_bytes")), "primary", "Including anything in the bin"),
        ])],
    }


def build_ai_usage(ctx: ReportContext, section: ReportSection) -> dict:
    store = ctx.state.get("trace_store")
    if store is None:
        return {"blocks": [text_block("AI activity is not available on this server.")]}
    totals = store.usage_totals(
        workspace_id=ctx.workspace_id,
        since=ctx.window.start.isoformat(),
        until=ctx.window.end.isoformat(),
    )
    rows = [
        [
            " / ".join(p for p in (m.get("provider"), m.get("model")) if p) or "Not recorded",
            fmt_int(m.get("requests")), fmt_int(m.get("tokens_in")), fmt_int(m.get("tokens_out")),
        ]
        for m in totals.get("by_model") or []
    ]
    return {
        "scope_note": "The whole workspace. Totals only, with no request contents.",
        "blocks": [
            kpis([
                kpi("AI requests", fmt_int(totals.get("requests")), "primary"),
                kpi("Tokens in", fmt_int(totals.get("tokens_in")), "primary"),
                kpi("Tokens out", fmt_int(totals.get("tokens_out")), "primary"),
                kpi("Failed", fmt_int(totals.get("failed")), _count_tone(int(totals.get("failed") or 0), "bad")),
                kpi("Blocked", fmt_int(totals.get("blocked")), _count_tone(int(totals.get("blocked") or 0), "warn")),
            ]),
            table(
                [("Model", "left"), ("Requests", "right"), ("Tokens in", "right"), ("Tokens out", "right")],
                rows, "No AI requests in this period.",
            ),
        ],
    }


def _environment_name(environment: str, tier: str) -> str:
    if environment == "prod":
        return "Production"
    if environment == "dev" or tier != "plus":
        return "Development"
    return "Development and Production"


def _detect_tier() -> str:
    from revoluzen_flow.api.reports import _detect_tier as detect

    return detect()


def build_environment(ctx: ReportContext, section: ReportSection) -> dict:
    from revoluzen_flow import __version__
    from revoluzen_flow.api.dashboard import _build_pool, _build_system

    system = (_build_system().get("data") or {})
    pool = (_build_pool().get("data") or {})
    workers = pool.get("workers") or []
    busy = sum(1 for w in workers if w.get("status") == "busy")
    waiting = len(pool.get("queue") or [])
    scheduler = ctx.state.get("scheduler")
    running = bool(scheduler is not None and scheduler.is_running)

    disk_detail = ""
    disk_value = "Not recorded"
    disk_tone = "neutral"
    data_dir = ctx.state.get("data_dir")
    if data_dir:
        try:
            usage = shutil.disk_usage(str(data_dir))
            free_pct = usage.free / usage.total * 100
            disk_value = fmt_bytes(usage.free)
            disk_detail = f"Free of {fmt_bytes(usage.total)}"
            disk_tone = "bad" if free_pct < 5 else "warn" if free_pct < 15 else "good"
        except OSError:
            pass

    database_size = sum(int(f.get("size_bytes") or 0) for f in system.get("db_files") or [])
    memory_pct = system.get("host_mem_pct")
    uptime = system.get("uptime_seconds")

    items = [
        kpi("Environment", _environment_name(ctx.filters.environment, _detect_tier()), "primary"),
        kpi("Flow version", __version__, "primary", _last_upgrade(ctx)),
        kpi("Scheduler", "Running" if running else "Stopped", "good" if running else "bad"),
        kpi("Workers busy", f"{busy} of {len(workers)}", "primary", f"{waiting} waiting" if waiting else "Nothing waiting"),
    ]
    if memory_pct is not None:
        items.append(kpi("Memory in use", f"{memory_pct:.0f}%", "bad" if memory_pct >= 90 else "warn" if memory_pct >= 75 else "good"))
    # No CPU figure: an instant sample says nothing about the period, and the
    # first reading after startup is always zero.
    items.append(kpi("Disk free", disk_value, disk_tone, disk_detail))
    if database_size:
        items.append(kpi("Database size", fmt_bytes(database_size), "primary"))
    if uptime is not None:
        items.append(kpi("Running for", fmt_duration(float(uptime) * 1000), "primary"))

    attention: list[list[Any]] = []
    connection_store = ctx.state.get("connection_store")
    if connection_store is not None:
        for conn in connection_store.list_all(workspace_id=ctx.workspace_id):
            if conn.get("last_test_ok") is False:
                attention.append([
                    "Connection", conn.get("name") or conn.get("id"),
                    cell("Last test failed", "bad"), ctx.when(parse_time(conn.get("last_test_at"))),
                ])
    credential_store = ctx.state.get("credential_store")
    if credential_store is not None:
        from revoluzen_flow.credentials.expiry import expiry_band

        for credential in credential_store.list_all(workspace_id=ctx.workspace_id):
            expires_at = parse_time(credential.get("expires_at"))
            band = expiry_band(expires_at, ctx.now) if expires_at else None
            if band is None:
                continue
            kind, days = band
            attention.append([
                "Credential", credential.get("name") or credential.get("id"),
                cell("Expired", "bad") if kind == "expired" else cell(f"Expires in {days} day{'s' if days != 1 else ''}", "warn"),
                ctx.when(expires_at),
            ])
    return {
        "scope_note": "This server at the moment the report was made.",
        "blocks": [
            kpis(items),
            table(
                [("Item", "left"), ("Name", "left"), ("Needs attention", "left"), ("When", "left")],
                attention, "No connections or credentials need attention.",
            ),
        ],
    }


def _last_upgrade(ctx: ReportContext) -> str:
    """When this version of Flow first ran, if that is known."""
    from .store import read_app_versions

    history = read_app_versions(ctx.db)
    # With one entry, the date is only when recording began, not an upgrade.
    if len(history) < 2:
        return ""
    return f"Since {ctx.when(parse_time(history[-1].get('first_seen_at')))}"


def build_workspace(ctx: ReportContext, section: ReportSection) -> dict:
    pipelines = [w for w in ctx.workflows.values() if ctx.in_scope(w["id"], w.get("project_id"))]
    status = Counter(str(w.get("status") or "draft") for w in pipelines)
    connection_store = ctx.state.get("connection_store")
    schedule_store = ctx.state.get("schedule_store")
    alert_store = ctx.state.get("alert_store")
    connections = connection_store.list_all(workspace_id=ctx.workspace_id) if connection_store is not None else []
    schedules = [
        s for s in (schedule_store.list_all(workspace_id=ctx.workspace_id) if schedule_store is not None else [])
        if ctx.in_scope(s.get("workflow_id"), s.get("project_id"))
    ]
    rules = alert_store.list_rules(workspace_id=ctx.workspace_id) if alert_store is not None else []
    project_store = ctx.state.get("project_store")
    projects = project_store.list_all(workspace_id=ctx.workspace_id) if project_store is not None else []
    active = [p for p in projects if (p.get("status") or "active") != "archived"]
    return {
        "scope_note": "As it stands now, not limited to the period.",
        "blocks": [kpis([
            kpi("Pipelines", fmt_int(len(pipelines)), "primary"),
            kpi("Published", fmt_int(status["published"]), "primary"),
            kpi("Drafts", fmt_int(status["draft"]), "primary"),
            kpi("Projects", fmt_int(len(active)), "primary"),
            kpi("Connections", fmt_int(len(connections)), "primary"),
            kpi("Schedules on", fmt_int(sum(1 for s in schedules if s.get("enabled"))), "primary", f"{len(schedules)} in all"),
            kpi("Alert rules", fmt_int(len(rules)), "primary"),
        ])],
    }


def build_new_builds(ctx: ReportContext, section: ReportSection) -> dict:
    changes = ctx.changes
    count = Counter(c["change"] for c in changes)
    by_pipeline: dict[str, list[dict]] = defaultdict(list)
    for change in changes:
        by_pipeline[change["workflow_id"]].append(change)
    rows: list[tuple[datetime, list[Any]]] = []
    for group in by_pipeline.values():
        saved = [c for c in group if c["version_number"] is not None]
        newest_save = max(saved, key=lambda c: c["version_number"]) if saved else None
        latest = max(group, key=lambda c: c["when"] or ctx.now)
        marks = [name for name in ("New pipeline", "Published", "Deployed", "Rolled back", "Unpublished")
                 if any(c["change"] == name for c in group)]
        rows.append((latest["when"] or ctx.now, [
            latest["pipeline"],
            latest["project"],
            fmt_int(sum(1 for c in group if c["change"] in ("New version", "Restored version", "New pipeline"))),
            newest_save["version"] if newest_save else "—",
            ", ".join(marks) or "—",
            newest_save["by"] if newest_save else "—",
            scrub((newest_save or latest)["note"], 120) or "—",
            ctx.when(latest["when"]),
        ]))
    rows.sort(key=lambda item: item[0], reverse=True)
    limit = _limit(section)

    blocks: list[dict] = [
        kpis([
            kpi("New pipelines", fmt_int(count["New pipeline"]), "primary"),
            kpi("New versions", fmt_int(count["New version"] + count["Restored version"]), "primary", "Auto-saves are not counted"),
            kpi("Published", fmt_int(count["Published"]), "primary"),
            kpi("Deployed", fmt_int(count["Deployed"]), "primary"),
        ]),
    ]
    from .store import read_app_versions

    upgrades = [
        v for v in read_app_versions(ctx.db)[1:]
        if (at := parse_time(v.get("first_seen_at"))) is not None and ctx.window.start <= at < ctx.window.end
    ]
    for upgrade in upgrades:
        blocks.append(text_block(
            f"Flow itself was updated to version {upgrade.get('version')} on "
            f"{ctx.when(parse_time(upgrade.get('first_seen_at')))}.",
        ))
    blocks.append(table(
        [("Pipeline", "left"), ("Project", "left"), ("Versions", "right"), ("Latest", "left"),
         ("What happened", "left"), ("Saved by", "left"), ("Note", "left"), ("Last change", "left")],
        [r for _, r in rows[:limit]],
        "No new builds in this period.",
        total=len(rows),
    ))
    return {"blocks": blocks}


def _describe_pipeline_schedule(schedule: dict, workflows: dict | None = None) -> str:
    kind = schedule.get("schedule_type")
    zone_name = schedule.get("timezone") or "UTC"
    if kind == "interval":
        return f"Every {schedule.get('interval_minutes')} minutes"
    if kind == "daily":
        return f"Daily at {schedule.get('daily_time')} {zone_name}"
    if kind == "weekly":
        names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        days = ", ".join(names[d] for d in schedule.get("weekly_days") or [] if 0 <= d <= 6)
        return f"{days} at {schedule.get('weekly_time')} {zone_name}"
    if kind == "cron":
        return f"{cron_to_human(schedule.get('cron_expression') or '')} {zone_name}"
    if kind == "file_arrival":
        where = {
            "local": "on the server", "sftp": "over SFTP", "smb": "on a Windows file share", "s3": "in S3",
            "gcs": "in Google Cloud Storage", "azure_blob": "in Azure Blob Storage",
        }.get(schedule.get("file_location") or "", "")
        folder = schedule.get("file_folder") or "the folder"
        return f"When a file matching {schedule.get('file_pattern') or '*'} arrives in {folder} {where}".strip()
    if kind == "after_pipeline":
        outcome = {"success": "succeeds", "failure": "fails", "always": "finishes"}.get(
            schedule.get("after_outcome") or "success", "finishes",
        )
        before = (workflows or {}).get(schedule.get("after_workflow_id") or "") or {}
        return f"When {before['name']} {outcome}" if before.get("name") else f"When another pipeline {outcome}"
    return str(kind or "—")


def build_schedules(ctx: ReportContext, section: ReportSection) -> dict:
    store = ctx.state.get("schedule_store")
    schedules = [
        s for s in (store.list_all(workspace_id=ctx.workspace_id) if store is not None else [])
        if ctx.in_scope(s.get("workflow_id"), s.get("project_id"))
    ]
    scheduled_runs = [r for r in ctx.runs if r["trigger"] == "Schedule"]
    failed = sum(1 for r in scheduled_runs if r["status_cat"] == "failed")
    limit = _limit(section)
    rows: list[list[Any]] = []
    ordered = sorted(schedules, key=lambda s: (not s.get("enabled"), str(s.get("name") or "")))
    for schedule in ordered[:limit]:
        known = ctx.workflows.get(schedule.get("workflow_id") or "") or {}
        result = schedule.get("last_run_status") or ""
        tone = "good" if result == "success" else "bad" if result == "error" else ""
        rows.append([
            known.get("name") or schedule.get("name") or schedule.get("workflow_id"),
            _describe_pipeline_schedule(schedule, ctx.workflows),
            cell("On", "good") if schedule.get("enabled") else "Off",
            ctx.when(parse_time(schedule.get("last_run_at"))),
            cell(_STATUS_LABELS[normalize_status(result)] if result else "—", tone),
        ])
    return {"blocks": [
        kpis([
            kpi("Schedules on", fmt_int(sum(1 for s in schedules if s.get("enabled"))), "primary"),
            kpi("Schedules off", fmt_int(sum(1 for s in schedules if not s.get("enabled"))), "neutral"),
            kpi("Scheduled runs", fmt_int(len(scheduled_runs)), "primary", "In this period"),
            kpi("Of those, failed", fmt_int(failed), _count_tone(failed, "bad")),
        ]),
        table(
            [("Pipeline", "left"), ("Schedule", "left"), ("State", "left"), ("Last run", "left"), ("Last result", "left")],
            rows, "No schedules set up.", total=len(schedules),
        ),
    ]}


def build_alerts(ctx: ReportContext, section: ReportSection) -> dict:
    logs = ctx.alert_logs
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for log in logs:
        groups[(log["rule"], log["pipeline"], log["channel"])].append(log)
    rows: list[tuple[int, list[Any]]] = []
    for (rule, pipeline, channel), group in groups.items():
        lost = sum(1 for g in group if g["status"] != "Sent")
        rows.append((len(group), [
            rule, pipeline, channel, fmt_int(len(group) - lost),
            cell(fmt_int(lost), "bad" if lost else ""),
            ctx.when(max((g["when"] for g in group if g["when"]), default=None)),
        ]))
    rows.sort(key=lambda item: (-item[0], str(item[1][0])))
    lost_total = sum(1 for g in logs if g["status"] != "Sent")
    limit = _limit(section)
    return {"blocks": [
        kpis([
            kpi("Alerts sent", fmt_int(len(logs) - lost_total), "primary"),
            kpi("Not delivered", fmt_int(lost_total), _count_tone(lost_total, "bad")),
            kpi("Rules that fired", fmt_int(len({g["rule"] for g in logs})), "primary"),
        ]),
        table(
            [("Rule", "left"), ("Pipeline", "left"), ("Channel", "left"), ("Sent", "right"),
             ("Not delivered", "right"), ("Last fired", "left")],
            [r for _, r in rows[:limit]],
            "No alerts in this period.",
            total=len(rows),
        ),
    ]}


def build_note(ctx: ReportContext, section: ReportSection) -> dict:
    return {"blocks": [text_block(str((section.options or {}).get("text") or ""))]}


# ── The table someone designs ───────────────────────────────────────────


def _run_rows(ctx: ReportContext) -> list[dict]:
    rows = []
    for run in ctx.runs:
        version = run.get("workflow_version")
        rows.append({
            "pipeline": run["pipeline"],
            "project": ctx.project_name(run["project_id"]),
            "status": _STATUS_LABELS[run["status_cat"]],
            "trigger": run["trigger"],
            "day": ctx.local_day(run["started"]),
            "week": ctx.local_week(run["started"]),
            "version": f"v{version}" if version else "—",
            "failed_step": run.get("failed_step") or run.get("first_failed_step") or "—",
            "error": scrub(run.get("error_message")) or "—",
            "started": run["started"],
            "duration": run["duration_ms"],
            "timed": _timed(run),
            "rows": run["rows_processed"],
            "memory": float(run["peak_memory_mb"]) if run.get("peak_memory_mb") else None,
            "cpu": float(run["cpu_seconds"]) if run.get("cpu_seconds") else None,
        })
    return rows


def _step_rows(ctx: ReportContext) -> list[dict]:
    return [
        {
            **step,
            "day": ctx.local_day(step["started"]),
            "duration": step["duration_ms"],
            "error": scrub(step["error"]) or "—",
        }
        for step in ctx.steps
    ]


def _change_rows(ctx: ReportContext) -> list[dict]:
    return [{**c, "day": ctx.local_day(c["when"]), "by": c["by"] or "—"} for c in ctx.changes]


def _alert_rows(ctx: ReportContext) -> list[dict]:
    return [{**a, "day": ctx.local_day(a["when"])} for a in ctx.alert_logs]


_DATASET_ROWS: dict[str, Callable[[ReportContext], list[dict]]] = {
    "runs": _run_rows, "steps": _step_rows, "builds": _change_rows, "alerts": _alert_rows,
}


def _status_count(name: str) -> Callable[[list[dict]], int]:
    return lambda rows: sum(1 for r in rows if r["status"] == name)


def _run_rate(rows: list[dict]) -> float | None:
    done = [r for r in rows if r["status"] in ("Succeeded", "Failed", "Stopped", "Skipped")]
    return round(sum(1 for r in done if r["status"] == "Succeeded") / len(done) * 100, 1) if done else None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _good_times(rows: list[dict]) -> list[float]:
    return [r["duration"] for r in rows if r["status"] == "Succeeded" and r["timed"]]


_MEASURES: dict[str, dict[str, Callable[[list[dict]], Any]]] = {
    "runs": {
        "runs": len,
        "succeeded": _status_count("Succeeded"),
        "failed": _status_count("Failed"),
        "stopped": _status_count("Stopped"),
        "success_rate": _run_rate,
        "avg_duration": lambda rows: _average(_good_times(rows)),
        "max_duration": lambda rows: max(_good_times(rows), default=None),
        "total_duration": lambda rows: sum(r["duration"] for r in rows),
        "rows": lambda rows: sum(r["rows"] for r in rows),
        "max_memory": lambda rows: max((r["memory"] for r in rows if r["memory"]), default=None),
        "cpu": lambda rows: sum(r["cpu"] for r in rows if r["cpu"]) or None,
    },
    "steps": {
        "runs": len,
        "failed": _status_count("Failed"),
        "rows": lambda rows: sum(r["rows"] for r in rows),
        "avg_duration": lambda rows: _average([r["duration"] for r in rows]),
        "total_duration": lambda rows: sum(r["duration"] for r in rows),
    },
    "builds": {"changes": len},
    "alerts": {
        "fired": len,
        "sent": _status_count("Sent"),
        "failed": _status_count("Not delivered"),
    },
}


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, 0)
    if isinstance(value, datetime):
        return (0, value.timestamp())
    return (0, value)


def _measure_tone(key: str, value: Any) -> str:
    if key == "failed" and value:
        return "bad"
    if key == "success_rate":
        return _rate_tone(value)
    return ""


def build_custom_table(ctx: ReportContext, section: ReportSection) -> dict:
    options = section.options or {}
    dataset_key = options["dataset"]
    dataset = DATASETS[dataset_key]
    group_by = list(options.get("group_by") or [])
    columns = list(options.get("columns") or [])
    where = options.get("where") or {}
    limit = _limit_custom(options)
    descending = (options.get("sort_dir") or "desc") == "desc"

    rows = _DATASET_ROWS[dataset_key](ctx)
    for key, values in where.items():
        if values:
            rows = [r for r in rows if r.get(key) in values]

    dim_labels = dict(dataset["dimensions"])
    empty = f"No matching {dataset['row']}s in this period."

    if group_by:
        specs = {key: (label, fmt) for key, label, fmt in dataset["measures"]}
        functions = _MEASURES[dataset_key]
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for row in rows:
            groups[tuple(row.get(g) for g in group_by)].append(row)
        computed = []
        for key, group in groups.items():
            record: dict[str, Any] = dict(zip(group_by, key))
            for column in columns:
                record[column] = functions[column](group)
            computed.append(record)
        sort_by = options.get("sort_by") or columns[0]
        computed.sort(key=lambda r: _sort_key(r.get(sort_by)), reverse=descending)
        # Nothing to compare goes last, whichever way the table is sorted.
        computed.sort(key=lambda r: r.get(sort_by) is None)
        shown = computed[:limit]
        headers = [(dim_labels[g], "left") for g in group_by] + [(specs[c][0], "right") for c in columns]
        body = [
            [str(r.get(g) or "—") for g in group_by]
            + [cell(_FORMATTERS[specs[c][1]](r.get(c)), _measure_tone(c, r.get(c))) for c in columns]
            for r in shown
        ]
        return {"blocks": [table(headers, body, empty, total=len(computed))]}

    specs = {key: (label, fmt) for key, label, fmt in dataset["list_columns"]}
    sort_by = options.get("sort_by") or columns[0]
    rows.sort(key=lambda r: _sort_key(r.get(sort_by)), reverse=descending)
    rows.sort(key=lambda r: r.get(sort_by) is None)
    headers = [(specs[c][0], "right" if specs[c][1] in _FORMATTERS else "left") for c in columns]

    def render(row: dict, column: str) -> Any:
        fmt = specs[column][1]
        value = row.get(column)
        if fmt == "datetime":
            return ctx.when(value)
        if fmt in _FORMATTERS:
            return _FORMATTERS[fmt](value)
        text = str(value) if value not in (None, "") else "—"
        if column == "status":
            tone = "good" if text in ("Succeeded", "Sent") else "bad" if text in ("Failed", "Not delivered") else ""
            return cell(text, tone)
        return text

    body = [[render(r, c) for c in columns] for r in rows[:limit]]
    return {"blocks": [table(headers, body, empty, total=len(rows))]}


def _limit_custom(options: dict) -> int:
    limit = options.get("limit")
    return limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 20


BUILDERS: dict[str, Callable[[ReportContext, ReportSection], dict]] = {
    "run_summary": build_run_summary,
    "run_trend": build_run_trend,
    "failures": build_failures,
    "successes": build_successes,
    "slowest": build_slowest,
    "triggers": build_triggers,
    "failed_steps": build_failed_steps,
    "usage": build_usage,
    "busiest": build_busiest,
    "storage": build_storage,
    "ai_usage": build_ai_usage,
    "environment": build_environment,
    "workspace": build_workspace,
    "new_builds": build_new_builds,
    "schedules": build_schedules,
    "alerts": build_alerts,
    "custom_table": build_custom_table,
    "note": build_note,
}


def _filters_summary(ctx: ReportContext) -> list[str]:
    """What the report is limited to, in words, for its header."""
    parts: list[str] = []
    filters = ctx.filters
    if filters.project_ids:
        names = [ctx.projects.get(p) or "A deleted project" for p in filters.project_ids]
        parts.append("Projects: " + ", ".join(names))
    if filters.workflow_ids:
        names = [(ctx.workflows.get(w) or {}).get("name") or "A deleted pipeline" for w in filters.workflow_ids]
        parts.append("Pipelines: " + ", ".join(names))
    if filters.environment != "all":
        parts.append("Environment: " + _environment_name(filters.environment, "plus"))
    if filters.run_kinds == "automatic":
        parts.append("Automatic runs only")
    return parts


def build_report(
    app_state: dict,
    definition: ReportDefinition,
    workspace_id: str,
    now: datetime | None = None,
) -> dict:
    """Make the report. A section that fails says so; the others still arrive."""
    now = now or datetime.now(timezone.utc)
    ctx = ReportContext(app_state, definition, workspace_id, now)
    sections: list[dict] = []
    for section in definition.sections:
        spec = SECTION_BY_TYPE[section.type]
        try:
            built = BUILDERS[section.type](ctx, section)
        except Exception:
            logger.exception("Report section %s could not be made", section.type)
            built = {"blocks": [text_block("This section could not be made. The other sections are not affected.", "warn")]}
        title = (section.title or "").strip()
        if not title and section.type != "note":
            title = spec["title"]
        sections.append({"id": section.id, "type": section.type, "title": title, "source": spec["source"], **built})

    local = now.astimezone(ctx.tz)
    tz_name = definition.timezone or "UTC"
    return {
        "name": definition.name,
        "description": definition.description,
        "generated_at": now.isoformat(),
        "generated_label": f"{local.day} {local.strftime('%b %Y %H:%M')} {tz_name}",
        "timezone": tz_name,
        "period": {
            "start": ctx.window.start.isoformat(),
            "end": ctx.window.end.isoformat(),
            "label": ctx.window.label,
        },
        "filters": _filters_summary(ctx),
        "sections": sections,
        "notes": ctx.notes,
    }


def failed_run_count(app_state: dict, definition: ReportDefinition, workspace_id: str, now: datetime) -> int:
    """How many runs failed in the report's period, whatever sections it has."""
    ctx = ReportContext(app_state, definition, workspace_id, now)
    return sum(1 for r in ctx.runs if r["status_cat"] == "failed")
