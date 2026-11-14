"""Open, repeat and resolve incidents from run outcomes; resume by policy.

Called once per finished run from ``notifications/run_events.py``, the one
place every terminal outcome already passes (manual and API runs, schedules,
event triggers). Only unattended runs count: a run someone started from the
editor may be a sample of a draft, so neither its failure nor its success
says anything about the pipeline that is live.

* An unattended failure opens an incident for the pipeline, or adds an
  occurrence to the open one.
* The next unattended success resolves it, saying how (a later run, or a
  resume made by the policy).
* With fixes by policy on (``policy.py``), a failure whose class a resume
  can fix is resumed from its checkpoint after a delay, a limited number of
  times. The resumed run's own outcome then resolves or repeats the
  incident like any other run.

Never raises: a bookkeeping problem must not touch the run it describes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, Callable

from .policy import load_policy
from .store import Incident, IncidentStore

logger = logging.getLogger("revoluzen_flow.incidents")

#: Runs nobody was watching: the live pipeline ran for real. The same set the
#: after-pipeline triggers treat as production (triggers/chain.CHAIN_SOURCES),
#: plus backfills and the policy's own resumes.
UNATTENDED = frozenset({
    "schedule", "file_arrival", "after_pipeline", "api", "webhook", "backfill", "auto_recover",
})

AUTO_RECOVER = "auto_recover"


def is_unattended(triggered_by: str | None) -> bool:
    return (triggered_by or "").strip().lower() in UNATTENDED


def _scrub(text: str, app_state: dict | None) -> str:
    """An error as it can be shown: the install's own paths taken out."""
    out = (text or "").strip()
    data_dir = str((app_state or {}).get("data_dir") or "")
    if data_dir:
        out = out.replace(data_dir, "<data>").replace(data_dir.replace("\\", "/"), "<data>")
    return out[:2000]


def _classify(message: str) -> str:
    try:
        from revoluzen_flow.engine.failure_class import classify_error
        return classify_error(message).value
    except Exception:  # noqa: BLE001
        return "unknown"


def record_run_outcome(
    app_state: dict | None,
    *,
    workflow_id: str,
    workflow_name: str,
    execution_id: str,
    status: str,
    workspace_id: str = "default",
    triggered_by: str = "",
    error_message: str = "",
    failed_step: str = "",
) -> Incident | None:
    """Fold one finished run into its pipeline's incident. Never raises."""
    try:
        store: IncidentStore | None = (app_state or {}).get("incident_store")
        if store is None or not workflow_id or not is_unattended(triggered_by):
            return None
        s = (status or "").lower()
        workspace_id = workspace_id or "default"
        current = store.open_for_workflow(workflow_id, workspace_id)
        trigger = (triggered_by or "").strip().lower()

        if s == "success":
            if current is None:
                return None
            if trigger == AUTO_RECOVER:
                resolution = "Resumed from its checkpoint by the auto-recover policy, and that run succeeded."
            else:
                resolution = f"A later {trigger.replace('_', ' ')} run succeeded."
            return store.resolve(current, resolution=resolution, run_id=execution_id, by=trigger)

        if s not in ("error", "failed"):
            return None
        error = _scrub(error_message, app_state)
        failure_class = _classify(error_message)
        if current is None:
            inc = store.open(
                Incident(
                    workspace_id=workspace_id,
                    workflow_id=workflow_id,
                    workflow_name=workflow_name or workflow_id,
                    failure_class=failure_class,
                    failed_step=failed_step or "",
                    error=error,
                    first_run_id=execution_id,
                    last_run_id=execution_id,
                ),
                detail={"triggered_by": trigger, "failure_class": failure_class, "failed_step": failed_step},
            )
        else:
            inc = store.recur(
                current, run_id=execution_id, error=error,
                failed_step=failed_step or "", failure_class=failure_class,
            )
        _maybe_auto_recover(app_state or {}, store, inc, execution_id)
        return inc
    except Exception:  # noqa: BLE001 — see module docstring
        logger.warning("incident bookkeeping failed for run %s", execution_id, exc_info=True)
        return None


# ── Fixes by policy ─────────────────────────────────────────────────────

def _start_timer(delay_seconds: float, fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
    """Run ``fn`` later, off the run's thread. Tests replace this."""
    timer = threading.Timer(delay_seconds, fn, kwargs=kwargs)
    timer.daemon = True
    timer.name = f"revoluzen_flow-auto-recover-{kwargs.get('incident_id', '')[:8]}"
    timer.start()


def _maybe_auto_recover(app_state: dict, store: IncidentStore, inc: Incident, run_id: str) -> None:
    policy = load_policy(app_state)
    if not policy.enabled or not run_id:
        return
    if inc.failure_class not in policy.failure_classes:
        store.add_event(inc.id, "auto_recover_skipped", run_id=run_id, detail={
            "reason": f"{inc.failure_class.replace('_', ' ')} failures are not resumed by the policy",
        })
        return
    if inc.auto_attempts >= policy.max_attempts:
        store.add_event(inc.id, "auto_recover_skipped", run_id=run_id, detail={
            "reason": f"the policy's {policy.max_attempts} attempt(s) are used up; a person needs to look",
        })
        return
    store.count_attempt(inc)
    store.add_event(inc.id, "auto_resume_scheduled", run_id=run_id, detail={
        "attempt": inc.auto_attempts, "of": policy.max_attempts, "in_seconds": policy.delay_seconds,
    })
    _start_timer(policy.delay_seconds, _auto_resume, {
        "app_state": app_state, "incident_id": inc.id, "workflow_id": inc.workflow_id,
        "workspace_id": inc.workspace_id, "run_id": run_id,
    })


def _auto_resume(*, app_state: dict, incident_id: str, workflow_id: str, workspace_id: str, run_id: str) -> None:
    """Resume a failed run from its checkpoint, as the policy scheduled.

    Goes through the same path as the Resume button (``run_saved_workflow``),
    so the run is admitted to the pool, recorded, notified, and its outcome
    comes back to :func:`record_run_outcome` like any other run's.
    """
    store: IncidentStore | None = app_state.get("incident_store")
    if store is None:
        return
    try:
        inc = store.get(incident_id)
        if inc is None or inc.status != "open":
            if inc is not None:
                store.add_event(incident_id, "auto_resume_skipped", run_id=run_id, detail={
                    "reason": "the incident was resolved before the resume was due",
                })
            return
        if inc.last_run_id != run_id:
            store.add_event(incident_id, "auto_resume_skipped", run_id=run_id, detail={
                "reason": "a newer run failed since; its outcome decides the next attempt",
            })
            return
        wf_store = app_state.get("store")
        version = wf_store.live_version(workflow_id, workspace_id=workspace_id) if wf_store else None
        if version is None:
            store.add_event(incident_id, "auto_resume_failed", run_id=run_id, detail={
                "error": "the pipeline has no live version to resume",
            })
            return
        store.add_event(incident_id, "auto_resume_started", run_id=run_id)
        from revoluzen_flow.api.execution import run_saved_workflow

        result = asyncio.run(run_saved_workflow(
            version,
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            full_run=True,
            resume_from_run_id=run_id,
            triggered_by=AUTO_RECOVER,
            run_user_id="system",
        ))
        status = result.get("status") if isinstance(result, dict) else None
        new_run = result.get("execution_id") if isinstance(result, dict) else None
        store.add_event(incident_id, "auto_resume_finished", run_id=str(new_run or ""), detail={
            "status": status, "resumed_from": run_id,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto-recover resume failed for incident %s", incident_id, exc_info=True)
        try:
            store.add_event(incident_id, "auto_resume_failed", run_id=run_id, detail={
                "error": _scrub(str(exc), app_state),
            })
        except Exception:  # noqa: BLE001
            pass


# ── Context, for whoever reads an incident ──────────────────────────────

def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def incident_context(app_state: dict, inc: Incident) -> dict[str, Any]:
    """What an operator asks next: when did it last work, what changed
    since, and what does the failed step depend on and feed."""
    out: dict[str, Any] = {"last_success": None, "changes_since": [], "upstream": [], "downstream": []}
    opened = _parse_time(inc.opened_at)

    executions = app_state.get("execution_store")
    last_ok_at: datetime | None = None
    if executions is not None:
        try:
            for run in executions.list_by_workflow(inc.workflow_id, limit=200, workspace_id=inc.workspace_id):
                started = _parse_time(run.get("started_at"))
                if run.get("status") == "success" and started and (opened is None or started < opened):
                    last_ok_at = started
                    out["last_success"] = {
                        "run_id": run.get("id"), "started_at": run.get("started_at"),
                        "triggered_by": run.get("triggered_by"),
                    }
                    break
        except Exception:  # noqa: BLE001
            logger.debug("incident context: run history unavailable", exc_info=True)

    wf_store = app_state.get("store")
    if wf_store is not None:
        try:
            versions = wf_store.get_versions(inc.workflow_id)
            changed = [
                v for v in versions
                if last_ok_at is None or ((_parse_time(v.get("created_at")) or last_ok_at) > last_ok_at)
            ]
            out["changes_since"] = [
                {k: v.get(k) for k in ("version", "created_by", "created_at", "change_summary", "kind", "live")}
                for v in changed[-10:]
            ]
        except Exception:  # noqa: BLE001
            logger.debug("incident context: versions unavailable", exc_info=True)
        try:
            version = wf_store.get(inc.workflow_id, workspace_id=inc.workspace_id)
            wf = version.workflow if version else None
        except Exception:  # noqa: BLE001
            wf = None
        if wf is not None and inc.failed_step:
            labels = {s.id: (s.label or s.id) for s in wf.steps}
            step = next((s for s in wf.steps if (s.label or s.id) == inc.failed_step or s.id == inc.failed_step), None)
            if step is not None:
                ups: dict[str, list[str]] = {}
                downs: dict[str, list[str]] = {}
                for c in wf.connections:
                    ups.setdefault(c.to_step, []).append(c.from_step)
                    downs.setdefault(c.from_step, []).append(c.to_step)

                def walk(start: str, edges: dict[str, list[str]]) -> list[str]:
                    seen: list[str] = []
                    queue = list(edges.get(start, []))
                    while queue:
                        sid = queue.pop(0)
                        if sid in seen:
                            continue
                        seen.append(sid)
                        queue.extend(edges.get(sid, []))
                    return [labels.get(s, s) for s in seen]

                out["upstream"] = walk(step.id, ups)
                out["downstream"] = walk(step.id, downs)
    return out


__all__ = ["AUTO_RECOVER", "UNATTENDED", "incident_context", "is_unattended", "record_run_outcome"]
