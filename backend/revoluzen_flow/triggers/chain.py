"""Run a pipeline after another one finishes.

An after-pipeline trigger on B names A and an outcome: A succeeds, A fails, or
A finishes either way. When a run of A ends that way, B starts once, at its
published version.

Which runs of A count: the ones nobody is watching, the runs started by a
schedule, a file arriving, another pipeline finishing, or another application
through a published endpoint. A run someone starts by hand (the Run button, a
replay, a test, a backfill) does not start B, and neither does a run someone
stops. A run stopped by its time limit is a failure.

Loops are refused when a trigger is saved (:func:`find_loop`). Each run also
carries the list of pipelines that led to it, so a loop made some other way, by
two people saving at once say, stops at run time instead of running forever.

How it flows: a finished run calls :func:`on_run_finished`, which records an
event for each trigger waiting on it (``triggers/store.py``) and wakes the
scheduler. The scheduler starts the trigger as soon as it is free
(:func:`dispatch_events`); if it is still running, it runs once more when
that run ends. Recording first means a finish seen by one server is acted on
by whichever server holds the scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

from revoluzen_flow.scheduling.models import ScheduleType

logger = logging.getLogger("revoluzen_flow.triggers.chain")

#: The ``triggered_by`` values of runs that can start an after-pipeline trigger.
CHAIN_SOURCES = frozenset({"schedule", "file_arrival", "after_pipeline", "api", "webhook"})
#: The most pipelines one chain may run through, a backstop for loops.
MAX_CHAIN_LENGTH = 25

TRIGGERED_BY = ScheduleType.AFTER_PIPELINE.value


def outcome_matches(wanted: str, status: str) -> bool:
    """Whether a run that ended with ``status`` fires a trigger wanting ``wanted``."""
    wanted = (wanted or "success").lower()
    if status == "success":
        return wanted in ("success", "always")
    if status == "error":
        return wanted in ("failure", "always")
    return False


def find_loop(
    schedule_store: Any,
    workspace_id: str,
    upstream_id: str,
    downstream_id: str,
    ignore_trigger_id: str = "",
) -> list[str] | None:
    """The loop that "run downstream after upstream" would close, or None.

    Returned as pipeline ids in the order they would run, starting and ending
    with ``downstream_id``. ``ignore_trigger_id`` leaves out the trigger being
    edited, whose old setting is about to be replaced.
    """
    if not upstream_id or not downstream_id:
        return None
    if upstream_id == downstream_id:
        return [downstream_id, downstream_id]
    edges: dict[str, set[str]] = {}
    for row in schedule_store.list_all(workspace_id=workspace_id):
        if row.get("schedule_type") != TRIGGERED_BY or row.get("id") == ignore_trigger_id:
            continue
        before, after = row.get("after_workflow_id") or "", row.get("workflow_id") or ""
        if before and after:
            edges.setdefault(before, set()).add(after)
    # A way already leading from downstream back to upstream closes the loop.
    came_from: dict[str, str] = {downstream_id: ""}
    queue = [downstream_id]
    while queue:
        current = queue.pop(0)
        for nxt in sorted(edges.get(current, ())):
            if nxt in came_from:
                continue
            came_from[nxt] = current
            if nxt == upstream_id:
                path = [nxt]
                while came_from[path[-1]]:
                    path.append(came_from[path[-1]])
                return list(reversed(path)) + [downstream_id]
            queue.append(nxt)
    return None


def describe_loop(path: list[str], names: dict[str, str]) -> str:
    return " → ".join(f'"{names.get(pid, pid)}"' for pid in path)


def on_run_finished(
    app_state: dict,
    *,
    workflow_id: str,
    workspace_id: str,
    workflow_name: str,
    status: str,
    execution_id: str,
    triggered_by: str,
    run_metadata: dict | None = None,
) -> int:
    """Record that a run ended, for the triggers waiting on its pipeline.

    Returns how many triggers will start. Never raises: a run's own outcome
    must not depend on what comes after it.
    """
    try:
        trigger = (triggered_by or "").strip().lower()
        if trigger not in CHAIN_SOURCES or status not in ("success", "error") or not workflow_id:
            return 0
        schedule_store = app_state.get("schedule_store")
        if schedule_store is None:
            return 0
        waiting = [
            row for row in schedule_store.list_after(workflow_id, workspace_id or "default")
            if row.get("enabled") and outcome_matches(row.get("after_outcome") or "success", status)
        ]
        if not waiting:
            return 0
        from revoluzen_flow.triggers.store import trigger_store

        store = trigger_store(app_state)
        if store is None:
            return 0
        chain = [str(p) for p in ((run_metadata or {}).get("chain") or []) if p]
        for row in waiting:
            store.put_event(
                trigger_id=row["id"], workspace_id=workspace_id or "default", upstream_id=workflow_id,
                upstream_name=workflow_name or "", execution_id=execution_id or "", status=status,
                chain=chain + [workflow_id],
            )
        scheduler = app_state.get("scheduler")
        wake = getattr(scheduler, "wake_after_pipeline_triggers", None)
        if callable(wake):
            wake()
        return len(waiting)
    except Exception:  # noqa: BLE001
        logger.warning("Could not record the end of run %s for its after-pipeline triggers", execution_id, exc_info=True)
        return 0


def dispatch_events(scheduler: Any, app_state: dict) -> int:
    """Start the after-pipeline triggers whose pipelines have finished. Returns
    how many started. Called by the scheduler holding the lease."""
    from revoluzen_flow.triggers.gate import WAIT, check_trigger_can_run
    from revoluzen_flow.triggers.store import trigger_store

    store = trigger_store(app_state)
    schedule_store = app_state.get("schedule_store")
    if store is None or schedule_store is None:
        return 0
    started = 0
    for event in store.pending_events():
        try:
            schedule = schedule_store.get(event.trigger_id)
            row = schedule.model_dump(mode="json") if schedule is not None else None
            if (
                row is None
                or not row.get("enabled")
                or row.get("schedule_type") != TRIGGERED_BY
                or row.get("after_workflow_id") != event.upstream_id
                or (row.get("workspace_id") or "default") != event.workspace_id
                or not outcome_matches(row.get("after_outcome") or "success", event.status)
            ):
                store.drop_event(event.trigger_id)
                continue
            if scheduler.is_trigger_busy(event.trigger_id):
                continue  # runs once more when its current run ends
            upstream = event.upstream_name or event.upstream_id
            if row["workflow_id"] in event.chain or len(event.chain) >= MAX_CHAIN_LENGTH:
                store.drop_event(event.trigger_id)
                schedule_store.record_check(
                    event.trigger_id,
                    f'Not started after "{upstream}": that would run the same pipelines round in a loop.',
                )
                logger.warning("After-pipeline trigger %s refused a loop: %s", event.trigger_id, event.chain)
                continue
            gate = check_trigger_can_run(app_state, row)
            if not gate.ok:
                if gate.kind != WAIT:
                    store.drop_event(event.trigger_id)
                schedule_store.record_check(event.trigger_id, gate.message)
                continue
            if not store.take_event(event):
                continue
            outcome = {"success": "succeeded", "error": "failed"}.get(event.status, event.status)
            options = {
                "triggered_by": TRIGGERED_BY,
                "run_metadata": {
                    "after_pipeline": {
                        "workflow_id": event.upstream_id,
                        "workflow_name": event.upstream_name,
                        "execution_id": event.execution_id,
                        "status": event.status,
                    },
                    "chain": event.chain,
                },
            }
            if scheduler.start_trigger_run(row, **options):
                started += 1
                schedule_store.record_check(event.trigger_id, f'Started because "{upstream}" {outcome}.')
            else:
                # Turned away at capacity: put it back for the next tick.
                store.put_event(
                    trigger_id=event.trigger_id, workspace_id=event.workspace_id, upstream_id=event.upstream_id,
                    upstream_name=event.upstream_name, execution_id=event.execution_id, status=event.status,
                    chain=event.chain,
                )
        except Exception:  # noqa: BLE001 — one bad trigger never holds up the others
            logger.warning("After-pipeline trigger %s could not be started", event.trigger_id, exc_info=True)
    return started
