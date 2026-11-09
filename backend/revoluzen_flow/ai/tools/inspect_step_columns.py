"""
inspect_step_columns tool — read-only.

Lists the columns a step receives, so the agent can fix a renamed column from
what the data actually holds instead of guessing from an error message. Engine
errors often leave the real column out of their suggestions.

Trust contract (per docs/ai-boundary-contract.md §2):
  - Column names and types only. Row values never leave this tool.
  - It runs the steps upstream of the target with a one-row preview, the same
    way Test Node and the drift check do, so it must not fire a side effect.
    If any upstream step could, the tool says so and runs nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from revoluzen_flow.ai.step_params import guide_for
from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier

_TIMEOUT_S = 60
_MAX_COLUMNS = 200
_MAX_STEPS = 30

# Steps that only read or reshape data in memory. Anything not listed here is
# treated as having a side effect.
_READ_ONLY_STEPS = frozenset({
    "filter", "transform", "deduplicate", "sort", "rename", "typecast", "derived_column",
    "aggregate", "join", "lookup", "union", "pivot", "unpivot", "window", "sample",
    "validate", "validation", "data_quality", "schema_mapper", "flatten_explode", "data_wrangler",
})
# Reading these moves an offset or calls an endpoint that may do more than read.
_ACTIVE_SOURCES = ("api", "rest", "http", "webhook", "kafka", "cdc", "openapi", "saas")


def _kind(step: Any) -> str:
    kind = getattr(step, "type", "") or ""
    return str(getattr(kind, "value", kind))


def _is_source(step: Any) -> bool:
    kind = _kind(step)
    return kind == "source" or kind.endswith("_source")


def _can_preview(step: Any) -> bool:
    kind = _kind(step)
    if kind in _READ_ONLY_STEPS:
        return True
    if not _is_source(step):
        return False
    connector = str((step.params or {}).get("connector_type") or "").lower()
    return not any(word in kind or word in connector for word in _ACTIVE_SOURCES)


def _columns(result: Any) -> list[dict[str, str]]:
    types = {c.get("name"): c.get("type") for c in (result.schema_info or []) if isinstance(c, dict)}
    return [
        {"name": name, "type": str(types.get(name) or "")}
        for name in (result.columns or [])[:_MAX_COLUMNS]
    ]


def _preview(workflow: Any, targets: list[str], data_dir: str, app_state: Any) -> list[dict[str, Any]]:
    from revoluzen_flow.engine.executor import WorkflowExecutor

    labels = {s.id: s.label or s.id for s in workflow.steps}
    seen: dict[str, dict[str, Any]] = {}
    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
    for target in targets:
        trace = executor.execute_step_trace(workflow, target, preview_limit=1)
        for step_id, result in trace.step_results.items():
            if step_id in seen:
                continue
            entry: dict[str, Any] = {
                "step_id": step_id,
                "label": labels.get(step_id, step_id),
                "columns": _columns(result),
            }
            if result.status == "error":
                # An engine error can carry the install's absolute paths.
                entry["error"] = (result.error or "").replace(str(data_dir), "<data>")[:300]
            seen[step_id] = entry
    return list(seen.values())


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline selected on the current page)")
    step_id = inputs.get("step_id", "")
    step_params: dict[str, str] = {}

    def answer(available: bool, message: str, steps: list | None = None, columns: list | None = None) -> dict[str, Any]:
        return {
            "pipeline_id": pipeline_id,
            "available": available,
            "steps": steps or [],
            "columns": columns or [],
            "step_params": step_params,
            "message": message,
        }

    try:
        from revoluzen_flow.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
        data_dir = app_state.get("data_dir")
    except Exception:
        store, data_dir, app_state = None, None, None
    if store is None or not data_dir:
        return answer(False, "The workflow store is unavailable, so columns cannot be read.")

    workspace_id = ctx.workspace_id or "default"
    try:
        version = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        version = store.get(pipeline_id)
    if version is None or version.workflow is None:
        return answer(False, f"Pipeline {pipeline_id!r} not found in this workspace.")

    workflow = version.workflow
    step_params.update(guide_for(_kind(s) for s in workflow.steps))
    outline = [
        {"step_id": s.id, "label": s.label or s.id, "type": _kind(s)}
        for s in workflow.steps[:_MAX_STEPS]
    ]
    by_id = {s.id: s for s in workflow.steps}

    from revoluzen_flow.engine.executor import WorkflowExecutor

    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
    if step_id:
        if step_id not in by_id:
            return answer(False, f"Step {step_id!r} not found in this pipeline.", steps=outline)
        upstream = executor._build_input_map(workflow).get(step_id, [])
        targets = list(upstream) or [step_id]
    else:
        targets = [s.id for s in workflow.steps if _is_source(s)]
    if not targets:
        return answer(False, "This pipeline has no source step to read columns from.", steps=outline)

    needed: list[Any] = []
    for target in targets:
        needed.append(by_id[target])
        needed.extend(executor._get_dependencies(workflow, target))
    blocked = sorted({s.id for s in needed if not _can_preview(s)})
    if blocked:
        return answer(
            False,
            f"Reading columns would have to run step(s) {', '.join(blocked)}, which may have "
            "side effects, so nothing was run. Ask the user for the column names.",
            steps=outline,
        )

    try:
        columns = await asyncio.wait_for(
            asyncio.to_thread(_preview, workflow, targets, data_dir, app_state),
            timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return answer(False, f"Reading columns took longer than {_TIMEOUT_S} seconds.", steps=outline)
    message = "Columns each upstream step produces. Names and types only."
    return answer(True, message, steps=outline, columns=columns)


DEFINITION = ToolDefinition(
    name="inspect_step_columns",
    tier=ToolTier.READ,
    description=(
        "List the columns available to a pipeline step, from the source and each "
        "step before it, as names and types only. Use this before editing a step "
        "that names a column, or when a run fails because a column was not found: "
        "error messages often leave the real column out of their suggestions. Pass "
        "step_id for the columns a step receives, or omit it for the source columns. "
        "Also returns every step's id, label and type, and what the parameters of "
        "these step types mean."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Pipeline UUID. Optional when one pipeline is open or selected.",
            },
            "step_id": {
                "type": "string",
                "description": "The step whose inputs you want. Omit for the source columns.",
            },
        },
        "required": [],
    },
    output_schema={
        "pipeline_id": "str",
        "available": "bool",
        "steps": "list",
        "columns": "list",
        "step_params": "dict",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "schema", "columns"],
)
