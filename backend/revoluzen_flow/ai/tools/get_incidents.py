"""
get_incidents tool — read-only.

The pipelines that are failing right now, and for one of them what an
operator asks first: when it last worked, which versions were saved since,
what the failed step reads from and feeds, the cause class of the error,
and what the auto-recover policy has already tried. Lets the Copilot start
a diagnosis from what changed instead of from the error text alone.

Sends no row values: incident fields, step names and version notes only.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier

_EVENTS_SHOWN = 12


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        from revoluzen_flow.main import app_state  # type: ignore
    except Exception:  # noqa: BLE001
        app_state = {}
    store = app_state.get("incident_store")
    if store is None:
        return {"incidents": [], "message": "Incidents are unavailable on this install."}
    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    incident_id = (inputs.get("incident_id") or "").strip()
    pipeline_id = (inputs.get("pipeline_id") or "").strip()

    if not incident_id and pipeline_id:
        open_one = store.open_for_workflow(pipeline_id, workspace_id)
        if open_one is None:
            return {"incidents": [], "message": "This pipeline has no open incident."}
        incident_id = open_one.id

    if incident_id:
        from revoluzen_flow.incidents.service import incident_context

        inc = store.get(incident_id, workspace_id=workspace_id)
        if inc is None:
            return {"incidents": [], "message": f"Incident {incident_id!r} not found in this workspace."}
        events = store.events(incident_id)[-_EVENTS_SHOWN:]
        return {
            "incident": inc.model_dump(),
            "timeline": [{"at": e.at, "kind": e.kind, "run_id": e.run_id, **e.detail} for e in events],
            "context": incident_context(app_state, inc),
        }

    status = (inputs.get("status") or "open").strip().lower()
    items = store.list(workspace_id, status=None if status == "all" else status, limit=25)
    from revoluzen_flow.incidents.shared import shared_causes
    return {
        # Open incidents that fail for one reason (a connection, or the same
        # error): fix the cause once instead of each pipeline.
        "shared_causes": shared_causes(app_state, items),
        "incidents": [
            {k: i.model_dump()[k] for k in (
                "id", "workflow_id", "workflow_name", "status", "failure_class",
                "failed_step", "opened_at", "last_seen_at", "occurrences", "auto_attempts",
            )}
            for i in items
        ],
        "count": len(items),
    }


DEFINITION = ToolDefinition(
    name="get_incidents",
    tier=ToolTier.READ,
    description=(
        "List the pipelines that are failing now (open incidents), with the "
        "groups of them that fail for one shared cause (the same connection or "
        "the same error), or read one "
        "incident: the failed step and the cause class of its error, when the "
        "pipeline last worked, the versions saved since then, what the failed "
        "step reads from and feeds, and what the auto-recover policy tried. Use "
        "it first when asked why a pipeline is failing or what is broken."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "One incident to read in full."},
            "pipeline_id": {"type": "string", "description": "Read the open incident of this pipeline."},
            "status": {"type": "string", "enum": ["open", "resolved", "all"], "description": "For a list. Default open."},
        },
        "required": [],
    },
    output_schema={},
    handler=_handler,
    requires_idempotency_key=False,
    tags=["incident", "read", "operations"],
)
