"""Collaboration API — comments, threads, @mentions on pipelines."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from revoluzen_flow.auth.deps import ADMIN_ROLES, current_user_optional, current_workspace_id

# 2026-10-23 — router-level posture gate. An unauthenticated probe posted a
# comment onto an arbitrary workflow id and read it straight back; the row
# was stored with author_id "anonymous" and served to every other user of
# the workflow. Comments are user-visible content, so that is a spam and
# content-injection surface, not just a missing check.
router = APIRouter(
    prefix="/api/collaboration",
    tags=["collaboration"],
    dependencies=[Depends(current_workspace_id)],
)

# 2026-11-13 — two more gaps from the pen test:
#   * The author came from ``request.state.user_id``, which nothing in the
#     app ever sets, so every comment was by "anonymous" and the author-only
#     edit/delete check passed for everyone. The author now comes from the
#     session.
#   * Comments were looked up by workflow or comment id alone, so a member
#     of workspace B read and edited workspace A's comments. Every route now
#     checks that the workflow belongs to the caller's workspace.


def _get_store(request: Request):
    # Stage 2: feature flag guard. Gated by REVOLUZEN_FLOW_ENABLE_COLLABORATION.
    from revoluzen_flow.feature_flags import require
    from revoluzen_flow.main import app_state
    require("collaboration")
    return app_state["collaboration_store"]


def _caller(request: Request) -> tuple[str, str, bool]:
    """(author_id, author_name, is_admin) for the signed-in caller.

    An anonymous caller only reaches this on a loopback local-mode install
    (the router dependency refuses them in server mode).
    """
    user = current_user_optional(request)
    if user is None:
        return "anonymous", "anonymous", False
    name = getattr(user, "name", None) or getattr(user, "email", None) or user.id
    return user.id, name, getattr(user, "role", "") in ADMIN_ROLES


def _require_workflow_in_workspace(workflow_id: str, request: Request) -> None:
    from revoluzen_flow.state import get_workflow_store
    workspace_id = current_workspace_id(request)
    if not get_workflow_store().get(workflow_id, workspace_id=workspace_id):
        raise HTTPException(404, "Workflow not found")


def _comment_in_workspace(store, comment_id: str, request: Request) -> dict:
    comment = store.get_comment(comment_id)
    if not comment:
        raise HTTPException(404, "Comment not found")
    _require_workflow_in_workspace(comment.get("workflow_id", ""), request)
    return comment


class CommentRequest(BaseModel):
    body: str
    step_id: str = ""
    parent_id: str = ""


class UpdateCommentRequest(BaseModel):
    body: str


@router.get("/{workflow_id}/comments")
async def list_comments(
    workflow_id: str,
    step_id: str | None = Query(None),
    include_resolved: bool = Query(True),
    request: Request = None,
):
    """List all comments for a workflow (optionally filtered by node)."""
    _require_workflow_in_workspace(workflow_id, request)
    store = _get_store(request)
    return store.list_comments(workflow_id, step_id=step_id, include_resolved=include_resolved)


@router.get("/{workflow_id}/stats")
async def comment_stats(workflow_id: str, request: Request):
    """Get comment counts and unresolved threads for a workflow."""
    _require_workflow_in_workspace(workflow_id, request)
    store = _get_store(request)
    return store.get_stats(workflow_id)


@router.post("/{workflow_id}/comments")
async def add_comment(
    workflow_id: str,
    body: CommentRequest,
    request: Request,
):
    """Add a comment to a workflow or node."""
    _require_workflow_in_workspace(workflow_id, request)
    store = _get_store(request)
    user_id, user_name, _ = _caller(request)
    comment = store.add_comment(
        workflow_id=workflow_id, author_id=user_id, body=body.body,
        step_id=body.step_id, parent_id=body.parent_id, author_name=user_name,
    )
    # Send notifications for @mentions
    if comment and comment.get("mentions"):
        _notify_mentions(request, workflow_id, comment)
    return comment


@router.put("/comments/{comment_id}")
async def update_comment(
    comment_id: str,
    body: UpdateCommentRequest,
    request: Request,
):
    """Update a comment (author only)."""
    store = _get_store(request)
    _comment_in_workspace(store, comment_id, request)
    user_id, _, _ = _caller(request)
    result = store.update_comment(comment_id, body.body, user_id)
    if not result:
        raise HTTPException(403, "Cannot edit this comment")
    return result


@router.post("/comments/{comment_id}/resolve")
async def resolve_comment(comment_id: str, request: Request):
    """Mark a comment thread as resolved."""
    store = _get_store(request)
    _comment_in_workspace(store, comment_id, request)
    user_id, _, _ = _caller(request)
    return store.resolve_comment(comment_id, user_id)


@router.post("/comments/{comment_id}/unresolve")
async def unresolve_comment(comment_id: str, request: Request):
    """Reopen a resolved comment thread."""
    store = _get_store(request)
    _comment_in_workspace(store, comment_id, request)
    return store.unresolve_comment(comment_id)


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, request: Request):
    """Delete a comment and its replies (author or admin)."""
    store = _get_store(request)
    comment = _comment_in_workspace(store, comment_id, request)
    user_id, _, is_admin = _caller(request)
    if comment.get("author_id") != user_id and not is_admin:
        raise HTTPException(403, "Cannot delete this comment")
    store.delete_comment(comment_id, comment.get("author_id", ""))
    return {"status": "deleted"}


@router.get("/mentions/me")
async def my_mentions(request: Request):
    """Get all comments that @mention the current user."""
    store = _get_store(request)
    user_id, _, _ = _caller(request)
    return store.get_mentions(user_id)

def _notify_mentions(request: Request, workflow_id: str, comment: dict):
    """Send notifications for @mentions in a comment."""
    try:
        from revoluzen_flow.main import app_state
        notification_store = app_state.get("notification_store")
        if not notification_store:
            return
        for mentioned_user in comment.get("mentions", []):
            notification_store.create(
                user_id=mentioned_user,
                title=f"@{comment.get('author_name', 'Someone')} mentioned you",
                body=comment["body"][:200],
                type="mention",
                link=f"#editor?workflow={workflow_id}&comment={comment['id']}",
            )
    except Exception:
        pass  # Non-critical — don't fail the comment creation
