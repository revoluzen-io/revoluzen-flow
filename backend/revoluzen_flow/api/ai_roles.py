"""Copilot roles API (2026-11-11): which of Discover, Build, Test and Operate
the Copilot may do in this workspace. See ai/roles.py.

GET /api/v1/ai/roles  — any signed-in user: the switches, what each role
                        does and which tools it holds.
PUT /api/v1/ai/roles  — admins: switch roles on or off.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from revoluzen_flow.ai.roles import GENERAL_TOOLS, ROLE_INFO, ROLES, TOOL_ROLES, load_roles, save_roles
from revoluzen_flow.auth.deps import current_workspace_id, require_auth, require_min_rank

router = APIRouter(prefix="/api/v1/ai/roles", tags=["ai-roles"])


class RolesBody(BaseModel):
    discover: bool | None = None
    build: bool | None = None
    test: bool | None = None
    operate: bool | None = None


def _payload(workspace_id: str, roles: dict[str, bool]) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "roles": [
            {
                "key": r,
                "label": ROLE_INFO[r]["label"],
                "does": ROLE_INFO[r]["does"],
                "enabled": roles[r],
                "tools": sorted(t for t, role in TOOL_ROLES.items() if role == r),
            }
            for r in ROLES
        ],
        "always_on_tools": sorted(GENERAL_TOOLS),
    }


@router.get("")
def get_roles(_user=Depends(require_auth), workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    return _payload(workspace_id, load_roles(workspace_id))


@router.put("", dependencies=[Depends(require_min_rank("admin"))])
def put_roles(
    body: RolesBody,
    user=Depends(require_auth),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "Nothing to change: send discover, build, test or operate as true or false.")
    by = getattr(user, "email", None) or getattr(user, "id", None) or "admin"
    try:
        roles = save_roles(workspace_id, changes, by=str(by))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    return _payload(workspace_id, roles)
