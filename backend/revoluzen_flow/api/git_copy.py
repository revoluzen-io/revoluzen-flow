"""Settings and status for the copy of published pipelines kept in Git.

Admins set it up. Anyone signed in can see how a pipeline's copy went, because
the Pipelines page shows it.
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from revoluzen_flow.auth.deps import current_workspace_id, require_auth, require_min_rank
from revoluzen_flow.gitcopy import service
from revoluzen_flow.gitcopy.client import GitFailed, GitRepo, git_program, git_version
from revoluzen_flow.gitcopy.settings import (
    GitSettingsError,
    merged_settings,
    public_settings,
    write_settings,
)

router = APIRouter(prefix="/api/git-copy", tags=["git-copy"])

_ADMIN = Depends(require_min_rank("admin"))
_AUTHOR = Depends(require_min_rank("developer"))
_SIGNED_IN = Depends(require_auth)


class GitCopySettingsBody(BaseModel):
    enabled: bool = False
    repo_url: str = Field(default="", max_length=1000)
    branch: str = Field(default="main", max_length=200)
    folder: str = Field(default="flow-pipelines", max_length=300)
    username: str = Field(default="", max_length=200)
    #: Blank keeps the saved token.
    token: str = Field(default="", max_length=4000)
    clear_token: bool = False


def _describe() -> dict[str, Any]:
    version = git_version()
    return {
        **public_settings(service.current_settings()),
        "git_installed": git_program() is not None,
        "git_version": ".".join(str(part) for part in version) if version else "",
    }


def _actor(request: Request) -> tuple[str, str]:
    from revoluzen_flow.auth.deps import current_user_optional

    user = current_user_optional(request)
    if user is None:
        return "", ""
    return user.name or user.email or user.id, user.email or ""


@router.get("/settings", dependencies=[_ADMIN])
async def get_settings() -> dict[str, Any]:
    return _describe()


@router.put("/settings", dependencies=[_ADMIN])
async def save_settings(body: GitCopySettingsBody, request: Request) -> dict[str, Any]:
    from revoluzen_flow.main import app_state

    try:
        merged = merged_settings(service.current_settings(), body.model_dump())
    except GitSettingsError as exc:
        raise HTTPException(400, str(exc)) from None
    write_settings(app_state["db"], app_state.get("encryptor"), merged, _actor(request)[0])
    return _describe()


@router.post("/test", dependencies=[_ADMIN])
async def test_settings(body: GitCopySettingsBody) -> dict[str, Any]:
    """Sign in to the repository with what is in the form, without saving it."""
    try:
        candidate = merged_settings(service.current_settings(), {**body.model_dump(), "enabled": True})
    except GitSettingsError as exc:
        return {"ok": False, "message": str(exc)}
    try:
        exists = await anyio.to_thread.run_sync(lambda: GitRepo(None, candidate).check_access())
    except GitFailed as exc:
        return {"ok": False, "message": str(exc)}
    return {
        "ok": True,
        "branch_exists": exists,
        "message": (
            f'Connected. Flow can use the branch "{candidate.branch}".'
            if exists else
            f'Connected. The branch "{candidate.branch}" does not exist yet, so Flow will start it with the first copy.'
        ),
    }


@router.post("/save-all", dependencies=[_ADMIN])
async def save_all(request: Request, workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    """Copy everything already published, for a repository connected later."""
    if not service.current_settings().ready:
        raise HTTPException(400, "Set up and switch on the Git copy first.")
    name, email = _actor(request)
    count = await anyio.to_thread.run_sync(lambda: service.save_all_published(workspace_id, name, email))
    return {"queued": count}


@router.get("/recent", dependencies=[_SIGNED_IN])
async def recent(workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    rows = service.queue().recent(workspace_id)
    return {"copies": [service.public_row(row) for row in rows]}


@router.get("/pipelines/{workflow_id}", dependencies=[_SIGNED_IN])
async def pipeline_copy(workflow_id: str, workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    """How the newest copy of this pipeline went."""
    settings = service.current_settings()
    return {
        "enabled": settings.ready,
        "latest": service.public_row(service.queue().latest_for(workflow_id, workspace_id)),
    }


@router.post("/pipelines/{workflow_id}/retry", dependencies=[_AUTHOR])
async def retry_copy(workflow_id: str, workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    latest = service.queue().latest_for(workflow_id, workspace_id)
    if latest is None or latest["status"] != "failed":
        raise HTTPException(400, "There is no failed copy of this pipeline to try again.")
    service.retry(latest["id"])
    return {"status": "queued"}
