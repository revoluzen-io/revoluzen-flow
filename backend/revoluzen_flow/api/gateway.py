"""API Gateway — manage API keys, published endpoints, and invoke published pipelines."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from revoluzen_flow.audit import actor_for, audit_action
from revoluzen_flow.auth.deps import current_workspace_id, require_auth, require_min_rank
from revoluzen_flow.common.client_ip import client_ip
from revoluzen_flow.ir.governance import is_deactivated

router = APIRouter(tags=["gateway"])

# The `triggered_by` value on a run another application started. It counts as a
# production trigger (monitoring/store.py) and shows as "API" on Executions.
TRIGGER_API = "api"


def _get_store(request: Request):
    from revoluzen_flow.main import app_state
    return app_state["gateway_store"]


# SQL-template injection on PUBLIC (no-API-key) endpoints. A published endpoint
# with require_api_key=False is callable by anyone; if its workflow puts request
# values into RAW SQL, an unauthenticated caller can inject SQL into the
# connected database. `{{ }}` expressions and `${param.x}` placeholders both
# reach the step as plain text (see engine/parameters.py), neither is bound.
# Refuse that combination unless the operator explicitly overrides.
_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}|\$\{param\.", re.S)
# Fields that end up in SQL. Nodes name them differently (query, source_query,
# pre_sql, where, a filter's condition or rules, a computed column's expression),
# so match loosely: a false positive only asks the operator to require a key.
_SQL_FIELD_RE = re.compile(
    r"sql|query|statement|script|where|having|condition|expression|filter|rules",
    re.I,
)


def _has_templated_sql(value: Any, in_sql_field: bool = False) -> bool:
    """True if a template sits in a SQL field, at any depth of a step's params."""
    if isinstance(value, str):
        return in_sql_field and bool(_TEMPLATE_RE.search(value))
    if isinstance(value, dict):
        return any(
            _has_templated_sql(v, in_sql_field or bool(_SQL_FIELD_RE.search(str(k))))
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_templated_sql(v, in_sql_field) for v in value)
    return False


def _templated_sql_step_ids(workflow) -> list[str]:
    """Ids of steps with a ``{{ }}`` or ``${param.x}`` template in a SQL field."""
    hits: list[str] = []
    for step in getattr(workflow, "steps", []) or []:
        if _has_templated_sql(getattr(step, "params", None) or {}):
            hits.append(getattr(step, "id", "?"))
    return hits


def _guard_public_templated_sql(workflow_id: str, allow_override: bool) -> None:
    """Raise 400 if publishing a PUBLIC endpoint for a workflow that uses
    templated raw SQL, unless the caller explicitly overrode it. Best-effort on
    lookup — a missing workflow doesn't block (it 404s downstream)."""
    if allow_override:
        return
    try:
        from revoluzen_flow.main import app_state
        wf_version = app_state["store"].get(workflow_id)
        workflow = getattr(wf_version, "workflow", None)
    except Exception:  # noqa: BLE001
        return
    if workflow is None:
        return
    bad = _templated_sql_step_ids(workflow)
    if bad:
        raise HTTPException(
            400,
            {
                "message": "Refusing to publish a PUBLIC (no-API-key) endpoint whose "
                "workflow interpolates request vars into raw SQL — an unauthenticated "
                "caller could inject SQL via the request body. Require an API key, remove "
                "the {{ }} and ${param.x} expressions from the SQL, or set "
                "allow_templated_sql_public=true to override (NOT recommended).",
                "steps": bad,
            },
        )


# ── API Key Management ────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str
    scopes: list[str] = ["read", "execute"]
    rate_limit_rpm: int = 60
    ip_allowlist: list[str] = []
    expires_days: int = 0


@router.get("/api/gateway/keys")
async def list_keys(request: Request, workspace_id: str = Depends(current_workspace_id),
                    _user=Depends(require_auth)):
    store = _get_store(request)
    return store.list_api_keys(workspace_id)


@router.post("/api/gateway/keys")
async def create_key(body: CreateKeyRequest, request: Request,
                     workspace_id: str = Depends(current_workspace_id),
                     _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    user_id = getattr(_user, "id", "anonymous")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Give the API key a name.")
    created = store.create_api_key(
        name=name, workspace_id=workspace_id, created_by=user_id,
        scopes=body.scopes, rate_limit_rpm=body.rate_limit_rpm,
        ip_allowlist=body.ip_allowlist, expires_days=body.expires_days,
    )
    audit_action(
        action="gateway.key.create", resource_type="api_key", resource_id=created["id"],
        actor=actor_for(_user), workspace_id=workspace_id, details={"name": name},
    )
    return created


@router.delete("/api/gateway/keys/{key_id}")
async def revoke_key(key_id: str, request: Request,
                     workspace_id: str = Depends(current_workspace_id),
                     _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    key = store.get_api_key(key_id)
    if not key or (key.get("workspace_id") or "default") != workspace_id:
        raise HTTPException(404, "API key not found")
    store.revoke_api_key(key_id)
    audit_action(
        action="gateway.key.revoke", resource_type="api_key", resource_id=key_id,
        actor=actor_for(_user), workspace_id=workspace_id, details={"name": key.get("name")},
    )
    return {"status": "revoked"}


# ── Published Endpoints Management ────────────────────────────────────

class PublishEndpointRequest(BaseModel):
    workflow_id: str
    path: str
    name: str = ""
    description: str = ""
    method: str = "POST"
    require_api_key: bool = True
    rate_limit_rpm: int = 30
    timeout_seconds: int = 300
    input_schema: dict = {}
    # Explicit acknowledgement to publish a PUBLIC endpoint whose workflow uses
    # templated raw SQL (unauth SQL-injection risk). Default False = refuse.
    allow_templated_sql_public: bool = False


class UpdateEndpointRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    rate_limit_rpm: int | None = None
    timeout_seconds: int | None = None
    is_active: int | None = None
    require_api_key: bool | None = None
    allow_templated_sql_public: bool = False


# The path becomes part of a URL other systems store, so it stays URL-safe with
# no encoding. Each segment starts with a letter or digit, which also rules out
# `.` and `..`.
_ENDPOINT_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*(?:/[A-Za-z0-9][A-Za-z0-9._~-]*)*$")
_ENDPOINT_METHODS = ("POST", "PUT", "GET")


def _clean_endpoint_path(path: str) -> str:
    cleaned = (path or "").strip().strip("/")
    if not cleaned or len(cleaned) > 200 or not _ENDPOINT_PATH_RE.match(cleaned):
        raise HTTPException(
            400,
            "The URL path can use letters, numbers, hyphens, underscores, dots and "
            "slashes, and each part must start with a letter or number.",
        )
    return cleaned


def _require_endpoint(store, endpoint_id: str, workspace_id: str) -> dict:
    """The endpoint, if it lives in the caller's workspace. Otherwise 404, the
    same answer as an id that does not exist."""
    endpoint = store.get_endpoint(endpoint_id)
    if not endpoint or (endpoint.get("workspace_id") or "default") != workspace_id:
        raise HTTPException(404, "Endpoint not found")
    return endpoint


@router.get("/api/gateway/endpoints")
async def list_endpoints(request: Request, workspace_id: str = Depends(current_workspace_id),
                         _user=Depends(require_auth)):
    store = _get_store(request)
    return store.list_endpoints(workspace_id)


@router.post("/api/gateway/endpoints")
async def publish_endpoint(body: PublishEndpointRequest, request: Request,
                           workspace_id: str = Depends(current_workspace_id),
                           _user=Depends(require_min_rank("data_engineer"))):
    from revoluzen_flow.main import app_state
    store = _get_store(request)
    path = _clean_endpoint_path(body.path)
    method = (body.method or "POST").upper()
    if method not in _ENDPOINT_METHODS:
        raise HTTPException(400, f"Method must be one of: {', '.join(_ENDPOINT_METHODS)}.")
    if not app_state["store"].get(body.workflow_id, workspace_id=workspace_id):
        raise HTTPException(404, "Pipeline not found in this workspace")
    if store.get_endpoint_by_path(path):
        raise HTTPException(409, f"The path /{path} is already in use. Choose another.")
    # Block a PUBLIC endpoint whose workflow has templated raw SQL (unauth SQLi).
    if not body.require_api_key:
        _guard_public_templated_sql(body.workflow_id, body.allow_templated_sql_public)
    created = store.publish_endpoint(
        workflow_id=body.workflow_id, path=path, name=body.name,
        description=body.description, method=method, workspace_id=workspace_id,
        require_api_key=body.require_api_key, rate_limit_rpm=body.rate_limit_rpm,
        timeout_seconds=body.timeout_seconds, input_schema=body.input_schema,
        allow_templated_sql_public=not body.require_api_key and body.allow_templated_sql_public,
    )
    audit_action(
        action="gateway.endpoint.publish", resource_type="published_endpoint", resource_id=path,
        actor=actor_for(_user), workspace_id=workspace_id,
        details={"workflow_id": body.workflow_id, "require_api_key": body.require_api_key},
    )
    return created


@router.put("/api/gateway/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: str, body: UpdateEndpointRequest, request: Request,
                          workspace_id: str = Depends(current_workspace_id),
                          _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    existing = _require_endpoint(store, endpoint_id, workspace_id)
    # If this update turns the endpoint PUBLIC, re-run the templated-SQL guard.
    if body.require_api_key is False:
        _guard_public_templated_sql(existing["workflow_id"], body.allow_templated_sql_public)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    # Only meaningful alongside turning the key off; the route re-checks it on
    # every call to a public endpoint.
    override = updates.pop("allow_templated_sql_public", False)
    if body.require_api_key is False:
        updates["allow_templated_sql_public"] = 1 if override else 0
    result = store.update_endpoint(endpoint_id, **updates)
    if not result:
        raise HTTPException(404, "Endpoint not found")
    audit_action(
        action="gateway.endpoint.update", resource_type="published_endpoint",
        resource_id=existing.get("path", endpoint_id), actor=actor_for(_user),
        workspace_id=workspace_id, details=updates,
    )
    return result


@router.delete("/api/gateway/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: str, request: Request,
                          workspace_id: str = Depends(current_workspace_id),
                          _user=Depends(require_min_rank("data_engineer"))):
    store = _get_store(request)
    existing = _require_endpoint(store, endpoint_id, workspace_id)
    store.delete_endpoint(endpoint_id)
    audit_action(
        action="gateway.endpoint.delete", resource_type="published_endpoint",
        resource_id=existing.get("path", endpoint_id), actor=actor_for(_user),
        workspace_id=workspace_id, details={"workflow_id": existing.get("workflow_id")},
    )
    return {"status": "deleted"}


# ── Public endpoint invocation ────────────────────────────────────────

def _client_ip_allowed(ip: str, allowlist: list[str]) -> bool:
    """An exact address or a CIDR range. An unreadable address is refused."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if address in ipaddress.ip_network(str(entry).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def _key_allowlist(key_info: dict) -> list[str]:
    raw = key_info.get("ip_allowlist")
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _is_paused(workflow) -> bool:
    """True when the pipeline is deactivated or archived."""
    if getattr(getattr(workflow, "status", None), "value", None) == "archived":
        return True
    return is_deactivated(workflow)


async def _read_parameter_values(request: Request) -> dict[str, Any]:
    """The values a caller sent: a JSON object, or the query string on a GET.

    An empty body means no values. A body that is not a JSON object is refused
    rather than ignored, because ignoring it would run the pipeline on its
    defaults while the caller believes it passed something.
    """
    if request.method == "GET":
        return {k: v for k, v in request.query_params.items() if k != "api_key"}
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise HTTPException(
            400,
            'The request body must be a JSON object of parameter values, such as {"region": "EU"}.',
        )
    return parsed


def _audit_invocation(request: Request, path: str, endpoint: dict, key_info: dict | None,
                      outcome: str, **extra: Any) -> None:
    """Leave a trail for a call that got past the key check.

    This is an unauthenticated-by-user, API-key-authed external entry point that
    executes a pipeline (who/which key, which endpoint, from where, outcome).
    Parameter values stay out of it, since they can be sensitive; the run record
    keeps them. Never raises: a logging failure must not fail the call.
    """
    ki = key_info or {}
    details = {
        "workflow_id": endpoint.get("workflow_id"),
        "workspace_id": endpoint.get("workspace_id"),
        "method": request.method,
        "client_ip": client_ip(request),
        "outcome": outcome,
        **extra,
    }
    audit_action(
        action="gateway.invoke", resource_type="published_endpoint", resource_id=path,
        actor=str(ki.get("id") or "public"),
        workspace_id=endpoint.get("workspace_id") or "default", details=details,
    )
    try:
        from revoluzen_flow.main import app_state
        audit_logger = app_state.get("audit_logger")
        if audit_logger:
            audit_logger.log(
                user_id=str(ki.get("id") or "api-key"),
                user_email=str(ki.get("name") or "gateway"),
                action="gateway.invoke",
                resource_type="published_endpoint",
                resource_id=path,
                details=details,
            )
    except Exception:
        pass


def _compact_run(workflow, run: dict, parameter_values: dict) -> dict[str, Any]:
    """What an outside caller gets back: the outcome of the run, never its data.

    `run` holds each step's sample rows, columns and raw error text, any of which
    can expose data or the machine it runs on. None of that leaves here. The
    execution id is how the caller's operator finds the rest on the Executions
    page.
    """
    results = run.get("step_results") or {}
    labels = {s.id: (s.label or s.id) for s in workflow.steps}
    # Canvas order, then anything the engine reported that is not on the canvas
    # (a run refused before its first step reports through a synthetic step).
    order = [s.id for s in workflow.steps if s.id in results]
    order += [sid for sid in results if sid not in labels]
    steps = [
        {
            "step_id": sid,
            "name": labels.get(sid, sid),
            "status": results[sid].get("status"),
            "row_count": results[sid].get("row_count", 0),
        }
        for sid in order
    ]
    compact: dict[str, Any] = {
        "status": run.get("status"),
        "execution_id": run.get("execution_id"),
        "pipeline_id": workflow.id,
        "parameter_values": parameter_values,
        "duration_ms": run.get("duration_ms"),
        "steps": steps,
    }
    if run.get("status") == "error":
        failed = next((s for s in steps if s["status"] == "error"), None)
        compact["message"] = (
            f'Step "{failed["name"]}" failed. Open this run on the Executions page to see why.'
            if failed and failed["step_id"] in labels
            else "The pipeline could not start. Open this run on the Executions page to see why."
        )
    elif run.get("overlap_detected"):
        # The pipeline's overlap policy turned this call away. No new run exists.
        compact["message"] = run.get("message")
        compact["running_execution_id"] = run.get("running_execution_id")
    return compact


@router.api_route(
    "/api/published/{path:path}",
    methods=["GET", "POST", "PUT"],
    # Dynamic catch-all gateway for user-published pipelines — the path is
    # arbitrary per user, so it can't be meaningfully documented, and the
    # multi-method registration produced a duplicate OpenAPI operation id.
    # Keep it out of the schema (it still serves requests normally).
    include_in_schema=False,
)
async def invoke_published_endpoint(path: str, request: Request):
    """Start a published pipeline from outside the product.

    Validates API key (via X-API-Key header or ?api_key query param), checks
    rate limits, then runs the pipeline through the same path as the Run button,
    so the run is admitted, recorded, counted and alerted on like any other. The
    JSON body's keys become the run's parameter values. An outside trigger is a
    real run, so it reads the full data rather than the dev sample.
    """
    store = _get_store(request)
    endpoint = store.get_endpoint_by_path(path)
    if not endpoint:
        raise HTTPException(404, f"No published endpoint at: /{path}")

    if not endpoint.get("is_active"):
        raise HTTPException(503, "Endpoint is currently disabled")

    # API key validation
    key_info = None
    if endpoint.get("require_api_key"):
        from revoluzen_flow import runtime_config
        header_key = request.headers.get("X-API-Key")
        # Server mode: accept the key ONLY via the header — never a query
        # string, which leaks into access logs, proxies, and browser history.
        # Local mode keeps ?api_key= for convenience.
        if runtime_config.IS_SERVER_MODE:
            api_key = header_key
            hint = "Pass it in the X-API-Key header."
        else:
            api_key = header_key or request.query_params.get("api_key")
            hint = "Pass via X-API-Key header or ?api_key param."
        if not api_key:
            raise HTTPException(401, f"API key required. {hint}")
        key_info = store.validate_api_key(api_key, required_scope="execute")
        if not key_info:
            raise HTTPException(403, "Invalid or expired API key")
        # A key opens endpoints in its own workspace only. Same answer as a bad
        # key, so a caller cannot use it to learn which paths exist elsewhere.
        if (key_info.get("workspace_id") or "default") != (endpoint.get("workspace_id") or "default"):
            raise HTTPException(403, "Invalid or expired API key")
        allowlist = _key_allowlist(key_info)
        if allowlist and not _client_ip_allowed(client_ip(request), allowlist):
            raise HTTPException(403, "This API key cannot be used from this address")
        rate_subject = hashlib.sha256(api_key.encode()).hexdigest()
        rpm = min(endpoint.get("rate_limit_rpm", 30), key_info.get("rate_limit_rpm", 60))
    else:
        # No key to count against, so a public endpoint is limited as a whole.
        # Otherwise anyone could start runs as fast as they can send requests.
        rate_subject = f"public:{endpoint['id']}"
        rpm = endpoint.get("rate_limit_rpm", 30)

    # Rate limiting
    if not store.check_rate_limit(rate_subject, rpm, endpoint["id"]):
        raise HTTPException(429, f"Rate limit exceeded ({rpm} requests/minute)")

    # A POST endpoint must not fire from a GET, which a link or an image tag can
    # send without the caller meaning to.
    allowed_method = (endpoint.get("method") or "POST").upper()
    if request.method != allowed_method:
        raise HTTPException(
            405, f"This endpoint accepts {allowed_method} requests.", headers={"Allow": allowed_method},
        )

    # Record the call
    store.record_call(endpoint["id"])

    from revoluzen_flow.main import app_state
    wf_store = app_state["store"]
    wf_version = wf_store.get(endpoint["workflow_id"])
    if not wf_version:
        raise HTTPException(404, "Associated workflow not found")

    # Tenant scope: the published endpoint is workspace-bound. Confirm the
    # workflow it points at actually lives in that workspace, so a stale or
    # tampered endpoint row can't invoke another tenant's pipeline by id
    # alone. No-op when either side lacks a workspace_id (legacy single-
    # workspace installs).
    wf = wf_version.workflow
    endpoint_ws = endpoint.get("workspace_id")
    workflow_ws = getattr(wf, "workspace_id", None)
    if endpoint_ws and workflow_ws and endpoint_ws != workflow_ws:
        raise HTTPException(404, "Associated workflow not found")

    if _is_paused(wf):
        _audit_invocation(request, path, endpoint, key_info, "blocked_deactivated")
        raise HTTPException(423, "This pipeline is deactivated. Activate it to run it from another application.")

    # A pipeline can change after publishing, so the check made when a public
    # endpoint was published is repeated before every run.
    if not endpoint.get("require_api_key") and not endpoint.get("allow_templated_sql_public"):
        if _templated_sql_step_ids(wf):
            _audit_invocation(request, path, endpoint, key_info, "blocked_templated_sql")
            raise HTTPException(
                403,
                "This endpoint is blocked because its pipeline now places request values "
                "in SQL. Require an API key for this endpoint.",
            )

    # Validate before starting anything, so a bad call is a 400 naming the
    # parameter rather than a failed run in the history.
    from revoluzen_flow.engine.parameters import ParameterError, resolve_parameter_values
    body = await _read_parameter_values(request)
    declared = {p.name for p in (wf.parameters or [])}
    # JSON null means "not supplied": the default applies, and it cannot stand
    # in for a required value. An unknown name is still refused.
    supplied = {k: v for k, v in body.items() if v is not None or k not in declared}
    try:
        resolved = resolve_parameter_values(wf.parameters or [], supplied)
    except ParameterError as exc:
        _audit_invocation(request, path, endpoint, key_info, "rejected_parameters")
        raise HTTPException(400, str(exc)) from exc
    parameter_values = {k: v for k, v in resolved.items() if v is not None}

    # Run via the shared path
    from revoluzen_flow.api.execution import run_saved_workflow
    ki = key_info or {}
    try:
        run = await run_saved_workflow(
            wf_version,
            workflow_id=endpoint["workflow_id"],
            workspace_id=endpoint_ws or workflow_ws or "default",
            parameter_values=parameter_values,
            full_run=True,
            triggered_by=TRIGGER_API,
            run_user_id=f"api-key:{ki['id']}" if ki else "public-endpoint",
            run_metadata={
                "published_endpoint": f"/{endpoint['path']}",
                "api_key_id": ki.get("id"),
                "api_key_name": ki.get("name"),
            },
        )
    except HTTPException as exc:
        # The run path puts the raw exception text in this detail. It is on the
        # run record already and has no business leaving the product.
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        execution_id = detail.get("execution_id")
        _audit_invocation(request, path, endpoint, key_info, "error", execution_id=execution_id)
        raise HTTPException(
            exc.status_code,
            {
                "message": "The pipeline run could not finish. Open it on the Executions page to see why.",
                "execution_id": execution_id,
            },
        ) from exc

    _audit_invocation(
        request, path, endpoint, key_info, str(run.get("status")), execution_id=run.get("execution_id"),
    )
    return _compact_run(wf, run, parameter_values)
