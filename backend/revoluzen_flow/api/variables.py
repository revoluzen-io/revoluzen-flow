"""Variables CRUD API — global and project-scoped key-value pairs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from revoluzen_flow.auth.deps import current_workspace_id, require_auth, require_min_rank
from revoluzen_flow.variables.models import Variable, VariableCreate, VariableUpdate

# 2026-05-30 (Track S P1): same router-level + write-level guard pattern
# as workflows/schedules. Variables hold runtime values that downstream
# pipelines consume, so write authority gates at data_engineer+.
_AUTH = Depends(require_auth)
_AUTHOR = Depends(require_min_rank("developer"))

router = APIRouter(
    prefix="/api/variables",
    tags=["variables"],
    dependencies=[_AUTH],
)


def _safe_workspace_id(request: Request) -> str:
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
    return app_state["variable_store"]


def _public(variable) -> dict:
    """A variable as the API returns it: a secret's value never leaves.

    GET /{id} and /resolve used to return secret values in clear to any
    signed-in role (2026-11-13 pen test). Pipelines still get the real
    value through the executor's own resolution path.
    """
    from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
    data = variable.model_dump(mode="json")
    if data.get("type") == "secret" and data.get("value"):
        data["value"] = SECRET_PLACEHOLDER
    return data


# 2026-05-30 (P7): trailing-slash alias.
@router.get("", include_in_schema=False)
@router.get("/")
async def list_variables(
    scope: str | None = None,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """List variables scoped to the caller's workspace."""
    store = get_store()
    return store.list_all(scope=scope, project_id=project_id, workspace_id=workspace_id)


@router.post("", dependencies=[_AUTHOR], include_in_schema=False)
@router.post("/", dependencies=[_AUTHOR])
async def create_variable(
    body: VariableCreate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Create a new variable in the caller's workspace."""
    store = get_store()
    variable = Variable(
        key=body.key,
        value=body.value,
        type=body.type,
        scope=body.scope,
        project_id=body.project_id,
        workspace_id=workspace_id,
        description=body.description,
    )
    created = store.create(variable)
    return _public(created)


@router.get("/{variable_id}")
async def get_variable(
    variable_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Get a variable by ID — 404 across workspace boundary."""
    store = get_store()
    variable = store.get(variable_id, workspace_id=workspace_id)
    if not variable:
        raise HTTPException(404, "Variable not found")
    return _public(variable)


@router.put("/{variable_id}", dependencies=[_AUTHOR])
async def update_variable(
    variable_id: str,
    body: VariableUpdate,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Update a variable — refuses to cross workspace boundary."""
    from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
    store = get_store()
    updates = body.model_dump(exclude_none=True)
    if updates.get("value") == SECRET_PLACEHOLDER:
        updates.pop("value")  # the masked value sent back means "unchanged"
    variable = store.update(variable_id, updates, workspace_id=workspace_id)
    if not variable:
        raise HTTPException(404, "Variable not found")
    return _public(variable)


@router.delete("/{variable_id}", dependencies=[_AUTHOR])
async def delete_variable(
    variable_id: str,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Delete a variable — scoped to workspace."""
    store = get_store()
    if not store.delete(variable_id, workspace_id=workspace_id):
        raise HTTPException(404, "Variable not found")
    return {"deleted": True}


@router.post("/resolve", dependencies=[_AUTHOR])
async def resolve_variable(
    key: str,
    project_id: str | None = None,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Resolve a variable value — project scope first, then global fallback,
    both restricted to the caller's workspace. Secret values stay masked."""
    from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
    store = get_store()
    value = store.resolve(key, project_id, workspace_id=workspace_id)
    if value is None:
        raise HTTPException(404, f"Variable '{key}' not found")
    secret_keys = {
        v.get("key") if isinstance(v, dict) else getattr(v, "key", None)
        for v in store.list_all(workspace_id=workspace_id)
        if (v.get("type") if isinstance(v, dict) else getattr(v, "type", None)) == "secret"
    }
    if key in secret_keys:
        value = SECRET_PLACEHOLDER
    return {"key": key, "value": value}
