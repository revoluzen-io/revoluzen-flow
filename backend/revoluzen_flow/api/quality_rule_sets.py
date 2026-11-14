"""Shared quality rule sets API (2026-11-11).

Named lists of Data Quality rules that many pipelines use: a Data Quality
step picks sets by id (``rule_sets``) and runs their rules as they are at run
time. Reading needs a signed-in user of the workspace; creating, changing and
deleting need developer rank, as changing a pipeline does. A set a saved
pipeline uses cannot be deleted. See quality_rules/store.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from revoluzen_flow.auth.deps import current_user_optional, current_workspace_id, require_min_rank
from revoluzen_flow.quality_rules.store import RuleSetProblem, rule_set_users

router = APIRouter(
    prefix="/api/quality-rule-sets",
    tags=["quality-rule-sets"],
    dependencies=[Depends(current_workspace_id)],
)

_AUTHOR = [Depends(require_min_rank("developer"))]


def _state() -> dict:
    from revoluzen_flow.main import app_state  # type: ignore
    return app_state


def _store():
    store = _state().get("quality_rule_set_store")
    if store is None:
        raise HTTPException(503, "rule set store unavailable")
    return store


def _who(user: Any) -> str:
    return (getattr(user, "name", "") or getattr(user, "email", "") or "").strip()


def _users(workspace_id: str, set_id: str) -> list[dict[str, Any]]:
    return rule_set_users(_state().get("store"), workspace_id, set_id)


def _summary(rs, used_by: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = rs.model_dump(mode="json", exclude={"history"})
    out["rule_count"] = len(rs.rules)
    if used_by is not None:
        out["used_by"] = used_by
    return out


def _not_saved(exc: RuleSetProblem) -> HTTPException:
    return HTTPException(400, {
        "message": "The rule set was not saved: " + "; ".join(exc.problems) + ".",
        "problems": exc.problems,
    })


class RuleSetBody(BaseModel):
    name: str
    description: str = ""
    rules: list[dict[str, Any]] = Field(default_factory=list)


class RuleSetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    rules: list[dict[str, Any]]


@router.get("")
async def list_rule_sets(workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    """The workspace's rule sets, by name, each with how many pipelines use it."""
    out = []
    for rs in _store().list(workspace_id):
        users = _users(workspace_id, rs.id)
        item = _summary(rs)
        item["used_by_count"] = len(users)
        item["published_count"] = sum(1 for u in users if u["published"])
        out.append(item)
    return {"rule_sets": out}


@router.get("/{set_id}")
async def get_rule_set(set_id: str, workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    """One set: its rules, its earlier versions and the pipelines that use it."""
    rs = _store().get(set_id, workspace_id=workspace_id)
    if rs is None:
        raise HTTPException(404, "rule set not found")
    out = _summary(rs, _users(workspace_id, rs.id))
    out["history"] = rs.history
    return out


@router.post("", dependencies=_AUTHOR, status_code=201)
async def create_rule_set(
    body: RuleSetBody,
    workspace_id: str = Depends(current_workspace_id),
    user=Depends(current_user_optional),
) -> dict[str, Any]:
    try:
        rs = _store().create(workspace_id, body.name, body.rules, description=body.description, by=_who(user))
    except RuleSetProblem as exc:
        raise _not_saved(exc) from None
    return _summary(rs, [])


@router.put("/{set_id}", dependencies=_AUTHOR)
async def update_rule_set(
    set_id: str,
    body: RuleSetUpdate,
    workspace_id: str = Depends(current_workspace_id),
    user=Depends(current_user_optional),
) -> dict[str, Any]:
    """Change a set. Every pipeline that uses it checks the new rules from its
    next run; the response lists them so the screen can say so."""
    store = _store()
    rs = store.get(set_id, workspace_id=workspace_id)
    if rs is None:
        raise HTTPException(404, "rule set not found")
    try:
        rs = store.update(rs, rules=body.rules, name=body.name, description=body.description, by=_who(user))
    except RuleSetProblem as exc:
        raise _not_saved(exc) from None
    return _summary(rs, _users(workspace_id, rs.id))


@router.delete("/{set_id}", dependencies=_AUTHOR)
async def delete_rule_set(set_id: str, workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    """Delete a set no saved pipeline uses."""
    store = _store()
    rs = store.get(set_id, workspace_id=workspace_id)
    if rs is None:
        raise HTTPException(404, "rule set not found")
    users = _users(workspace_id, rs.id)
    if users:
        names = ", ".join(u["name"] for u in users[:5]) + (f" and {len(users) - 5} more" if len(users) > 5 else "")
        raise HTTPException(409, {
            "message": f'"{rs.name}" is used by {len(users)} pipeline{"s" if len(users) != 1 else ""}: {names}. '
                       "Remove it from their Data Quality steps first.",
            "used_by": users,
        })
    store.delete(rs.id)
    return {"deleted": rs.id}
