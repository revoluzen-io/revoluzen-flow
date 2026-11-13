"""SQLite storage for incidents and their timeline.

One open incident per pipeline at a time. A failure while one is open is
another occurrence of it; the next successful run resolves it. Every step
(opened, failed again, a fix scheduled or tried, resolved, a note) is an
event, so the timeline says what happened and in what order.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace_id: str = "default"
    workflow_id: str
    workflow_name: str = ""
    status: str = "open"            # open | resolved
    failure_class: str = "unknown"  # engine/failure_class.FailureClass value
    failed_step: str = ""           # the step's label, as the run reported it
    error: str = ""
    opened_at: str = Field(default_factory=_now)
    last_seen_at: str = Field(default_factory=_now)
    resolved_at: str | None = None
    resolution: str = ""
    occurrences: int = 1
    first_run_id: str = ""
    last_run_id: str = ""
    auto_attempts: int = 0


class IncidentEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    incident_id: str
    at: str = Field(default_factory=_now)
    kind: str
    run_id: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


_COLUMNS = (
    "id", "workspace_id", "workflow_id", "workflow_name", "status", "failure_class",
    "failed_step", "error", "opened_at", "last_seen_at", "resolved_at", "resolution",
    "occurrences", "first_run_id", "last_run_id", "auto_attempts",
)


def _row(r: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(r, dict):
        return {c: r.get(c) for c in columns}
    try:
        return {c: r[c] for c in columns}
    except (TypeError, IndexError, KeyError):
        return dict(zip(columns, r))


class IncidentStore:
    """Incidents and their events, in the app database."""

    def __init__(self, db):
        self.db = db
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id            TEXT PRIMARY KEY,
                workspace_id  TEXT NOT NULL DEFAULT 'default',
                workflow_id   TEXT NOT NULL,
                workflow_name TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL,
                failure_class TEXT NOT NULL DEFAULT 'unknown',
                failed_step   TEXT NOT NULL DEFAULT '',
                error         TEXT NOT NULL DEFAULT '',
                opened_at     TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL,
                resolved_at   TEXT,
                resolution    TEXT NOT NULL DEFAULT '',
                occurrences   INTEGER NOT NULL DEFAULT 1,
                first_run_id  TEXT NOT NULL DEFAULT '',
                last_run_id   TEXT NOT NULL DEFAULT '',
                auto_attempts INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_incidents_workflow
            ON incidents(workflow_id, status)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_incidents_workspace
            ON incidents(workspace_id, status, last_seen_at)
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS incident_events (
                id          TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                at          TEXT NOT NULL,
                kind        TEXT NOT NULL,
                run_id      TEXT NOT NULL DEFAULT '',
                detail      TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_incident_events_incident
            ON incident_events(incident_id, at)
        """)
        try:
            # Database.execute() does not commit (only the DDL above uses it);
            # every write below goes through execute_write.
            self.db.conn.commit()
        except Exception:  # noqa: BLE001
            pass

    # ── Incidents ────────────────────────────────────────────────────

    def _save(self, inc: Incident) -> Incident:
        values = tuple(getattr(inc, c) for c in _COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        self.db.execute_write(
            f"INSERT OR REPLACE INTO incidents ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
        return inc

    def get(self, incident_id: str, workspace_id: str | None = None) -> Incident | None:
        rows = self.db.fetchall(
            f"SELECT {', '.join(_COLUMNS)} FROM incidents WHERE id = ?", (incident_id,),
        )
        if not rows:
            return None
        inc = Incident(**_row(rows[0], _COLUMNS))
        if workspace_id is not None and inc.workspace_id != workspace_id:
            return None
        return inc

    def open_for_workflow(self, workflow_id: str, workspace_id: str) -> Incident | None:
        rows = self.db.fetchall(
            f"SELECT {', '.join(_COLUMNS)} FROM incidents "
            "WHERE workflow_id = ? AND workspace_id = ? AND status = 'open' "
            "ORDER BY opened_at DESC LIMIT 1",
            (workflow_id, workspace_id),
        )
        return Incident(**_row(rows[0], _COLUMNS)) if rows else None

    def list(self, workspace_id: str, status: str | None = None, limit: int = 100) -> list[Incident]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM incidents WHERE workspace_id = ?"
        params: list[Any] = [workspace_id]
        if status in ("open", "resolved"):
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [Incident(**_row(r, _COLUMNS)) for r in self.db.fetchall(sql, tuple(params))]

    def open(self, inc: Incident, detail: dict[str, Any] | None = None) -> Incident:
        self._save(inc)
        self.add_event(inc.id, "opened", run_id=inc.first_run_id, detail=detail or {})
        return inc

    def recur(self, inc: Incident, *, run_id: str, error: str, failed_step: str, failure_class: str) -> Incident:
        changed = (failed_step and failed_step != inc.failed_step) or (failure_class != inc.failure_class)
        before = {"failed_step": inc.failed_step, "failure_class": inc.failure_class}
        inc.occurrences += 1
        inc.last_seen_at = _now()
        inc.last_run_id = run_id
        inc.error = error or inc.error
        inc.failed_step = failed_step or inc.failed_step
        inc.failure_class = failure_class
        self._save(inc)
        detail: dict[str, Any] = {"failed_step": inc.failed_step, "failure_class": failure_class}
        if changed:
            detail["was"] = before
        self.add_event(inc.id, "failed_again", run_id=run_id, detail=detail)
        return inc

    def resolve(self, inc: Incident, *, resolution: str, run_id: str = "", by: str = "") -> Incident:
        inc.status = "resolved"
        inc.resolved_at = _now()
        inc.resolution = resolution
        self._save(inc)
        self.add_event(inc.id, "resolved", run_id=run_id, detail={"resolution": resolution, "by": by})
        return inc

    def count_attempt(self, inc: Incident) -> Incident:
        inc.auto_attempts += 1
        return self._save(inc)

    # ── Events ───────────────────────────────────────────────────────

    def add_event(self, incident_id: str, kind: str, *, run_id: str = "", detail: dict[str, Any] | None = None) -> IncidentEvent:
        ev = IncidentEvent(incident_id=incident_id, kind=kind, run_id=run_id or "", detail=detail or {})
        self.db.execute_write(
            "INSERT INTO incident_events (id, incident_id, at, kind, run_id, detail) VALUES (?,?,?,?,?,?)",
            (ev.id, ev.incident_id, ev.at, ev.kind, ev.run_id, json.dumps(ev.detail, default=str)),
        )
        return ev

    def events(self, incident_id: str) -> list[IncidentEvent]:
        cols = ("id", "incident_id", "at", "kind", "run_id", "detail")
        out: list[IncidentEvent] = []
        for r in self.db.fetchall(
            "SELECT id, incident_id, at, kind, run_id, detail FROM incident_events "
            "WHERE incident_id = ? ORDER BY at, rowid",
            (incident_id,),
        ):
            d = _row(r, cols)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except (TypeError, ValueError):
                d["detail"] = {}
            out.append(IncidentEvent(**d))
        return out


__all__ = ["Incident", "IncidentEvent", "IncidentStore"]
