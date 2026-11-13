"""Incidents API — what is failing, what changed, and fixes by policy.

Incidents are opened and resolved by run outcomes (incidents/service.py);
this exposes them, lets a person resolve one or add a note, and lets an
admin set the auto-recover policy (incidents/policy.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from revoluzen_flow.auth.deps import current_user_optional, current_workspace_id, require_min_rank
from revoluzen_flow.incidents.policy import ALLOWED_CLASSES, AutoRecoverPolicy, load_policy, save_policy

router = APIRouter(
    prefix="/api/incidents",
    tags=["incidents"],
    dependencies=[Depends(current_workspace_id)],
)


def _state() -> dict:
    from revoluzen_flow.main import app_state  # type: ignore
    return app_state


def _store():
    store = _state().get("incident_store")
    if store is None:
        raise HTTPException(503, "incident store unavailable")
    return store


def _who(user: Any) -> str:
    return (getattr(user, "name", "") or getattr(user, "email", "") or "").strip() or "a person"


# Declared before /{incident_id} so the literal path is matched as itself.
@router.get("/policy", dependencies=[Depends(require_min_rank("admin"))])
async def get_policy() -> dict[str, Any]:
    """The auto-recover policy, and which failure classes it may include."""
    return {"policy": load_policy(_state()).model_dump(), "allowed_classes": list(ALLOWED_CLASSES)}


@router.put("/policy", dependencies=[Depends(require_min_rank("admin"))])
async def put_policy(body: AutoRecoverPolicy) -> dict[str, Any]:
    """Turn fixes by policy on or off, and set what they may do."""
    try:
        saved = save_policy(body, _state())
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"policy": saved.model_dump(), "allowed_classes": list(ALLOWED_CLASSES)}


@router.get("")
async def list_incidents(
    status: str = "open",
    limit: int = 100,
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Incidents in this workspace, most recent first. status: open, resolved or all.

    ``shared_causes`` groups the open ones that fail for one reason: the same
    connection, or the same error (incidents/shared.py)."""
    from revoluzen_flow.incidents.shared import shared_causes

    items = _store().list(workspace_id, status=None if status == "all" else status, limit=limit)
    return {
        "incidents": [i.model_dump() for i in items],
        "count": len(items),
        "shared_causes": shared_causes(_state(), items),
    }


@router.get("/{incident_id}")
async def get_incident(
    incident_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """One incident, its timeline, and what to look at: the last run that
    worked, the versions saved since, and what the failed step reads from
    and feeds."""
    from revoluzen_flow.incidents.service import incident_context

    store = _store()
    inc = store.get(incident_id, workspace_id=workspace_id)
    if inc is None:
        raise HTTPException(404, "Incident not found")
    return {
        "incident": inc.model_dump(),
        "events": [e.model_dump() for e in store.events(incident_id)],
        "context": incident_context(_state(), inc),
        "policy": load_policy(_state()).model_dump(),
    }


class NoteBody(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post("/{incident_id}/resolve", dependencies=[Depends(require_min_rank("developer"))])
async def resolve_incident(
    incident_id: str,
    body: NoteBody | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user=Depends(current_user_optional),
) -> dict[str, Any]:
    """Close an incident by hand, saying why. A later failure opens a new one."""
    store = _store()
    inc = store.get(incident_id, workspace_id=workspace_id)
    if inc is None:
        raise HTTPException(404, "Incident not found")
    if inc.status != "open":
        raise HTTPException(400, "This incident is already resolved.")
    note = ((body.note if body else "") or "").strip()
    who = _who(user)
    resolved = store.resolve(
        inc, resolution=f"Resolved by {who}" + (f": {note}" if note else "."), by=who,
    )
    return {"incident": resolved.model_dump()}


@router.post("/{incident_id}/notes", dependencies=[Depends(require_min_rank("developer"))])
async def add_note(
    incident_id: str,
    body: NoteBody,
    workspace_id: str = Depends(current_workspace_id),
    user=Depends(current_user_optional),
) -> dict[str, Any]:
    """Add a note to the incident's timeline: what was tried, who is on it."""
    store = _store()
    if store.get(incident_id, workspace_id=workspace_id) is None:
        raise HTTPException(404, "Incident not found")
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(400, "A note needs some text.")
    ev = store.add_event(incident_id, "note", detail={"note": note, "by": _who(user)})
    return {"event": ev.model_dump()}
