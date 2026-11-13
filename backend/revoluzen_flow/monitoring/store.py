"""SQLite-backed execution history store for monitoring dashboard."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator
from .timing import execution_timing


class StepLog(BaseModel):
    """Log for a single step within an execution."""
    step_id: str
    step_name: str = ""
    step_type: str = ""
    status: str = "pending"  # pending | running | success | error | skipped
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float = 0
    rows_processed: int = 0
    error_message: str | None = None
    output_preview: list[dict[str, Any]] | None = None


def _compute_ir_sha(snapshot: dict[str, Any] | None) -> str | None:
    """D1 — SHA-256 over the JSON-canonical workflow snapshot.

    Canonical form = json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
    UTF-8 encoded. Returns None when the snapshot is missing so the
    field stays NULL for legacy / minimal rows. Cheap (< 1ms for a
    typical pipeline IR).
    """
    if not snapshot:
        return None
    import hashlib
    import json
    try:
        canon = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return None
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class ExecutionRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str
    workflow_name: str = ""
    project_id: str = "default"
    # Tenant boundary — inherited from the parent workflow at record
    # creation time. Executions are historical/audit data and NEVER
    # change workspace after being written, even if the workflow is
    # moved or deleted. Legacy rows back-filled to 'default' by v10.
    workspace_id: str = "default"
    status: str = "running"  # running | success | error | cancelled | queued
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_ms: float = 0
    steps_total: int = 0
    steps_completed: int = 0
    steps_failed: int = 0
    error_message: str | None = None
    failed_step: str | None = None
    triggered_by: str = "manual"  # manual | schedule | api | webhook | file_arrival | after_pipeline | ...
    step_logs: list[StepLog] = Field(default_factory=list)
    # Full IR of the workflow at the moment this run started. Stored so
    # "Rerun" and "Open in Editor" can replay the exact pipeline that ran
    # even if the workflow was edited since. Also the foundation for the
    # Approval Diff (D6) artifact — proposed IR vs. actual IR. None on
    # legacy rows created before this column existed.
    workflow_snapshot: dict[str, Any] | None = None
    # D1 (2026-05-26) — SHA-256 over the JSON-canonical workflow_snapshot.
    # Lets the UI prove "the IR that ran is bit-for-bit identical to a
    # known-good version" without rendering the full IR. Computed once
    # at record-creation time by the executor (see executor._snapshot_ir).
    # None on rows written before this column existed and on records
    # that omit the snapshot. The IR Replay endpoint reads BOTH fields:
    # snapshot to reconstitute the Workflow, sha to embed in the new
    # replay run's metadata so the audit trail closes.
    ir_sha: str | None = None
    # Saved pipeline version this run executed. The pipeline moves on after
    # the run, so its current version says nothing about this one. None on
    # rows written before this field existed and on paths without a version.
    workflow_version: int | None = None

    @model_validator(mode="after")
    def _auto_fill_ir_sha(self):
        """Auto-compute ir_sha from workflow_snapshot at construction
        time if the caller didn't supply one — keeps the 6 existing
        ExecutionRecord(workflow_snapshot=...) call sites working
        without code changes. If a caller explicitly passes ir_sha,
        respect that value (lets a downstream replay record claim a
        specific lineage)."""
        if self.ir_sha is None and self.workflow_snapshot is not None:
            self.ir_sha = _compute_ir_sha(self.workflow_snapshot)
        timed = execution_timing(self.model_dump())
        self.duration_ms = timed['duration_ms']
        self.metadata = timed['metadata']
        return self

    # Sprint 2 PR5 step 7 — execution budget (requested) + actuals
    # (observed). All NULL-tolerant: thread-based pipeline runs won't
    # have memory_peak_mb; legacy rows have every field NULL. Persisted
    # both in the JSON blob (via model_dump) AND as top-level columns
    # (via ExecutionStore._save, for query-friendliness).
    budget_memory_mb: int | None = None
    budget_runtime_s: int | None = None
    budget_max_attempts: int | None = None
    memory_peak_mb: float | None = None
    runtime_ms: float | None = None
    attempts: int | None = None
    exit_reason: str | None = None  # ok | budget_memory | budget_runtime | cancelled | killed_throttle | error
    # Per-run metadata bag — `peak_memory_mb`, `cpu_seconds`, `sample_count`,
    # `parameter_values`, `replay_of`, etc. Stored as a JSON sub-object so
    # fields can be added without a schema migration. The Pool history
    # endpoint (`pool.py::_enrich_history_row`) reads `peak_memory_mb` and
    # `cpu_seconds` from this bag to populate the compute columns.
    metadata: dict[str, Any] = Field(default_factory=dict)


#: Runs that happened by themselves: not someone pressing Run while building.
PRODUCTION_TRIGGERS = frozenset({
    "schedule", "webhook", "replay", "api", "pipeline", "file_arrival", "after_pipeline",
})


def is_production_trigger(triggered_by: str | None) -> bool:
    """Replay rows read "replay:<exec_id_prefix>", so those match by prefix."""
    trigger = (triggered_by or "").lower()
    return trigger in PRODUCTION_TRIGGERS or trigger.startswith("replay")


class ExecutionStore:
    """Execution history store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _list_json(self, *args, **kwargs):
        return [execution_timing(row) for row in self._db.list_json(*args, **kwargs)]

    def _save(self, execution: ExecutionRecord):
        data = execution_timing(execution.model_dump(mode="json"))
        execution.duration_ms = data['duration_ms']
        execution.metadata = data['metadata']
        self._db.insert_json(
            "executions", execution.id, data,
            workflow_id=execution.workflow_id,
            project_id=execution.project_id,
            workspace_id=execution.workspace_id or "default",
            status=execution.status,
            started_at=execution.started_at.isoformat(),
            # v17 columns — None values skip-through fine; SQLite
            # stores NULL, and insert_json just passes kwargs through
            # to the INSERT. Rows before v17 simply omit these kwargs.
            budget_memory_mb=execution.budget_memory_mb,
            budget_runtime_s=execution.budget_runtime_s,
            budget_max_attempts=execution.budget_max_attempts,
            memory_peak_mb=execution.memory_peak_mb,
            runtime_ms=execution.runtime_ms,
            attempts=execution.attempts,
            exit_reason=execution.exit_reason,
        )

    def record(self, execution: ExecutionRecord) -> ExecutionRecord:
        self._save(execution)
        return execution

    def update(
        self,
        execution_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> ExecutionRecord | None:
        data = self._db.get_json("executions", execution_id)
        if data is None:
            return None
        if workspace_id is not None:
            if (data.get("workspace_id") or "default") != workspace_id:
                return None
        exe = ExecutionRecord(**data)
        for key, value in updates.items():
            # Historical audit record — workspace is immutable once written.
            if key == "workspace_id":
                continue
            if hasattr(exe, key):
                setattr(exe, key, value)
        self._save(exe)
        return exe

    def get(
        self,
        execution_id: str,
        workspace_id: str | None = None,
    ) -> ExecutionRecord | None:
        data = self._db.get_json("executions", execution_id)
        if data is None:
            return None
        if workspace_id is not None:
            if (data.get("workspace_id") or "default") != workspace_id:
                return None
        return ExecutionRecord(**data)

    def list_all(self, limit: int = 200, workspace_id: str | None = None) -> list[dict]:
        if workspace_id is not None:
            return self._list_json(
                "executions", "workspace_id = ?", (workspace_id,),
                order_by=f"started_at DESC LIMIT {limit}",
            )
        return self._list_json(
            "executions",
            order_by=f"started_at DESC LIMIT {limit}",
        )

    def list_by_workflow(
        self,
        workflow_id: str,
        limit: int = 50,
        workspace_id: str | None = None,
    ) -> list[dict]:
        if workspace_id is not None:
            return self._list_json(
                "executions",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
                order_by=f"started_at DESC LIMIT {limit}",
            )
        return self._list_json(
            "executions", "workflow_id = ?", (workflow_id,),
            order_by=f"started_at DESC LIMIT {limit}",
        )

    def version_run_stats(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
        limit: int = 5000,
    ) -> dict[int, dict]:
        """Runs of one pipeline grouped by the saved version each one ran:
        ``{version: {"runs": n, "last_status": ..., "last_started_at": ...}}``.

        Runs that recorded no version (rows from before the field existed)
        are left out. Reads the newest ``limit`` runs.
        """
        sql = (
            "SELECT json_extract(data, '$.workflow_version') AS v, status, started_at "
            "FROM executions WHERE workflow_id = ?"
        )
        params: list = [workflow_id]
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        sql += " ORDER BY julianday(started_at) DESC, id DESC LIMIT ?"
        params.append(int(limit))
        out: dict[int, dict] = {}
        for row in self._db.fetchall(sql, tuple(params)):
            v = row["v"]
            if not isinstance(v, int):
                continue
            entry = out.get(v)
            if entry is None:
                out[v] = {
                    "runs": 1,
                    "last_status": row["status"] or "",
                    "last_started_at": row["started_at"] or "",
                }
            else:
                entry["runs"] += 1
        return out

    def list_by_project(
        self,
        project_id: str,
        limit: int = 100,
        workspace_id: str | None = None,
    ) -> list[dict]:
        if workspace_id is not None:
            return self._list_json(
                "executions",
                "project_id = ? AND workspace_id = ?",
                (project_id, workspace_id),
                order_by=f"started_at DESC LIMIT {limit}",
            )
        return self._list_json(
            "executions", "project_id = ?", (project_id,),
            order_by=f"started_at DESC LIMIT {limit}",
        )

    def run_counts(
        self,
        hours: int = 24,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        """Runs over a window, counted by status in ten-minute slots, for charts.

        Charts used to bucket the newest rows of the run list, which is capped,
        so a busy workspace lost everything older than its last few hundred
        runs while the headline beside the chart still counted them. Counting
        here has no such limit.

        Each slot also carries the summed ``duration_ms`` of its runs that
        recorded one, and how many did (``timed``), so the Dashboard's average
        duration has a trend line like its other headline numbers: the same
        runs its average counts (successful, with a duration), by slot.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        where = ["started_at >= ?"]
        params: list[Any] = [cutoff]
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        dur = "CAST(json_extract(data, '$.duration_ms') AS REAL)"
        rows = self._db.fetchall(
            "SELECT substr(started_at, 1, 15) AS slot, status, COUNT(*) AS count, "
            f"SUM(CASE WHEN {dur} > 0 THEN {dur} ELSE 0 END) AS duration_ms, "
            f"SUM(CASE WHEN {dur} > 0 THEN 1 ELSE 0 END) AS timed "
            f"FROM executions WHERE {' AND '.join(where)} GROUP BY slot, status",
            params,
        )
        # Rows store UTC, so the slot is the start of a UTC ten-minute window.
        return [
            {
                "at": f"{r['slot']}0:00Z", "status": r["status"], "count": r["count"],
                "duration_ms": round(float(r["duration_ms"] or 0), 1), "timed": int(r["timed"] or 0),
            }
            for r in rows
        ]

    def get_stats(self, hours: int = 24, workspace_id: str | None = None) -> dict:
        """Get execution statistics for the monitor dashboard.

        When ``workspace_id`` is provided, restricts the stats to that
        workspace — critical so a dev in workspace A doesn't see runtime
        counts from workspace B in their own dashboard.
        """
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - (hours * 3600)

        # Fetch recent executions
        if workspace_id is not None:
            all_data = self._list_json(
                "executions", "workspace_id = ?", (workspace_id,)
            )
        else:
            all_data = self._list_json("executions")
        recent = []
        for d in all_data:
            started = d.get("started_at", "")
            try:
                ts = datetime.fromisoformat(started).timestamp()
                if ts > cutoff:
                    recent.append(d)
            except (ValueError, TypeError):
                pass

        # 2026-05-22 (audit J3): canonical status normalization.
        # Previously this only counted ``status == "error"`` as a
        # failure, which silently excluded ``failed`` / ``timeout`` /
        # ``cancelled`` writers and made the Dashboard's failure
        # count disagree with /monitor/failed and the Executions page.
        # ``normalize_status`` is the single source of truth — any
        # new status writer should be aliased there, not added to
        # the counters individually.
        from revoluzen_flow.monitoring.status import normalize_status

        # 2026-05-28 — "production" triggers vs "exploratory" triggers.
        # Reported in internal testing: the headline Success Rate KPI was including
        # manual test runs the user fired while iterating on a pipeline,
        # which made the metric noisy (every failed test dragged the
        # rate down). The fix is to compute TWO success-rate numbers:
        #
        #   - All runs (legacy `success_rate` field — backwards compat)
        #   - Production runs only — scheduled + webhook + replay + runs
        #     another application started through a published endpoint
        #     (new `scheduled.*` sub-dict — what the headline KPI should
        #     show for an operational view)
        #
        # ``manual`` and ``test`` triggers are deliberately EXCLUDED
        # from the production set. ``ephemeral`` runs were already
        # excluded earlier in this session (they no longer write to
        # the execution log at all). Backfill child runs use the
        # `manual` trigger today; they're also excluded — a backfill
        # window failing is a backfill problem, not a production-
        # health problem.
        cats: dict[str, int] = {
            "success": 0, "failed": 0, "running": 0,
            "queued": 0, "cancelled": 0, "skipped": 0, "unknown": 0,
        }
        sched_cats: dict[str, int] = {
            "success": 0, "failed": 0, "running": 0,
            "queued": 0, "cancelled": 0, "skipped": 0, "unknown": 0,
        }
        for e in recent:
            normalized = normalize_status(e.get("status"))
            cats[normalized] += 1
            if is_production_trigger(e.get("triggered_by")):
                sched_cats[normalized] += 1

        total = len(recent)
        success = cats["success"]
        failed = cats["failed"]
        running = cats["running"]
        queued = cats["queued"]

        # Average over SUCCESSFUL runs only, matching the Dashboard card's
        # "per successful run" note and docs/dashboard-metrics.md. A run that
        # failed fast, or was killed by a timeout, says nothing about how long
        # the pipeline takes, and would drag the figure either way. Running and
        # queued runs have no finished duration yet and stay out regardless.
        avg_duration = 0.0
        completed = [e for e in recent
                     if normalize_status(e.get("status")) == "success"
                     and (e.get("duration_ms", 0) > 0
                          or (e.get("metadata") or {}).get("duration_basis") == "recorded_start_to_finish")]
        if completed:
            avg_duration = sum(e["duration_ms"] for e in completed) / len(completed)

        # success_rate denominator excludes still-running and queued
        # rows — they can't have succeeded yet. Previously this divided
        # by `total` which under-counted the rate while a run was in
        # flight (a single in-flight run on an otherwise-clean window
        # showed up as < 100% even though nothing had failed).
        terminal_total = (
            cats["success"] + cats["failed"]
            + cats["cancelled"] + cats["skipped"]
        )
        sched_total = sum(sched_cats.values())
        sched_terminal = (
            sched_cats["success"] + sched_cats["failed"]
            + sched_cats["cancelled"] + sched_cats["skipped"]
        )
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "running": running,
            "queued": queued,
            # New: surface cancelled / skipped / unknown so the
            # Dashboard can render full status breakdowns. Older
            # consumers that read only success/failed/running/queued
            # keep working.
            "cancelled": cats["cancelled"],
            "skipped": cats["skipped"],
            "unknown": cats["unknown"],
            "success_rate": (
                round(success / terminal_total * 100, 1)
                if terminal_total > 0 else 0
            ),
            "avg_duration_ms": round(avg_duration, 1),
            "period_hours": hours,
            # 2026-05-28 — production-only sub-dict (scheduled + API + webhook
            # + replay triggers). The Dashboard's headline Success Rate
            # KPI should read this so manual test runs the user fires
            # while iterating don't drag the operational health number
            # down. Legacy `success_rate` above stays for back-compat —
            # existing reports / Exports / API consumers that want
            # all-runs numbers keep their semantics.
            "scheduled": {
                "total": sched_total,
                "success": sched_cats["success"],
                "failed": sched_cats["failed"],
                "running": sched_cats["running"],
                "queued": sched_cats["queued"],
                "cancelled": sched_cats["cancelled"],
                "skipped": sched_cats["skipped"],
                "unknown": sched_cats["unknown"],
                "success_rate": (
                    round(sched_cats["success"] / sched_terminal * 100, 1)
                    if sched_terminal > 0 else 0
                ),
            },
        }
