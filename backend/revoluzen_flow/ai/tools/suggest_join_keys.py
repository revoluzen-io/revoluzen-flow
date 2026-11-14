"""
suggest_join_keys tool — read-only.

Which columns a Join (or Lookup Join) step should match on, ranked from the
evidence in both inputs: shared values, names, types and uniqueness. Built
because the repair evaluation found every recorded model fixing a renamed
join column by writing the new name into `join_key`, when the step needed
`key_mode: mapped_keys` with a pair: guessing from names was not enough.

Trust contract:
  - Inputs run as Test Node runs them: in preview, upstream first, sinks
    never fire, nothing is saved.
  - A draft is model-written, so `ai/draft_checks.py` decides first, exactly
    as for test_pipeline_draft. If it refuses, nothing runs.
  - Column names, percentages and counts only. Row VALUES never leave here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier

_TIMEOUT_S = 60


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from revoluzen_flow.intelligence.join_discovery import suggest_for_step

    step_id = (inputs.get("step_id") or "").strip()
    pipeline_id = (inputs.get("pipeline_id") or "").strip()
    draft_id = (inputs.get("draft_id") or "").strip()
    if not step_id:
        raise ValueError("step_id is required: the Join step whose keys to suggest")

    try:
        from revoluzen_flow.main import app_state  # type: ignore
        store = app_state.get("workflow_store") or app_state.get("store")
        data_dir = app_state.get("data_dir")
    except Exception:  # noqa: BLE001
        store, data_dir, app_state = None, None, None
    if store is None or not data_dir:
        return {"suggestions": [], "message": "The pipeline store is unavailable, so the inputs could not be sampled."}

    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    draft = None
    if draft_id:
        from revoluzen_flow.ai.tools.draft_pipeline_from_intent import get_draft
        draft = get_draft(draft_id)
        if draft is None:
            return {"suggestions": [], "message": f"Draft {draft_id!r} no longer exists."}
        pipeline_id = draft.get("_modification_of") or pipeline_id
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline open or selected)")

    saved = store.get(pipeline_id, workspace_id=workspace_id)
    if saved is None or saved.workflow is None:
        return {"suggestions": [], "message": f"Pipeline {pipeline_id!r} not found in this workspace."}

    workflow = saved.workflow
    if draft is not None:
        from revoluzen_flow.ai.draft_checks import unsafe_to_test_reason
        from revoluzen_flow.ai.tools.test_pipeline_draft import _workflow_from

        saved_ir = saved.workflow.model_dump()
        reason = unsafe_to_test_reason(draft.get("steps") or [], saved_ir.get("steps") or [])
        if reason:
            return {"suggestions": [], "message": f"The draft was not run: {reason}. A person needs to review it."}
        workflow = _workflow_from(draft, pipeline_id, workspace_id)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                suggest_for_step, workflow, step_id, app_state=app_state, data_dir=data_dir,
            ),
            timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return {"suggestions": [], "message": f"Sampling the inputs took longer than {_TIMEOUT_S} seconds."}
    except ValueError as exc:
        return {"suggestions": [], "message": str(exc)}

    top = result["suggestions"][0] if result["suggestions"] else None
    if top is None:
        result["message"] = "No pair of columns shares values or a name; check that the two inputs describe the same things."
    elif top["same_name"]:
        result["message"] = (
            f"Best key: {top['left']} on both sides. Set key_mode to same_key and join_key to "
            f"[{top['left']!r}]."
        )
    else:
        result["message"] = (
            f"Best key: {top['left']} on {result['left_label']} with {top['right']} on "
            f"{result['right_label']}. The names differ, so set key_mode to mapped_keys with "
            f"key_pairs [{{'left': {top['left']!r}, 'right': {top['right']!r}, 'operator': '='}}]; "
            "do not put either name in join_key."
        )
    return result


DEFINITION = ToolDefinition(
    name="suggest_join_keys",
    tier=ToolTier.READ,
    description=(
        "Suggest which columns a Join or Lookup Join step should match on, ranked by "
        "evidence from both inputs: how many values they share, their names, types "
        "and uniqueness, and the join's shape (many-to-one, many-to-many). Runs the "
        "inputs on a sample without writing anything. Use it before setting or "
        "repairing a join's keys, especially when the key columns have different "
        "names on each side. Pass draft_id to check a draft you made."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {"type": "string", "description": "The pipeline. Defaults to the open one."},
            "step_id": {"type": "string", "description": "The Join or Lookup Join step."},
            "draft_id": {"type": "string", "description": "Optional: a draft of the pipeline to sample instead."},
        },
        "required": ["step_id"],
    },
    output_schema={
        "suggestions": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "join"],
)
