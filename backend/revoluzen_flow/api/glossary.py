"""Business glossary API (2026-11-11): the workspace's own terms and metrics,
which the Copilot reads when a request uses them. See glossary/store.py.

Reading needs a signed-in user of the workspace; adding, changing and
deleting need developer rank, as for a pipeline.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from revoluzen_flow.auth.deps import current_user_optional, current_workspace_id, require_min_rank
from revoluzen_flow.glossary.store import GlossaryProblem, match_terms

router = APIRouter(
    prefix="/api/glossary",
    tags=["glossary"],
    dependencies=[Depends(current_workspace_id)],
)

_AUTHOR = [Depends(require_min_rank("developer"))]


def _store():
    from revoluzen_flow.main import app_state  # type: ignore
    store = app_state.get("glossary_store")
    if store is None:
        raise HTTPException(503, "glossary unavailable")
    return store


def _who(user: Any) -> str:
    return (getattr(user, "name", "") or getattr(user, "email", "") or "").strip()


def _bad(exc: GlossaryProblem) -> HTTPException:
    return HTTPException(400, {"message": "Not saved: " + "; ".join(exc.problems) + ".", "problems": exc.problems})


class TermBody(BaseModel):
    name: str
    kind: str = "term"
    definition: str
    synonyms: list[str] = Field(default_factory=list)
    sql: str = ""
    where: str = ""
    owner: str = ""


class TermUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    definition: str | None = None
    synonyms: list[str] | None = None
    sql: str | None = None
    where: str | None = None
    owner: str | None = None


@router.get("")
async def list_terms(q: str = "", workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    """Every entry, by name. With ``q``: the entries that text mentions, as
    the Copilot would find them, or whose name contains it."""
    terms = _store().list(workspace_id)
    if q.strip():
        mentioned = match_terms(terms, q, limit=50)
        contains = [t for t in terms if q.strip().lower() in t.name.lower() and t not in mentioned]
        terms = mentioned + contains
    return {"terms": [t.model_dump(mode="json") for t in terms]}


@router.post("", dependencies=_AUTHOR, status_code=201)
async def add_term(body: TermBody, workspace_id: str = Depends(current_workspace_id), user=Depends(current_user_optional)) -> dict[str, Any]:
    try:
        return _store().save(workspace_id, body.model_dump(), by=_who(user)).model_dump(mode="json")
    except GlossaryProblem as exc:
        raise _bad(exc) from None


@router.put("/{term_id}", dependencies=_AUTHOR)
async def change_term(
    term_id: str, body: TermUpdate,
    workspace_id: str = Depends(current_workspace_id), user=Depends(current_user_optional),
) -> dict[str, Any]:
    store = _store()
    term = store.get(term_id, workspace_id=workspace_id)
    if term is None:
        raise HTTPException(404, "term not found")
    try:
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        return store.save(workspace_id, fields, by=_who(user), term=term).model_dump(mode="json")
    except GlossaryProblem as exc:
        raise _bad(exc) from None


@router.delete("/{term_id}", dependencies=_AUTHOR)
async def delete_term(term_id: str, workspace_id: str = Depends(current_workspace_id)) -> dict[str, Any]:
    store = _store()
    term = store.get(term_id, workspace_id=workspace_id)
    if term is None:
        raise HTTPException(404, "term not found")
    store.delete(term.id)
    return {"deleted": term.id}
