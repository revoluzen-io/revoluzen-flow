"""Pipeline scheduling API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from revoluzen_flow.auth.deps import current_workspace_id, require_auth, require_min_rank
from revoluzen_flow.scheduling.models import (
    FILE_LOCATIONS,
    Schedule,
    ScheduleCreate,
    ScheduleType,
    ScheduleUpdate,
    is_event_schedule,
)
from revoluzen_flow.security.step_secret_routes import MaskStepSecretsRoute

# 2026-05-30 (Track S P1): router-level require_auth blocks anonymous;
# write endpoints add require_min_rank("data_engineer") so analyst/viewer
# can read schedule status but can't create/edit/delete schedules.
_AUTH = Depends(require_auth)
_AUTHOR = Depends(require_min_rank("developer"))

router = APIRouter(
    prefix="/api/schedules",
    tags=["schedules"],
    dependencies=[_AUTH],
    route_class=MaskStepSecretsRoute,
)


def _safe_workspace_id(request: Request) -> str:
    """Wrap current_workspace_id so dep failures surface as readable HTTP errors."""
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def get_store():
    from revoluzen_flow.main import app_state
    return app_state["schedule_store"]


def _workflow_in_workspace(workflow_id: str, workspace_id: str) -> bool:
    """Check that the referenced workflow belongs to the caller's workspace.

    Prevents a cross-tenant attack where a user in workspace A submits
    a schedule body with a workflow_id that lives in workspace B — the
    schedule itself would be stamped with workspace A's id, but the
    scheduler would still execute a workflow from B.

    The workflow store is registered in app_state under the key
    ``"store"`` (matching every other API module — see contracts.py,
    execution.py, planner.py). An earlier draft of this file used
    ``"workflow_store"`` which silently KeyError'd inside the broad
    except, causing every schedule create to 404 with "Workflow not
    found" — the user-visible regression that prompted this fix.
    """
    try:
        from revoluzen_flow.main import app_state
        workflow_store = app_state["store"]
        return workflow_store.get(workflow_id, workspace_id=workspace_id) is not None
    except Exception:
        # Conservative: if the workflow store lookup fails for any
        # reason, treat it as "not in workspace" so the create is
        # rejected. A missing workflow always blocks schedule creation.
        return False


def _check_event_trigger(fields: dict[str, Any], workspace_id: str, schedule_id: str = "") -> None:
    """Refuse a file-arrival or after-pipeline trigger that could never run.

    ``fields`` is the whole trigger as it would be saved. A switched-off
    trigger may be saved half done; switching it on runs every check.
    """
    from revoluzen_flow.ir.versioning import PublishedVersionMissing, published_pin
    from revoluzen_flow.main import app_state

    kind = fields.get("schedule_type")
    kind = getattr(kind, "value", kind)
    store = app_state["store"]
    workflow_id = fields.get("workflow_id") or ""
    latest = store.get(workflow_id, workspace_id=workspace_id)
    if latest is None:
        raise HTTPException(404, "Workflow not found")
    name = latest.workflow.name or workflow_id
    enabled = bool(fields.get("enabled", True))

    if kind == ScheduleType.FILE_ARRIVAL.value:
        location = (fields.get("file_location") or "").strip().lower()
        if location not in FILE_LOCATIONS:
            raise HTTPException(400, "Choose where to watch: a folder on this server, SFTP, a Windows file share, S3, Google Cloud Storage or Azure Blob Storage.")
        if location == "local" and not (fields.get("file_folder") or "").strip():
            raise HTTPException(400, "Choose the folder on this server to watch.")
        if location != "local":
            from revoluzen_flow.triggers.file_listing import ListingError, connection_config
            try:
                connection_config(app_state, fields.get("file_connection_id") or "", workspace_id, location)
            except ListingError as exc:
                raise HTTPException(400, str(exc)) from None
    elif kind == ScheduleType.AFTER_PIPELINE.value:
        upstream_id = fields.get("after_workflow_id") or ""
        if not upstream_id:
            raise HTTPException(400, "Choose the pipeline this one runs after.")
        upstream = store.get(upstream_id, workspace_id=workspace_id)
        if upstream is None:
            raise HTTPException(404, "The pipeline to run after was not found in this workspace.")
        from revoluzen_flow.triggers.chain import describe_loop, find_loop
        loop = find_loop(get_store(), workspace_id, upstream_id, workflow_id, ignore_trigger_id=schedule_id)
        if loop:
            names = {workflow_id: name, upstream_id: upstream.workflow.name or upstream_id}
            for pid in loop:
                if pid not in names:
                    other = store.get(pid, workspace_id=workspace_id)
                    names[pid] = other.workflow.name if other else pid
            raise HTTPException(
                409,
                {
                    "code": "trigger_loop",
                    "message": (
                        f"This would start the pipelines in a loop: {describe_loop(loop, names)}. "
                        "Remove one of these triggers first."
                    ),
                    "loop": loop,
                },
            )
    else:
        return

    if not enabled:
        return
    if published_pin(latest) is None:
        raise HTTPException(
            400,
            f'Publish "{name}" first. A trigger only ever runs the published version of a pipeline.',
        )
    parameter = (fields.get("file_parameter") or "").strip() if kind == ScheduleType.FILE_ARRIVAL.value else ""
    if parameter:
        try:
            live = store.live_version(workflow_id, workspace_id=workspace_id)
        except PublishedVersionMissing as exc:
            raise HTTPException(409, str(exc)) from None
        declared = {p.name for p in (live.workflow.parameters or [])} if live else set()
        if parameter not in declared:
            raise HTTPException(
                400,
                f'The published version of "{name}" has no parameter named "{parameter}" to receive the '
                "file's path. Add it in the Editor and publish, or choose another.",
            )


@router.get("", include_in_schema=False)
@router.get("/")
async def list_schedules(
    workflow_id: str | None = None,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List schedules scoped to the caller's workspace."""
    store = get_store()
    if workflow_id:
        return store.list_by_workflow(workflow_id, workspace_id=workspace_id)
    if project_id:
        return store.list_by_project(project_id, workspace_id=workspace_id)
    return store.list_all(workspace_id=workspace_id)


# 2026-05-30 (P7): trailing-slash alias (same rationale as workflows.py).
@router.post("", dependencies=[_AUTHOR], include_in_schema=False)
@router.post("/", dependencies=[_AUTHOR])
async def create_schedule(
    body: ScheduleCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new schedule in the caller's workspace.

    Rejects the create if the referenced workflow doesn't exist in
    the caller's workspace — blocks cross-tenant workflow pinning.

    2026-05-26 — Also rejects when the workflow contains an
    append_risky or external sink and the caller didn't pass
    ``acknowledge_side_effects=true``. Scheduled runs fire on a
    cadence; an append sink will multiply rows on every tick and an
    external sink (email/webhook/Kafka) will re-fire the side effect.
    """
    if not _workflow_in_workspace(body.workflow_id, workspace_id):
        raise HTTPException(404, "Workflow not found")
    if is_event_schedule(body.model_dump()):
        _check_event_trigger(body.model_dump(mode="json"), workspace_id)

    # Sink-safety guardrail — mirrors the backfill check in
    # api/backfills.py. We reach into the workflow IR once to find any
    # sink the idempotency classifier flagged as unsafe; the caller can
    # explicitly opt in with `acknowledge_side_effects=true` if they
    # understand the consequences (e.g. a "send daily digest" pipeline
    # legitimately wants email_sink to fire on every tick).
    from revoluzen_flow.main import app_state as _app_state
    from revoluzen_flow.backfills.idempotency import find_unsafe_sinks
    wf_store = _app_state.get("store")
    versioned = wf_store.get(body.workflow_id, workspace_id=workspace_id) if wf_store else None
    workflow = versioned.workflow if versioned else None
    if workflow is not None:
        unsafe = find_unsafe_sinks(workflow.steps)
        if unsafe and not body.acknowledge_side_effects:
            raise HTTPException(
                400,
                {
                    "code": "unsafe_for_schedule",
                    "message": (
                        "Pipeline contains sinks that are not safe to "
                        "re-run on every schedule tick (append-mode "
                        "writes duplicate rows, external sinks re-fire "
                        "the side effect). Pass "
                        "acknowledge_side_effects=true if this is "
                        "intentional (e.g. a digest email sink)."
                    ),
                    "unsafe_sinks": unsafe,
                },
            )

    store = get_store()
    schedule = Schedule(**body.model_dump(), workspace_id=workspace_id)
    created = store.create(schedule)
    return created.model_dump(mode="json")


@router.put("/by-workflow/{workflow_id}/default", dependencies=[_AUTHOR])
async def upsert_default_schedule(
    workflow_id: str,
    body: ScheduleCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Idempotent upsert of the "default schedule" for a workflow.

    2026-05-22 (audit D3): SaveDialog used to POST /schedules every
    time the dialog saved a pipeline. Repeat saves piled up duplicate
    schedule rows for the same workflow — confusing for operators and
    expensive at run time (the scheduler would fire the workflow once
    per duplicate). This endpoint gives the dialog a single
    fire-and-forget call that creates the row on the first save and
    updates it in place on subsequent saves.

    Behaviour:
      * If no schedule exists for ``workflow_id`` in this workspace,
        create one. (Same validation as POST /schedules.)
      * If one or more schedules exist, update the OLDEST row (the
        first one created) in place with the request body. Returns
        the updated row.
      * Multiple manually-created schedules on the same workflow are
        not touched — only the chronologically first row is owned by
        the "default" upsert lane.

    The Toolbar / Pipelines page can still POST /schedules to make
    explicit secondary schedules — this endpoint is the "save
    dialog's default schedule" carve-out, not a hard replace.
    """
    if not _workflow_in_workspace(workflow_id, workspace_id):
        raise HTTPException(404, "Workflow not found")
    if body.workflow_id != workflow_id:
        # Body's workflow_id is informational; the URL is authoritative.
        # Reject mismatch so a client can't smuggle a different
        # workflow_id through the upsert.
        raise HTTPException(400, "workflow_id in body must match URL")

    store = get_store()
    existing = store.list_by_workflow(workflow_id, workspace_id=workspace_id)
    if existing:
        # Pick the oldest by created_at if available, else first.
        try:
            target = sorted(existing, key=lambda s: getattr(s, "created_at", None) or "")[0]
        except Exception:
            target = existing[0]
        target_id = target.id if hasattr(target, "id") else (target.get("id") if isinstance(target, dict) else None)
        if not target_id:
            raise HTTPException(500, "Could not resolve default schedule id")
        if is_event_schedule(body.model_dump()):
            _check_event_trigger(body.model_dump(mode="json"), workspace_id, schedule_id=target_id)
        updates = body.model_dump(exclude={"workflow_id", "acknowledge_side_effects"})
        updated = store.update(target_id, updates, workspace_id=workspace_id)
        if not updated:
            raise HTTPException(404, "Schedule not found")
        return updated.model_dump(mode="json")

    if is_event_schedule(body.model_dump()):
        _check_event_trigger(body.model_dump(mode="json"), workspace_id)
    schedule = Schedule(**body.model_dump(), workspace_id=workspace_id)
    created = store.create(schedule)
    return created.model_dump(mode="json")


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    schedule = store.get(schedule_id, workspace_id=workspace_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    return schedule.model_dump(mode="json")


@router.put("/{schedule_id}", dependencies=[_AUTHOR])
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    current = store.get(schedule_id, workspace_id=workspace_id)
    if not current:
        raise HTTPException(404, "Schedule not found")
    updates = body.model_dump(exclude_none=True)
    merged = {**current.model_dump(mode="json"), **body.model_dump(mode="json", exclude_none=True)}
    if is_event_schedule(merged):
        _check_event_trigger(merged, workspace_id, schedule_id=schedule_id)
    schedule = store.update(schedule_id, updates, workspace_id=workspace_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    return schedule.model_dump(mode="json")


@router.delete("/{schedule_id}", dependencies=[_AUTHOR])
async def delete_schedule(
    schedule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    store = get_store()
    if not store.delete(schedule_id, workspace_id=workspace_id):
        raise HTTPException(404, "Schedule not found")
    _forget_trigger(schedule_id)
    return {"deleted": True}


def _forget_trigger(schedule_id: str) -> None:
    """Drop what an event trigger remembered. Best-effort: the scheduler also
    clears what deleted triggers left, every hour."""
    try:
        from revoluzen_flow.main import app_state
        from revoluzen_flow.triggers.store import trigger_store

        tstore = trigger_store(app_state)
        if tstore is not None:
            tstore.forget_trigger(schedule_id)
        scheduler = app_state.get("scheduler")
        if scheduler is not None and getattr(scheduler, "_file_watcher_obj", None) is not None:
            scheduler.file_watcher.forget(schedule_id)
    except Exception:  # noqa: BLE001
        pass


class FolderCheck(BaseModel):
    file_location: str
    file_connection_id: str = ""
    file_folder: str = ""
    file_pattern: str = "*"


@router.post("/check-folder", dependencies=[_AUTHOR])
async def check_folder(
    body: FolderCheck,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """What a file-arrival trigger with these settings would see right now.

    Lists the folder once and starts nothing, so the Schedules screen can show
    whether the connection, folder and pattern are right before saving.
    """
    import anyio

    from revoluzen_flow.main import app_state
    from revoluzen_flow.triggers.file_listing import ListingError
    from revoluzen_flow.triggers.file_watch import preview_folder

    if (body.file_location or "").strip().lower() not in FILE_LOCATIONS:
        raise HTTPException(400, f"file_location must be one of: {', '.join(FILE_LOCATIONS)}")
    try:
        return await anyio.to_thread.run_sync(
            lambda: preview_folder(app_state, workspace_id, body.model_dump()),
        )
    except ListingError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/{schedule_id}/files")
async def trigger_files(
    schedule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """The files a file-arrival trigger has started runs for, newest first."""
    from revoluzen_flow.main import app_state
    from revoluzen_flow.triggers.store import trigger_store

    schedule = get_store().get(schedule_id, workspace_id=workspace_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    tstore = trigger_store(app_state)
    return tstore.recent_files(schedule_id) if tstore is not None else []


@router.post("/{schedule_id}/toggle", dependencies=[_AUTHOR])
async def toggle_schedule(
    schedule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Enable/disable a schedule — scoped to workspace."""
    store = get_store()
    schedule = store.get(schedule_id, workspace_id=workspace_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    if not schedule.enabled and is_event_schedule(schedule):
        _check_event_trigger({**schedule.model_dump(mode="json"), "enabled": True}, workspace_id, schedule_id)
    # Persist the toggle — the original code only mutated the in-memory
    # copy, which is a pre-existing bug we're fixing in passing. The
    # store.update path also enforces the workspace boundary on write.
    updated = store.update(
        schedule_id,
        {"enabled": not schedule.enabled},
        workspace_id=workspace_id,
    )
    if not updated:
        raise HTTPException(404, "Schedule not found")
    return {"enabled": updated.enabled}


@router.post("/{schedule_id}/run-now", dependencies=[_AUTHOR])
async def run_schedule_now(
    schedule_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Manually trigger a scheduled pipeline to run immediately — workspace-scoped."""
    store = get_store()
    schedule = store.get(schedule_id, workspace_id=workspace_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found")

    from revoluzen_flow.main import app_state
    scheduler = app_state["scheduler"]

    sched_dict = schedule.model_dump(mode="json")
    if sched_dict.get("schedule_type") == ScheduleType.FILE_ARRIVAL.value:
        # A file trigger runs for a file, so "now" means look for one now.
        if not schedule.enabled:
            raise HTTPException(400, "Turn the trigger on first.")
        if not scheduler.check_file_trigger_now(sched_dict):
            raise HTTPException(409, "The folder is being checked already, or this server does not run triggers.")
        return {
            "status": "checking",
            "message": "Looking in the folder now. New files start their runs as usual.",
            "schedule_id": schedule_id,
        }
    if sched_dict.get("schedule_type") == ScheduleType.AFTER_PIPELINE.value:
        raise HTTPException(
            400,
            "This trigger starts the pipeline after another one finishes. Run it from the Pipelines page instead.",
        )
    sched_dict["enabled"] = True  # Force enabled for manual run
    scheduler._execute_schedule(schedule_id, sched_dict)

    return {
        "status": "started",
        "message": f"Pipeline '{schedule.workflow_id}' execution started",
        "schedule_id": schedule_id,
    }
