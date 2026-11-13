"""The pipeline Bin: everything around the rows.

``WorkflowStore`` (``ir/versioning.py``) moves a pipeline's version rows into
the Bin tables and back. This module does the rest of a delete, a restore and
a purge, so the API, the folder delete and the daily clean-up all behave the
same way:

* **Delete** moves the pipeline to the Bin. As before the Bin existed, its
  schedules are removed (left behind they would fire and record "workflow not
  found" as a failed run every time), its step cache is cleared and its CDC
  replication slots are dropped (a slot left behind pins WAL on the source
  server). A snapshot of the schedules goes into the Bin entry so a restore
  can put them back. ``permanent=True`` skips the Bin.
* **Restore** brings the pipeline back as a **draft**, with its schedules
  back **paused**, so nothing starts running by surprise. A name that has been
  taken meanwhile gets a ``(2)`` suffix; a project or folder that no longer
  exists is replaced by Default / the project root. Every correction is
  reported back in ``notes``.
* **Purge** removes a Bin entry for good: one at a time, all at once, or
  every entry older than the retention window
  (``REVOLUZEN_FLOW_BIN_RETENTION_DAYS``).

A replication slot dropped at delete time is created again by the CDC source
on its next run; changes made while the pipeline was in the Bin are not
replayed.

Stores are passed in rather than fetched from application state, so the
behaviour can be tested without starting the app.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from revoluzen_flow.common.unique_name import ensure_unique_name
from revoluzen_flow.ir.versioning import WorkflowIdInUse, WorkflowStore, published_pin

logger = logging.getLogger(__name__)


class NotInBin(LookupError):
    """There is no such pipeline in the Bin (in that workspace)."""


class PublishedPipeline(RuntimeError):
    """A published pipeline cannot be deleted. It has to be unpublished first."""


class IdInUse(RuntimeError):
    """A live pipeline already has the id of the one being restored."""


def purge_at(deleted_at: str, retention_days: int) -> str | None:
    """When an entry deleted at ``deleted_at`` is removed for good.

    None when automatic removal is off (retention below 1 day) or the
    timestamp cannot be read.
    """
    if retention_days < 1:
        return None
    try:
        when = datetime.fromisoformat(deleted_at)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when + timedelta(days=retention_days)).isoformat()


def public_entry(entry: dict, retention_days: int) -> dict:
    """A Bin entry as the API returns it: without the schedule snapshot, and
    with the moment it will be removed for good."""
    out = {k: v for k, v in entry.items() if k != "schedules"}
    out["purge_at"] = purge_at(entry.get("deleted_at", ""), retention_days)
    return out


def _audit(action: str, workflow_id: str, workspace_id: str, actor: str, **details: Any) -> None:
    """One audit event. ``audit_action`` never raises."""
    try:
        from revoluzen_flow.audit import audit_action
        audit_action(
            action=action,
            resource_type="workflow",
            resource_id=workflow_id,
            actor=actor or "system",
            workspace_id=workspace_id or "default",
            details=details,
        )
    except Exception:  # noqa: BLE001
        logger.debug("audit emission failed for %s", action, exc_info=True)


def _remove_side_effects(workflow, workflow_id: str, workspace_id: str, *, schedule_store, data_dir) -> None:
    """What a delete takes away besides the version rows. Best-effort, each
    part on its own: one failing must not undo or block the others."""
    # Left behind, a schedule keeps firing and records "workflow not found"
    # as a failed run every time.
    try:
        if schedule_store is not None:
            for sched in schedule_store.list_by_workflow(workflow_id, workspace_id=workspace_id):
                schedule_store.delete(sched["id"], workspace_id=workspace_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not remove schedules of deleted workflow %s", workflow_id, exc_info=True)
    # Purge the on-disk step cache so stale parquet files don't linger in the
    # data dir after the workflow is gone.
    try:
        if data_dir:
            from revoluzen_flow.engine.step_cache import StepCache
            StepCache(data_dir, workflow_id).clear()
    except Exception:  # noqa: BLE001
        pass
    # Drop any Postgres logical replication slots the workflow's CDC sources
    # created: an orphaned slot pins WAL on the source server until its disk
    # fills.
    try:
        from revoluzen_flow.connectors.cdc import cleanup_workflow_cdc_slots
        cleanup_workflow_cdc_slots(workflow)
    except Exception:  # noqa: BLE001
        pass


def delete_pipeline(
    workflow_id: str,
    workspace_id: str,
    *,
    store: WorkflowStore,
    schedule_store=None,
    data_dir: str | None = None,
    permanent: bool = False,
    actor_id: str = "",
    actor_name: str = "",
) -> dict | None:
    """Delete a pipeline: into the Bin, or for good with ``permanent=True``.

    Returns None when there is no such pipeline in that workspace; otherwise
    ``{"binned": bool, "name": str, "entry": <Bin entry> | None}``. Raises
    :class:`PublishedPipeline` if it is published: every delete comes through
    here, so a published pipeline has no way round being unpublished first.
    """
    existing = store.get(workflow_id, workspace_id=workspace_id)
    if existing is None:
        return None
    wf = existing.workflow
    if published_pin(existing) is not None:
        raise PublishedPipeline(wf.name or workflow_id)
    actor = actor_name or actor_id
    entry = None
    if permanent:
        if not store.delete(workflow_id):
            return None
    else:
        snapshot: list[dict] = []
        try:
            if schedule_store is not None:
                snapshot = list(schedule_store.list_by_workflow(workflow_id, workspace_id=workspace_id))
        except Exception:  # noqa: BLE001
            logger.warning("Could not read schedules of workflow %s before binning it", workflow_id, exc_info=True)
        entry = store.trash(
            workflow_id,
            workspace_id,
            deleted_by_id=actor_id,
            deleted_by_name=actor_name,
            schedules=snapshot,
        )
        if entry is None:
            return None
    _remove_side_effects(wf, workflow_id, workspace_id, schedule_store=schedule_store, data_dir=data_dir)
    status = getattr(wf.status, "value", wf.status)
    _audit(
        "workflow.delete", workflow_id, workspace_id, actor,
        name=wf.name or "(unnamed)", status=status, mode="permanent" if permanent else "bin",
    )
    return {"binned": not permanent, "name": wf.name, "entry": entry}


def restore_pipeline(
    workflow_id: str,
    workspace_id: str,
    *,
    store: WorkflowStore,
    schedule_store=None,
    project_lookup: Callable[[str], Any] | None = None,
    folder_lookup: Callable[[str], Any] | None = None,
    actor_id: str = "",
    actor_name: str = "",
) -> dict:
    """Restore a binned pipeline as a draft.

    Raises :class:`NotInBin` when it is not in the Bin (in that workspace)
    and :class:`IdInUse` when a live pipeline already has its id. Returns
    ``{"id", "name", "renamed_from", "status", "project_id", "folder_id",
    "schedules_restored", "notes"}``; ``notes`` lists, in plain words,
    everything that was corrected on the way back.
    """
    entry = store.get_trash_entry(workflow_id, workspace_id, include_schedules=True)
    if entry is None:
        raise NotInBin(workflow_id)
    notes: list[str] = []
    entry_ws = entry["workspace_id"]

    # A name taken in the meantime gets a "(2)" suffix, as on every create.
    taken = {w.get("name", "") for w in store.list_all(workspace_id=entry_ws)}
    name = ensure_unique_name(entry["name"], taken)
    renamed_from = entry["name"] if name != entry["name"] else None
    if renamed_from:
        notes.append(f'The name "{renamed_from}" is in use again, so it was restored as "{name}".')

    # A project that is gone (or archived) is replaced by Default.
    project_id = entry["project_id"] or "default"
    if project_id != "default" and project_lookup is not None:
        project = project_lookup(project_id)
        if project is None or getattr(project, "status", "active") == "archived":
            project_id = "default"
            notes.append("Its project no longer exists or is archived, so it was restored to Default.")

    # A folder that is gone, or no longer in that project, becomes the root.
    folder_id = entry["folder_id"]
    if folder_id:
        folder = folder_lookup(folder_id) if folder_lookup is not None else None
        in_place = (
            folder is not None
            and getattr(folder, "project_id", None) == project_id
            and (getattr(folder, "workspace_id", entry_ws) or entry_ws) == entry_ws
        )
        if not in_place:
            folder_id = None
            notes.append("Its folder no longer exists, so it was restored to the project root.")

    try:
        store.restore_from_trash(
            workflow_id,
            workspace_id,
            name=name,
            project_id=project_id,
            folder_id=folder_id,
            restored_by=actor_name or actor_id or "user",
        )
    except WorkflowIdInUse as exc:
        raise IdInUse(workflow_id) from exc

    # Schedules come back paused: the pipeline is a draft again, and a run
    # starting on its own after a restore would be a surprise.
    restored_schedules = 0
    if schedule_store is not None:
        from revoluzen_flow.scheduling.models import Schedule
        for data in entry.get("schedules") or []:
            try:
                schedule = Schedule(**{**data, "enabled": False, "project_id": project_id, "next_run_at": None})
                if schedule_store.get(schedule.id) is None:
                    schedule_store.create(schedule)
                    restored_schedules += 1
            except Exception:  # noqa: BLE001
                logger.warning("Could not restore a schedule of workflow %s", workflow_id, exc_info=True)
    if restored_schedules:
        plural = "schedule" if restored_schedules == 1 else "schedules"
        notes.append(f"{restored_schedules} {plural} came back paused.")

    _audit(
        "workflow.restore", workflow_id, entry_ws, actor_name or actor_id,
        name=name, renamed_from=renamed_from, project_id=project_id,
    )
    return {
        "id": workflow_id,
        "name": name,
        "renamed_from": renamed_from,
        "status": "draft",
        "project_id": project_id,
        "folder_id": folder_id,
        "schedules_restored": restored_schedules,
        "notes": notes,
    }


def purge_pipeline(workflow_id: str, workspace_id: str, *, store: WorkflowStore, actor: str = "") -> dict | None:
    """Remove one pipeline from the Bin for good. Returns its entry, or None
    when it was not in the Bin (in that workspace)."""
    entry = store.purge_trash(workflow_id, workspace_id)
    if entry is not None:
        _audit("workflow.purge", workflow_id, entry["workspace_id"], actor, name=entry["name"], reason="deleted from the Bin")
    return entry


def empty_bin(workspace_id: str, *, store: WorkflowStore, actor: str = "") -> int:
    """Remove everything in a workspace's Bin for good. Returns how many."""
    entries = store.purge_trash_all(workspace_id)
    for entry in entries:
        _audit("workflow.purge", entry["id"], entry["workspace_id"], actor, name=entry["name"], reason="Bin emptied")
    return len(entries)


def purge_expired(*, store: WorkflowStore, retention_days: int, now: datetime | None = None) -> list[dict]:
    """Remove every Bin entry deleted more than ``retention_days`` ago.

    Does nothing when ``retention_days`` is below 1 (automatic removal off).
    Returns the entries removed.
    """
    if retention_days < 1:
        return []
    moment = now or datetime.now(timezone.utc)
    entries = store.purge_trash_older_than((moment - timedelta(days=retention_days)).isoformat())
    for entry in entries:
        _audit(
            "workflow.purge", entry["id"], entry["workspace_id"], "system",
            name=entry["name"], reason=f"in the Bin for more than {retention_days} days",
        )
    if entries:
        logger.info("Bin: removed %d pipeline(s) older than %d days", len(entries), retention_days)
    return entries
