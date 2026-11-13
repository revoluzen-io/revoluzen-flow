"""A pipeline's overlap setting: what to do when it is asked to run while a run
of it is still going.

Set in the editor's Save with details > Execution, stored as
``metadata.execution_settings.enable_overlap_detection`` and ``overlap_policy``:

* ``parallel`` (or detection off): start another run alongside.
* ``skip`` / ``queue``: do not start one now.
* ``cancel_previous``: stop the running one, then start.

The Run button and published endpoints (``api/execution.run_saved_workflow``)
and the event triggers (``triggers/``) all ask here, so one setting means the
same thing whatever starts the run. A run is "going" when the run history says
so, which is true from its first moment for unattended runs, as they write
their record before they start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

CANCEL_REASON = "Stopped because a newer run started (overlap policy: Cancel Previous)"


@dataclass(frozen=True)
class OverlapCheck:
    """A run that should not start now, and why."""

    policy: str
    running_execution_id: str
    running_started_at: Any
    message: str


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def overlap_settings(workflow: Any) -> tuple[bool, str]:
    settings = (getattr(workflow, "metadata", None) or {}).get("execution_settings") or {}
    return bool(settings.get("enable_overlap_detection", False)), str(settings.get("overlap_policy") or "parallel")


def running_runs(exe_store: Any, workflow_id: str, workspace_id: str, exclude: str = "") -> list[Any]:
    return [
        row for row in exe_store.list_by_workflow(workflow_id, workspace_id=workspace_id)
        if _field(row, "status") == "running" and (_field(row, "id") or "") != exclude
    ]


def check_overlap(workflow: Any, exe_store: Any, workflow_id: str, workspace_id: str, exclude: str = "") -> OverlapCheck | None:
    """None when a new run may start. Otherwise what the setting says instead.

    Cancel Previous stops the running runs here, through their cancel tokens,
    and marks them stopped, then answers None so the new run goes ahead.
    """
    enabled, policy = overlap_settings(workflow)
    if not enabled or policy == "parallel" or exe_store is None:
        return None
    running = running_runs(exe_store, workflow_id, workspace_id, exclude=exclude)
    if not running:
        return None
    first = running[0]
    first_id = _field(first, "id") or ""
    first_started = _field(first, "started_at") or ""
    if policy == "skip":
        return OverlapCheck(policy, first_id, first_started, f"Skipped: previous execution still running (started {first_started})")
    if policy == "queue":
        return OverlapCheck(policy, first_id, first_started, "Queued: waiting for running execution to complete")
    if policy == "cancel_previous":
        # It used to relabel the row "cancelled" while that run carried on.
        from revoluzen_flow.engine.cancellation import cancel_run

        for row in running:
            run_id = _field(row, "id") or ""
            if not run_id:
                continue
            cancel_run(run_id, CANCEL_REASON, create=True)
            exe_store.update(
                run_id,
                {"status": "cancelled", "completed_at": datetime.now(timezone.utc), "error_message": f"{CANCEL_REASON}."},
                workspace_id=workspace_id,
            )
    return None
