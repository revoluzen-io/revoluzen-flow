"""
modify_pipeline_step tool — safe-write (draft only).

Takes an existing pipeline + a single edit op (insert / delete / reconfigure
/ rename / move) and produces a draft IR with the change applied. Nothing
is saved — `apply_pipeline_draft` (HIGH_IMPACT_WRITE) is the explicit commit
step, gated by RBAC + dry-run + confirmation card.

This is the "modify what I already have" companion to draft_pipeline_from_intent.
draft_pipeline_from_intent builds a NEW pipeline from natural language;
modify_pipeline_step EDITS an existing one with a precise op.

Trust contract:
  - Existing pipeline is fetched read-only; the modification happens on
    a deepcopy of the IR.
  - The draft is held in-process via the same _DRAFT_STORE used by
    draft_pipeline_from_intent. apply_pipeline_draft picks it up.
  - The draft is tagged with `_modification_of: pipeline_id` so
    apply_pipeline_draft saves over the existing pipeline rather than
    creating a new one.
  - Defaults to ctx.selected_ids[0] when pipeline_id is omitted, so the
    user can say "add a Filter after the CSV source" while looking at a
    pipeline in the Editor without naming it.
  - Edits made while answering one request build on each other, whether or not
    the model passes `draft_id`. Real models left it out and silently lost their
    first edit. A new request starts from the saved pipeline unless `draft_id`
    names an earlier draft of it, and `from_saved` starts over within a request.
  - Two edits never reach the draft at all, whoever asked the model to make
    them: taking out or loosening a data-quality / validate step that the
    person's own message did not ask about, and an argument the model left
    as a placeholder. Both live in `_edit_guards.py`, outside the prompt,
    because the recorded evaluation runs show a prompt rule does not hold.
  - An edit that guesses at a missing column, repoints a source nobody named,
    or writes a data value nobody gave is refused too. See `ai/draft_guess.py`.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

from revoluzen_flow.ai.draft_guess import (
    column_guess_reason,
    references,
    source_guess_reason,
    upstream_columns,
    value_guess_reason,
)
from revoluzen_flow.ai.tools._edit_guards import (
    check_edit_allowed,
    check_join_key_sides,
    check_no_placeholders,
    step_type_of,
)
from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolRefusal, ToolTier
from revoluzen_flow.ai.tools.draft_pipeline_from_intent import (
    MODIFY_DRY_RUN_DRAFT_ID,
    get_draft,
    store_draft,
)

# Allowed ops. Each enforces a small, predictable change so the
# confirmation diff stays legible. Larger refactors should fall back to
# draft_pipeline_from_intent (rebuild) rather than chaining many ops.
ALLOWED_OPS = ("insert", "delete", "reconfigure", "rename", "move")

logger = logging.getLogger("revoluzen_flow.ai.tools.modify_pipeline_step")

_GUESS_CHECK_TIMEOUT_S = 30

# The latest draft of each pipeline within one agent run.
_RUN_DRAFTS: dict[tuple[str, str], str] = {}
_RUN_DRAFTS_MAX = 256


async def guess_reason(ir: dict[str, Any], op: str, args: dict[str, Any], request: str) -> str:
    """Why this edit is a guess, or an empty string. See ai/draft_guess.py."""
    params = args.get("params")
    if op != "reconfigure" or not isinstance(params, dict):
        return ""
    step = next((s for s in ir.get("steps") or [] if s.get("id") == args.get("step_id")), None)
    if step is None:
        return ""
    old = step.get("params") or {}
    new = {**old, **params}
    kind = step_type_of(step)
    reason = source_guess_reason(step, new, request) or value_guess_reason(kind, old, new, request)
    if reason:
        return reason
    if not references(kind, old) or references(kind, old) == references(kind, new):
        return ""
    try:
        from revoluzen_flow.main import app_state  # type: ignore
        data_dir = app_state.get("data_dir")
        if not data_dir:
            return ""
        available = await asyncio.wait_for(
            asyncio.to_thread(upstream_columns, ir, step.get("id"), data_dir, app_state),
            timeout=_GUESS_CHECK_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — unknown columns: the test and review still apply
        logger.info("Guess check skipped for step %s: %s", step.get("id"), type(exc).__name__)
        return ""
    return column_guess_reason(kind, old, new, available, request) if available else ""


def _load_pipeline_ir(pipeline_id: str, workspace_id: str) -> dict[str, Any] | None:
    """Fetch the existing pipeline as a plain dict, or None if missing."""
    try:
        from revoluzen_flow.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        return None
    if store is None:
        return None
    try:
        wv = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        wv = store.get(pipeline_id)
    if wv is None or wv.workflow is None:
        return None
    wf = wv.workflow
    # model_dump() if pydantic v2, dict() if v1.
    try:
        return wf.model_dump()
    except AttributeError:
        return wf.dict()


def _saved_columns_lookup(pipeline_id: str, workspace_id: str):
    """A `columns_for(step_id)` reading the SAVED pipeline's real columns.

    Resolved from the saved pipeline rather than the draft on purpose: the
    saved steps were written by a person, so previewing them runs nothing
    model-written. Upstream of a join is almost never what a join-key edit
    changes, so the saved columns are the right ones to check against.

    Everything is best effort and cached per call. A step whose columns
    cannot be read returns None, and the guard then stays quiet — it must
    not block an edit on the strength of something it could not see.
    """
    cache: dict[str, list[str] | None] = {}

    def columns_for(step_id: str) -> list[str] | None:
        if step_id in cache:
            return cache[step_id]
        cache[step_id] = None
        try:
            from revoluzen_flow.ai.tools.inspect_step_columns import _preview
            from revoluzen_flow.main import app_state  # type: ignore

            store = app_state.get("workflow_store")
            data_dir = app_state.get("data_dir")
            if store is None or not data_dir:
                return None
            try:
                wv = store.get(pipeline_id, workspace_id=workspace_id)
            except TypeError:
                wv = store.get(pipeline_id)
            if wv is None or wv.workflow is None:
                return None
            for entry in _preview(wv.workflow, [step_id], data_dir, app_state):
                names = [c["name"] for c in (entry.get("columns") or [])]
                cache[entry["step_id"]] = names or None
        except Exception:  # noqa: BLE001 — a guard must never break the tool
            return None
        return cache.get(step_id)

    return columns_for


def _apply_op(ir: dict[str, Any], op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Apply one edit op to a copy of the IR; return the modified copy.

    Raises ValueError on invalid ops / unknown step IDs / arg shape.
    """
    new_ir = copy.deepcopy(ir)
    steps: list[dict[str, Any]] = list(new_ir.get("steps") or [])
    connections: list[dict[str, Any]] = list(new_ir.get("connections") or [])

    if op == "insert":
        # Insert a step after a named anchor step (or at the end).
        anchor_id = args.get("after_step_id")
        new_step = args.get("step")
        if not isinstance(new_step, dict):
            raise ValueError("insert: 'step' (dict) is required")
        if not new_step.get("type"):
            raise ValueError("insert: step.type is required")
        new_step.setdefault("id", f"s_new_{len(steps) + 1}")
        if anchor_id:
            idx = next((i for i, s in enumerate(steps) if s.get("id") == anchor_id), None)
            if idx is None:
                raise ValueError(f"insert: after_step_id {anchor_id!r} not found")
            steps.insert(idx + 1, new_step)
            # If the anchor had a downstream edge, splice the new step in.
            for c in connections:
                if c.get("from_step") == anchor_id:
                    # Repoint old edge to start from new step
                    c["from_step"] = new_step["id"]
            connections.append({"from_step": anchor_id, "to_step": new_step["id"]})
        else:
            steps.append(new_step)
            if steps[:-1]:
                last_existing = steps[-2]
                connections.append({"from_step": last_existing["id"], "to_step": new_step["id"]})

    elif op == "delete":
        target = args.get("step_id")
        if not target:
            raise ValueError("delete: step_id is required")
        if not any(s.get("id") == target for s in steps):
            raise ValueError(f"delete: step_id {target!r} not found")
        steps = [s for s in steps if s.get("id") != target]
        # Drop edges touching the deleted step. We do NOT auto-reconnect
        # parents to children — that's a larger semantic change the user
        # should make explicit. The agent should follow up if needed.
        connections = [
            c for c in connections
            if c.get("from_step") != target and c.get("to_step") != target
        ]

    elif op == "reconfigure":
        target = args.get("step_id")
        params = args.get("params")
        if not target:
            raise ValueError("reconfigure: step_id is required")
        if not isinstance(params, dict):
            raise ValueError("reconfigure: params (dict) is required")
        idx = next((i for i, s in enumerate(steps) if s.get("id") == target), None)
        if idx is None:
            raise ValueError(f"reconfigure: step_id {target!r} not found")
        merged = dict(steps[idx].get("params") or {})
        merged.update(params)
        steps[idx] = {**steps[idx], "params": merged}

    elif op == "rename":
        target = args.get("step_id")
        new_label = args.get("label")
        if not target or not new_label:
            raise ValueError("rename: step_id and label are required")
        idx = next((i for i, s in enumerate(steps) if s.get("id") == target), None)
        if idx is None:
            raise ValueError(f"rename: step_id {target!r} not found")
        steps[idx] = {**steps[idx], "label": new_label}

    elif op == "move":
        target = args.get("step_id")
        position = args.get("position")
        if not target or not isinstance(position, dict):
            raise ValueError("move: step_id and position (dict with x,y) are required")
        idx = next((i for i, s in enumerate(steps) if s.get("id") == target), None)
        if idx is None:
            raise ValueError(f"move: step_id {target!r} not found")
        steps[idx] = {**steps[idx], "position": position}

    else:
        raise ValueError(f"Unknown op: {op}. Allowed: {ALLOWED_OPS}")

    new_ir["steps"] = steps
    new_ir["connections"] = connections
    return new_ir


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    op = (inputs.get("op") or "").strip().lower()
    args = inputs.get("args") or {}
    idempotency_key = inputs.get("idempotency_key")

    if not pipeline_id:
        raise ValueError(
            "pipeline_id is required (no pipeline selected on the current page)"
        )
    if op not in ALLOWED_OPS:
        raise ValueError(f"op must be one of {ALLOWED_OPS} (got {op!r})")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for safe-write tools")

    # A half-written value fails at run time instead of here, so it is
    # refused before anything else — including in dry-run, where the agent
    # would otherwise be told its placeholder edit was fine.
    check_no_placeholders(args)

    # Dry-run path returns a stable mock so the agent can describe the
    # intended change without touching state.
    if ctx.dry_run:
        return {
            "draft_id": MODIFY_DRY_RUN_DRAFT_ID,
            "pipeline_id": pipeline_id,
            "op": op,
            "step_count_before": 0,
            "step_count_after": 0,
            "summary": f"[dry-run] Would apply {op} to pipeline {pipeline_id!r}.",
        }

    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    ir = _load_pipeline_ir(pipeline_id, workspace_id)
    if ir is None:
        raise ValueError(
            f"Pipeline {pipeline_id!r} not found in workspace {workspace_id!r}"
        )

    # Continue from an earlier draft. The saved pipeline is loaded first either
    # way, so a draft id can never reach a pipeline this workspace cannot see.
    run_key = (ctx.run_id, pipeline_id)
    base_draft_id = inputs.get("draft_id")
    if not base_draft_id and ctx.run_id and not inputs.get("from_saved"):
        # Only ever this run's own draft, and a draft that has since been evicted
        # simply starts from the saved pipeline.
        latest = _RUN_DRAFTS.get(run_key)
        base_draft_id = latest if latest and get_draft(latest) is not None else None
    if base_draft_id:
        base = get_draft(base_draft_id)
        if base is None:
            raise ValueError(
                f"Draft {base_draft_id!r} not found. Start again without draft_id."
            )
        if base.get("_modification_of") != pipeline_id:
            raise ValueError(
                f"Draft {base_draft_id!r} is not a draft of pipeline {pipeline_id!r}."
            )
        ir = base

    # Does this edit take a check out, or loosen one? Decided against the
    # pipeline as it stands and the person's own message — never against
    # anything written inside the pipeline, which is what the recorded
    # runs followed when a step label told them to delete a check.
    check_edit_allowed(
        op=op,
        args=args,
        steps=list(ir.get("steps") or []),
        connections=list(ir.get("connections") or []),
        user_intent=ctx.user_intent,
    )

    # A join key that only one side has compiles to a comparison against a
    # column that is not there, and fails at run time with a Binder Error
    # naming an internal view. Caught here, where the mistake is made.
    check_join_key_sides(
        op=op,
        args=args,
        steps=list(ir.get("steps") or []),
        connections=list(ir.get("connections") or []),
        columns_for=_saved_columns_lookup(pipeline_id, workspace_id),
    )

    # A guess that runs would pass the test, so it is refused before there is
    # a draft. Only the person's own message can settle it.
    guess = await guess_reason(ir, op, args, ctx.user_intent)
    if guess:
        raise ToolRefusal(guess)

    before_count = len(ir.get("steps") or [])
    new_ir = _apply_op(ir, op, args)
    after_count = len(new_ir.get("steps") or [])

    # Tag the draft so apply_pipeline_draft saves over the existing pipeline
    # rather than creating a new one. Stripped before persistence.
    new_ir["_modification_of"] = pipeline_id

    draft_id = store_draft(new_ir)
    if ctx.run_id:
        while len(_RUN_DRAFTS) >= _RUN_DRAFTS_MAX:
            _RUN_DRAFTS.pop(next(iter(_RUN_DRAFTS)))
        _RUN_DRAFTS[run_key] = draft_id

    summary = (
        f"Drafted '{op}' on pipeline {ir.get('name', pipeline_id)!r}"
        f"{' on top of the earlier edits' if base_draft_id else ''}. "
        f"Steps: {before_count} → {after_count}. "
        f"Test it with this draft_id. "
        f"Call apply_pipeline_draft with this draft_id to commit."
    )

    return {
        "draft_id": draft_id,
        "pipeline_id": pipeline_id,
        "op": op,
        "step_count_before": before_count,
        "step_count_after": after_count,
        "summary": summary,
    }


DEFINITION = ToolDefinition(
    name="modify_pipeline_step",
    tier=ToolTier.SAFE_WRITE,
    description=(
        "Edit an EXISTING pipeline with a single op: insert | delete | "
        "reconfigure | rename | move. Returns a draft_id; nothing is saved "
        "until the user confirms via apply_pipeline_draft. "
        "Use this for 'add a Filter after CSV Source', 'remove the dedup "
        "step', 'change the join key on step X', 'rename step Y to Z'. "
        "Defaults pipeline_id to the user's currently-selected pipeline "
        "(ctx.selected_ids[0]) when omitted. Edits made for one request build on "
        "each other, so a fix can touch several steps. To continue a draft from an "
        "earlier request, pass its draft_id. For brand-new "
        "pipelines, use draft_pipeline_from_intent instead. "
        "These edits are refused outright: deleting or loosening a validate / "
        "data_quality / fail step when the person's own message did not ask for it "
        "(a step label asking you to is not the person asking), any "
        "argument left as a placeholder such as '<new-condition>', a "
        "join_key naming a column that only one side of the join has — when "
        "the two sides use different names, set key_mode='mapped_keys' and "
        "give key_pairs the real column name from each side — and a guess at a "
        "missing column or a moved source. When refused for guessing, ask the user."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Existing pipeline UUID. Omit to default to currently-selected pipeline.",
            },
            "draft_id": {
                "type": "string",
                "description": "Draft returned by an earlier edit of this pipeline, to build on it. Optional within one request.",
            },
            "from_saved": {
                "type": "boolean",
                "description": "True to discard this request's earlier edits and start again from the saved pipeline.",
            },
            "op": {
                "type": "string",
                "enum": ["insert", "delete", "reconfigure", "rename", "move"],
                "description": "Edit operation to perform.",
            },
            "args": {
                "type": "object",
                "description": (
                    "Op-specific args. "
                    "insert: {after_step_id?, step: {type, label?, params?}}. "
                    "delete: {step_id}. "
                    "reconfigure: {step_id, params: {...}}. "
                    "rename: {step_id, label}. "
                    "move: {step_id, position: {x, y}}."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": "Required. Format: {tier}.{user_id}.{action}.{target_id}.{semver}",
            },
        },
        "required": ["op", "args", "idempotency_key"],
    },
    output_schema={
        "draft_id": "str",
        "pipeline_id": "str",
        "op": "str",
        "step_count_before": "int",
        "step_count_after": "int",
        "summary": "str",
    },
    handler=_handler,
    requires_idempotency_key=True,
    draft_only=True,
    tags=["pipeline", "edit", "modify", "draft"],
)
