"""
validate_pipeline tool — read-only.

Runs the IR validator and a column check against a saved pipeline, or
against a draft, and returns the structured list of errors.

Two things it did not do, both found by the repair evaluation:

  - **It could not see a draft.** The agent edits a pipeline into a draft
    and the draft is what it is about to ask someone to apply, but the only
    thing validatable was the saved pipeline — so validating after an edit
    reported on the version without the edit in it.

  - **It said "valid" for pipelines that fail on the first row.**
    `ir.validator` checks shape: types, connections, required params. It
    has no opinion on `amount > 100` against a source whose column is
    `order_amount`, which is precisely the fault in the evaluation's
    renamed-column cases. A column pass now resolves what each step's
    upstream really produces and reports references that do not exist.

The column pass needs to read the pipeline's sources, so it runs only when
the engine can resolve a schema. When it cannot, the result says
`columns_checked: false` rather than quietly reporting a clean bill.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.column_refs import missing_columns
from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier
from revoluzen_flow.ai.tools.draft_pipeline_from_intent import get_draft


# Steps that pass their columns through unchanged. They drop rows, reorder
# them or route them, but the set of column names out is the set in. That
# is what lets a broken one be stepped over when working out what reaches
# the step after it.
#
# Deliberately short. Anything that rewrites the schema — transform,
# derived_column, aggregate, join, union, pivot, window, schema_mapper —
# must NOT be here, or a reference to a column that step creates would be
# reported as missing.
_COLUMN_PRESERVING = frozenset({
    "filter", "sample", "sort", "deduplicate",
    "validate", "validation", "data_quality",
    "if_condition", "switch_case", "conditional_split",
})


def _kind_of(step: Any) -> str:
    raw = getattr(step, "type", "") or ""
    return str(getattr(raw, "value", raw)).strip().lower()


def _upstream_columns(workflow: Any, data_dir: str, app_state: Any) -> dict[str, list[str]] | None:
    """Columns reaching each step, or None if the engine could not say.

    Reuses the same preview path `inspect_step_columns` uses, so the agent
    sees one account of what a step receives.

    A step that FAILED produces nothing, which used to make everything
    downstream of it unresolvable — so a pipeline with two renamed columns
    reported only the first, and every recorded model fixed that one and
    stopped (`docs/repair-evaluation.md`, `two-faults`). Where the steps in
    between only pass columns through, the columns are carried down from
    the nearest ancestor that did resolve, so the second fault is visible
    while the first is still broken.
    """
    try:
        from revoluzen_flow.ai.tools.inspect_step_columns import _preview

        consumers = {c.from_step for c in (workflow.connections or [])}
        leaves = [s.id for s in workflow.steps if s.id not in consumers]
        previews = _preview(workflow, leaves, data_dir, app_state)
    except Exception:
        return None
    if not previews:
        return None

    produced = {
        p["step_id"]: [c["name"] for c in (p.get("columns") or [])]
        for p in previews
    }
    by_id = {s.id: s for s in workflow.steps}
    parents: dict[str, list[str]] = {}
    for conn in (workflow.connections or []):
        parents.setdefault(conn.to_step, []).append(conn.from_step)

    def emits(step_id: str, seen: frozenset[str]) -> list[str]:
        """What this step puts out, by preview or by passing input through."""
        ran = produced.get(step_id)
        if ran:
            return ran
        step = by_id.get(step_id)
        if step is None or step_id in seen:
            return []
        if _kind_of(step) not in _COLUMN_PRESERVING:
            # It reshapes the schema and we could not see the result, so
            # nothing downstream can be checked against a guess.
            return []
        return _merge(parents.get(step_id, []), seen | {step_id})

    def _merge(step_ids: list[str], seen: frozenset[str]) -> list[str]:
        names: list[str] = []
        for sid in step_ids:
            for name in emits(sid, seen):
                if name not in names:
                    names.append(name)
        return names

    incoming: dict[str, list[str]] = {}
    for step in workflow.steps:
        names = _merge(parents.get(step.id, []), frozenset({step.id}))
        if names:
            incoming[step.id] = names
    return incoming


def _column_errors(workflow: Any, incoming: dict[str, list[str]]) -> list[dict[str, Any]]:
    """A reference to a column the step's input does not carry."""
    errors: list[dict[str, Any]] = []
    for step in workflow.steps:
        available = incoming.get(step.id)
        # No resolved schema for this step's input: say nothing rather than
        # report every reference it makes as missing.
        if not available:
            continue
        missing = missing_columns(getattr(step, "params", {}) or {}, available)
        for name in missing:
            errors.append({
                "step_id": step.id,
                "severity": "error",
                "message": (
                    f"Step {step.label or step.id!r} refers to column {name!r}, "
                    f"which its input does not have. Available: "
                    f"{', '.join(available[:20])}."
                ),
            })
    return errors


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    # Default to the user's selection / single-visible pipeline so the LLM
    # doesn't need to thread an explicit ID when the user says "validate
    # this one". Mirrors summarize_pipeline's resolution rule.
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    draft_id = (inputs.get("draft_id") or "").strip()

    def answer(**rest: Any) -> dict[str, Any]:
        base = {
            "pipeline_id": pipeline_id,
            "draft_id": draft_id,
            "pipeline_name": "",
            "valid": False,
            "error_count": 0,
            "errors": [],
            "columns_checked": False,
            "message": "",
        }
        base.update(rest)
        return base

    try:
        from revoluzen_flow.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
        data_dir = app_state.get("data_dir")
    except Exception:
        store, data_dir, app_state = None, None, None

    if store is None:
        return answer(message="Workflow store unavailable; cannot validate.")

    workspace_id = ctx.workspace_id or "default"
    workflow = None

    if draft_id:
        # Validate what the agent is about to ask someone to apply. The
        # draft names the pipeline it modifies, and that pipeline is
        # fetched through the workspace, so a draft id cannot reach one
        # this caller could not open anyway.
        ir = get_draft(draft_id)
        if ir is None:
            return answer(message=f"Draft {draft_id!r} no longer exists. Make the edit again.")
        pipeline_id = pipeline_id or ir.get("_modification_of") or ""
        from revoluzen_flow.ir.schema import Workflow

        payload = {k: v for k, v in ir.items() if not k.startswith("_")}
        payload["workspace_id"] = workspace_id
        payload.setdefault("name", "Draft")
        try:
            workflow = Workflow(**payload)
        except Exception as exc:
            return answer(
                error_count=1,
                errors=[{"step_id": "", "severity": "error",
                         "message": f"The draft is not a valid pipeline: {exc}"}],
                message="The draft does not load as a pipeline.",
            )
    else:
        if not pipeline_id:
            raise ValueError(
                "pipeline_id or draft_id is required (no pipeline selected on the current page)"
            )
        try:
            wv = store.get(pipeline_id, workspace_id=workspace_id)
        except TypeError:
            wv = store.get(pipeline_id)
        if wv is None or wv.workflow is None:
            return answer(message=f"Pipeline {pipeline_id!r} not found in this workspace.")
        workflow = wv.workflow

    # Reuse the canonical IR validator — same one the executor invokes
    # under safety_mode=dry_run/validate_only. Keeping a single source of
    # truth means the agent's answer can never diverge from what Publish
    # or Run would report.
    try:
        from revoluzen_flow.ir.validator import validate_workflow as _validate
        raw_errors = _validate(workflow) or []
    except Exception as exc:
        return answer(
            error_count=1,
            errors=[{"step_id": "", "severity": "error", "message": f"Validator crashed: {exc}"}],
        )

    errors_out: list[dict[str, Any]] = []
    for e in raw_errors:
        errors_out.append({
            "step_id": getattr(e, "step_id", "") or "",
            "severity": getattr(e, "severity", "error") or "error",
            "message": getattr(e, "message", str(e)),
        })

    # The column pass. Shape being right is not the same as being runnable,
    # and a renamed column passes every structural check there is.
    columns_checked = False
    if data_dir:
        incoming = _upstream_columns(workflow, data_dir, app_state)
        if incoming:
            columns_checked = True
            errors_out.extend(_column_errors(workflow, incoming))

    message = ""
    if not columns_checked:
        message = (
            "Structure only: the column check needs to read this pipeline's "
            "sources and could not. A pipeline can pass this and still fail "
            "on the first row."
        )

    return answer(
        pipeline_name=getattr(workflow, "name", "") or "",
        valid=len(errors_out) == 0,
        error_count=len(errors_out),
        errors=errors_out,
        columns_checked=columns_checked,
        message=message,
    )


DEFINITION = ToolDefinition(
    name="validate_pipeline",
    tier=ToolTier.READ,
    description=(
        "Check a saved pipeline, or a DRAFT, and return the structured list "
        "of errors (empty list = valid). Two passes: the same IR validator "
        "Publish and Run gate on, plus a column check that reports a step "
        "referring to a column its input does not have. "
        "Use this when the user asks 'what's wrong with this pipeline?', "
        "'is it ready to publish?', or any variant — and pass draft_id after "
        "an edit, so you check the version you are about to ask them to "
        "apply rather than the saved one without your change. "
        "Read `columns_checked`: when it is false only the structure was "
        "checked, and the pipeline can still fail on the first row. "
        "A pipeline often has MORE THAN ONE fault and this reports all of "
        "them it can see — fix every error listed, chaining the edits with "
        "draft_id, and validate the draft again before telling the user it "
        "is fixed. Do not stop at the first one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Pipeline UUID. Optional when the user has a single pipeline selected/open — the tool will fall back to ctx.selected_ids / ctx.visible_ids.",
            },
            "draft_id": {
                "type": "string",
                "description": "Validate this draft instead of the saved pipeline. Use the draft_id your last edit returned.",
            },
        },
        "required": [],
    },
    output_schema={
        "pipeline_id": "str",
        "draft_id": "str",
        "pipeline_name": "str",
        "valid": "bool",
        "error_count": "int",
        "errors": "list",
        "columns_checked": "bool",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "validation"],
)
