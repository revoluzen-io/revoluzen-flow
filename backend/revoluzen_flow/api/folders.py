"""Folders API — nested grouping of pipelines inside a project.

Delete semantics: removing a folder removes every descendant folder. The
workflows whose `folder_id` lives anywhere in that subtree are either kept
(``move_to_root``) or, with ``force``, sent to the Bin, which a published
pipeline blocks. Pipelines at the project root (folder_id=None) are not
affected by a folder delete.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from revoluzen_flow.auth.deps import require_auth, require_min_rank, current_workspace_id
from revoluzen_flow.folders.models import Folder, FolderCreate, FolderUpdate
from revoluzen_flow.ir.schema import PipelineStatus

router = APIRouter(prefix="/api/folders", tags=["folders"])

# Deleting a folder can delete the pipelines inside it (``force``), which takes
# the same rank as deleting a pipeline: developer and above. Without this an
# analyst or viewer, who get 403 on ``DELETE /workflows/{id}``, could send every
# pipeline in a folder to the Bin through here.
_AUTHOR = Depends(require_min_rank("developer"))


def _store():
    from revoluzen_flow.main import app_state
    return app_state["folder_store"]


def _project_store():
    from revoluzen_flow.main import app_state
    return app_state["project_store"]


def _workflow_store():
    from revoluzen_flow.main import app_state
    return app_state["store"]


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("workspace resolve failed")
        raise HTTPException(500, "workspace resolve failed") from exc


def _would_create_cycle(folder_id: str, candidate_parent: str | None, store) -> bool:
    """Return True if assigning candidate_parent to folder_id would create
    a cycle (parent chain eventually loops back to folder_id)."""
    if candidate_parent is None or candidate_parent == "":
        return False
    if candidate_parent == folder_id:
        return True
    visited: set[str] = set()
    cursor: str | None = candidate_parent
    while cursor:
        if cursor in visited:
            return True
        visited.add(cursor)
        if cursor == folder_id:
            return True
        parent = store.get(cursor)
        if not parent:
            return False
        cursor = parent.parent_folder_id or None
    return False


@router.get("")
async def list_folders(
    project_id: str = Query(..., description="Project to list folders for"),
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List all folders within a project (flat list — frontend builds the tree)."""
    folders = _store().list_for_project(project_id, workspace_id)
    return [f.model_dump(mode="json") for f in folders]


@router.post("")
async def create_folder(
    body: FolderCreate,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a folder at the root of a project.

    Revoluzen Flow uses 1-level folders by design: a project contains
    folders, a folder contains pipelines. The `parent_folder_id` column
    is kept on the data model for forward-compat (a future tier may
    allow nesting) but the API rejects non-null values today.
    """
    project = _project_store().get(body.project_id)
    if not project:
        raise HTTPException(404, f"Project not found: {body.project_id}")
    if project.workspace_id != workspace_id:
        raise HTTPException(403, "Project belongs to a different workspace")

    if body.parent_folder_id:
        raise HTTPException(
            400,
            "Sub-folders are not supported in Revoluzen Flow — folders are one level deep.",
        )

    folder = Folder(
        name=body.name,
        description=body.description,
        project_id=body.project_id,
        parent_folder_id=None,
        workspace_id=workspace_id,
        color=body.color,
        icon=body.icon,
    )
    _store().create(folder)
    return folder.model_dump(mode="json")


@router.get("/{folder_id}")
async def get_folder(
    folder_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    folder = _store().get(folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise HTTPException(404, f"Folder not found: {folder_id}")
    return folder.model_dump(mode="json")


@router.patch("/{folder_id}")
async def update_folder(
    folder_id: str,
    body: FolderUpdate,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    folder = _store().get(folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise HTTPException(404, f"Folder not found: {folder_id}")

    updates = body.model_dump(exclude_unset=True)
    if "parent_folder_id" in updates and updates["parent_folder_id"]:
        # 1-level OSS rule — folders cannot be moved under another folder.
        # Allowed: PATCH with parent_folder_id=null (no-op move to root).
        raise HTTPException(
            400,
            "Sub-folders are not supported in Revoluzen Flow — folders are one level deep.",
        )

    updated = _store().update(folder_id, updates)
    return updated.model_dump(mode="json")


def _compute_folder_delete_impact(folder_id: str, workspace_id: str):
    """Compute the impact of deleting a folder without doing it.

    Returns (folder, doomed_folder_ids, affected_workflow_ids, descendants,
    published) or raises 404 if the folder doesn't exist or isn't in this
    workspace. ``published`` lists ``{"id", "name"}`` for the affected
    pipelines whose status is published. Used by both the dry-run preview
    endpoint and the actual delete so the counting logic is identical
    between preview and execute.
    """
    folder = _store().get(folder_id)
    if not folder or folder.workspace_id != workspace_id:
        raise HTTPException(404, f"Folder not found: {folder_id}")

    descendants = _store().descendants(folder_id)
    doomed_folder_ids = {folder_id} | {d.id for d in descendants}

    wf_store = _workflow_store()
    affected_workflow_ids: list[str] = []
    published: list[dict] = []
    for wf_row in wf_store.list_all(workspace_id=workspace_id):
        wf_id = wf_row.get("id")
        if not wf_id:
            continue
        if wf_row.get("folder_id") in doomed_folder_ids:
            affected_workflow_ids.append(wf_id)
            # By its live version too: a published pipeline being tested
            # says "testing" for a moment and is still published.
            if wf_row.get("status") == PipelineStatus.PUBLISHED.value or wf_row.get("published_version") is not None:
                published.append({"id": wf_id, "name": wf_row.get("name") or wf_id})

    return folder, doomed_folder_ids, affected_workflow_ids, descendants, published


@router.get("/{folder_id}/delete-preview")
async def preview_folder_delete(
    folder_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Dry-run: show what a delete of this folder would do.

    2026-05-22 (audit E3): the previous delete endpoint was a cascade
    that silently nuked every pipeline under the folder. This preview
    lets the UI render "Deleting this folder will also delete 7
    pipelines: …" so the user can choose between Move-to-root vs
    Cascade-delete before the destructive op fires.

    Returns folder names + pipeline ids so the UI can show them in
    the confirmation modal. ``published_workflow_*`` name the pipelines
    that are published: ``force`` refuses to delete a folder while any
    exist (see delete_folder), so the UI says so before it asks. Does not
    mutate anything.
    """
    folder, doomed_folder_ids, affected_workflow_ids, descendants, published = (
        _compute_folder_delete_impact(folder_id, workspace_id)
    )
    return {
        "folder_id": folder_id,
        "folder_name": folder.name,
        "descendant_folder_ids": [d.id for d in descendants],
        "descendant_folder_names": [d.name for d in descendants],
        "doomed_folder_count": len(doomed_folder_ids),
        "affected_workflow_count": len(affected_workflow_ids),
        "affected_workflow_ids": affected_workflow_ids,
        "published_workflow_count": len(published),
        "published_workflow_ids": [p["id"] for p in published],
        "published_workflow_names": [p["name"] for p in published],
    }


@router.delete("/{folder_id}", dependencies=[_AUTHOR])
async def delete_folder(
    folder_id: str,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
    force: bool = False,
    move_to_root: bool = False,
):
    """Delete a folder.

    2026-05-22 (audit E3) — destructive cascade behaviour is now an
    explicit opt-in. Three modes:

      * ``move_to_root=true`` (recommended default): reparent every
        pipeline whose folder is being deleted to project-root
        (folder_id = None), then delete the folder + descendants.
        Pipelines are preserved.
      * ``force=true``: the cascade — folder + descendants deleted, and
        every pipeline in them moved to the Bin (restorable until its
        retention window ends; they come back in the project's top level).
        Needs developer rank, like deleting a pipeline. Refused (409,
        ``code: published_pipelines_in_folder``) while any pipeline in the
        subtree is published: a published pipeline cannot be deleted from
        the Pipelines page either (revoke it first), and a folder delete
        must not be a way around that.
      * Neither flag set + the folder contains pipelines: 409. The
        client must explicitly pick a mode. This avoids the silent
        data-loss the audit flagged.

    Empty folders (no pipelines anywhere in the subtree) delete
    without any flag — there's nothing to lose.
    """
    _folder, doomed_folder_ids, affected_workflow_ids, descendants, published = (
        _compute_folder_delete_impact(folder_id, workspace_id)
    )

    wf_store = _workflow_store()

    if affected_workflow_ids and not (force or move_to_root):
        raise HTTPException(
            409,
            {
                "message": (
                    "Folder contains pipelines — choose move_to_root=true "
                    "(reparent them to project root) or force=true (delete "
                    "them with the folder)."
                ),
                "affected_workflow_count": len(affected_workflow_ids),
                "affected_workflow_ids": affected_workflow_ids,
                "descendant_folder_count": len(descendants),
            },
        )

    if force and not move_to_root and published:
        shown = ", ".join(f'"{p["name"]}"' for p in published[:3])
        if len(published) > 3:
            shown += f" and {len(published) - 3} more"
        plural = "pipeline" if len(published) == 1 else "pipelines"
        raise HTTPException(
            409,
            {
                "code": "published_pipelines_in_folder",
                "message": (
                    f"This folder holds {len(published)} published {plural} ({shown}). "
                    "A published pipeline cannot be deleted: unpublish it first, or "
                    "use move_to_root=true to keep the pipelines in the project."
                ),
                "published_workflow_count": len(published),
                "published_workflow_ids": [p["id"] for p in published],
                "published_workflow_names": [p["name"] for p in published],
            },
        )

    moved_count = 0
    deleted_workflows = 0
    if move_to_root and affected_workflow_ids:
        # Reparent every affected workflow to project root.
        for wf_id in affected_workflow_ids:
            try:
                wf_v = wf_store.get(wf_id, workspace_id=workspace_id)
                if wf_v is None:
                    continue
                wf_v.workflow.folder_id = None
                wf_store.save(
                    wf_v.workflow,
                    change_summary="Reparented to project root on folder delete",
                )
                moved_count += 1
            except Exception:
                # Best-effort — keep going on individual failures
                # rather than aborting the whole operation. The folder
                # delete that follows is safe because the workflow
                # rows still exist (they just stayed in their old
                # folder); the user can retry.
                continue
    elif force and affected_workflow_ids:
        # A published pipeline outside this folder may run one inside it.
        from revoluzen_flow.ir.published_refs import published_calling_pipeline, refusal
        for wf_id in affected_workflow_ids:
            callers = [
                c for c in published_calling_pipeline(wf_store, workspace_id, wf_id)
                if c["id"] not in affected_workflow_ids
            ]
            if callers:
                inside = wf_store.get(wf_id, workspace_id=workspace_id)
                raise HTTPException(409, refusal("The pipeline", inside.workflow.name if inside else wf_id, callers))
        # Into the Bin, like any other delete: "delete them with the folder"
        # stays recoverable until the Bin's retention window ends. The
        # deleted_workflow_count in the response counts them as before.
        from revoluzen_flow.ir import workflow_bin
        from revoluzen_flow.main import app_state
        actor_id = getattr(user, "id", "") or ""
        actor_name = getattr(user, "name", "") or getattr(user, "email", "") or ""
        for wf_id in affected_workflow_ids:
            result = workflow_bin.delete_pipeline(
                wf_id,
                workspace_id,
                store=wf_store,
                schedule_store=app_state.get("schedule_store"),
                data_dir=app_state.get("data_dir"),
                actor_id=actor_id,
                actor_name=actor_name,
            )
            if result is not None:
                deleted_workflows += 1

    for fid in doomed_folder_ids:
        _store().delete(fid)

    return {
        "deleted_folder_id": folder_id,
        "deleted_descendant_count": len(descendants),
        "deleted_workflow_count": deleted_workflows,
        "moved_to_root_workflow_count": moved_count,
        "mode": "force" if force else ("move_to_root" if move_to_root else "empty"),
    }


class MoveWorkflowsRequest(BaseModel):
    workflow_ids: list[str]
    folder_id: str | None = None


@router.post("/move-workflows")
async def move_workflows(
    body: MoveWorkflowsRequest,
    user=Depends(require_auth),
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Move a batch of workflows to a folder (or to project root if
    folder_id is None). All workflows must already belong to the same
    project as the target folder."""
    target_project_id: str | None = None
    if body.folder_id:
        folder = _store().get(body.folder_id)
        if not folder or folder.workspace_id != workspace_id:
            raise HTTPException(404, f"Folder not found: {body.folder_id}")
        target_project_id = folder.project_id

    wf_store = _workflow_store()
    moved = 0
    for wf_id in body.workflow_ids:
        wf_version = wf_store.get(wf_id, workspace_id=workspace_id)
        if not wf_version:
            continue
        if target_project_id and wf_version.workflow.project_id != target_project_id:
            raise HTTPException(
                400,
                f"Workflow {wf_id} belongs to a different project than target folder",
            )
        wf_version.workflow.folder_id = body.folder_id
        wf_store.save(wf_version.workflow, change_summary=f"Moved to folder {body.folder_id or 'root'}")
        moved += 1
    return {"moved": moved}
