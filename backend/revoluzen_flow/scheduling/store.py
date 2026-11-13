"""SQLite-backed schedule store."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from revoluzen_flow.ir.step_secrets import keep_stored_parameter_values, seal_parameter_values

from .models import Schedule, ScheduleType, upgrade_legacy_row


def _rows(rows: list[dict]) -> list[dict]:
    """Rows as the rest of the app expects them, with pre-2026-11 event rows
    read as the trigger they described (see ``upgrade_legacy_row``)."""
    return [upgrade_legacy_row(row) for row in rows]


class ScheduleStore:
    """Schedule store backed by SQLite."""

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db):
        self._db = db

    def _declared_parameters(self, workflow_id: str) -> list:
        """The pipeline's parameter declarations, which say which values are
        secret. Empty if it cannot be read; the rule by name still applies."""
        try:
            row = self._db.fetchone(
                "SELECT json_extract(data, '$.workflow.parameters') AS parameters "
                "FROM workflow_versions WHERE workflow_id = ? ORDER BY version DESC LIMIT 1",
                (workflow_id,),
            )
            return json.loads(row["parameters"] or "[]") if row else []
        except Exception:  # noqa: BLE001 - never block a schedule on this lookup
            return []

    def _save(self, schedule: Schedule):
        # A value for a secret parameter is kept encrypted, and a value already
        # encrypted is left as it is. The run decrypts it when it resolves its
        # parameters (engine/parameters.py).
        if schedule.parameter_values:
            schedule.parameter_values = seal_parameter_values(
                schedule.parameter_values, self._declared_parameters(schedule.workflow_id),
            )
        data = schedule.model_dump(mode="json")
        self._db.insert_json(
            "schedules", schedule.id, data,
            workflow_id=schedule.workflow_id,
            project_id=schedule.project_id,
            workspace_id=schedule.workspace_id or "default",
            enabled=1 if schedule.enabled else 0,
            created_at=schedule.created_at.isoformat(),
            updated_at=schedule.updated_at.isoformat(),
        )

    def create(self, schedule: Schedule) -> Schedule:
        self._save(schedule)
        return schedule

    def get(self, schedule_id: str, workspace_id: str | None = None) -> Schedule | None:
        """Get a schedule — returns None across workspace boundary."""
        data = self._db.get_json("schedules", schedule_id)
        if data is None:
            return None
        if workspace_id is not None:
            sched_ws = data.get("workspace_id") or "default"
            if sched_ws != workspace_id:
                return None
        return Schedule(**data)

    def list_all(self, workspace_id: str | None = None) -> list[dict]:
        if workspace_id is not None:
            return _rows(self._db.list_json(
                "schedules", "workspace_id = ?", (workspace_id,)
            ))
        return _rows(self._db.list_json("schedules"))

    def list_by_workflow(
        self,
        workflow_id: str,
        workspace_id: str | None = None,
    ) -> list[dict]:
        if workspace_id is not None:
            return _rows(self._db.list_json(
                "schedules",
                "workflow_id = ? AND workspace_id = ?",
                (workflow_id, workspace_id),
            ))
        return _rows(self._db.list_json("schedules", "workflow_id = ?", (workflow_id,)))

    def list_by_project(
        self,
        project_id: str,
        workspace_id: str | None = None,
    ) -> list[dict]:
        if workspace_id is not None:
            return _rows(self._db.list_json(
                "schedules",
                "project_id = ? AND workspace_id = ?",
                (project_id, workspace_id),
            ))
        return _rows(self._db.list_json("schedules", "project_id = ?", (project_id,)))

    def list_after(self, workflow_id: str, workspace_id: str) -> list[dict]:
        """The after-pipeline triggers waiting on ``workflow_id`` in one workspace."""
        return [
            row for row in self.list_all(workspace_id=workspace_id)
            if row.get("schedule_type") == ScheduleType.AFTER_PIPELINE.value
            and row.get("after_workflow_id") == workflow_id
        ]

    def record_check(self, schedule_id: str, message: str, checked_at: datetime | None = None) -> None:
        """Note when an event trigger last looked and what it found.

        A single UPDATE of two keys, so it cannot undo an edit someone saves on
        the Schedules screen while a check is under way, as a read-then-save of
        the whole row would.
        """
        when = (checked_at or datetime.now(timezone.utc)).isoformat()
        self._db.execute_write(
            "UPDATE schedules SET data = json_set(data, '$.last_checked_at', ?, '$.status_message', ?) "
            "WHERE id = ?",
            (when, (message or "")[:500], schedule_id),
        )

    def update(
        self,
        schedule_id: str,
        updates: dict,
        workspace_id: str | None = None,
    ) -> Schedule | None:
        schedule = self.get(schedule_id, workspace_id=workspace_id)
        if not schedule:
            return None
        for key, value in updates.items():
            # Schedule workspace membership is immutable via update body
            # — same rule as connections/credentials.
            if key == "workspace_id":
                continue
            if key == "parameter_values" and value:
                value = keep_stored_parameter_values(
                    value, schedule.parameter_values, self._declared_parameters(schedule.workflow_id),
                )
            if value is not None and hasattr(schedule, key):
                setattr(schedule, key, value)
        schedule.updated_at = datetime.now(timezone.utc)
        self._save(schedule)
        return schedule

    def delete(self, schedule_id: str, workspace_id: str | None = None) -> bool:
        if workspace_id is not None:
            if not self.get(schedule_id, workspace_id=workspace_id):
                return False
        return self._db.delete_row("schedules", schedule_id)

    def record_run(self, schedule_id: str, status: str, fired_at: datetime | None = None) -> None:
        """System-level — called by the scheduler loop, unscoped by design.

        ``fired_at`` is when the run started; ``last_run_at`` is when it ended.
        """
        schedule = self.get(schedule_id)
        if schedule:
            schedule.last_run_at = datetime.now(timezone.utc)
            schedule.last_run_status = status
            schedule.run_count += 1
            if fired_at is not None:
                schedule.last_fired_at = fired_at
            self._save(schedule)
