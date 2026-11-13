"""
get_pipeline_spec tool — read-only.

What a pipeline was asked to do: its goal, who answers for it, who uses its
output, where its data comes from, the acceptance criteria and the outcome it
should bring, plus how each criterion is checked and the last verdict on the
current version. The Copilot reads this before changing a pipeline so a repair
keeps the promise the pipeline was built to, and to answer "what is this
pipeline for?" from the requester's words rather than from its steps.

Sends no row values: only the spec as a person wrote it, check names and
settings, and pass/fail counts (docs/ai-boundary-contract.md §2).
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier


def _app_state() -> dict:
    try:
        from revoluzen_flow.main import app_state  # type: ignore
        return app_state
    except Exception:  # noqa: BLE001
        return {}


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from revoluzen_flow.ir.spec import SPEC_CHECKS, spec_digest

    pipeline_id = inputs.get("pipeline_id", "")
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline selected on the current page)")

    state = _app_state()
    store = state.get("workflow_store") or state.get("store")
    if store is None:
        return {"has_spec": False, "message": "Pipeline store unavailable; the spec was not loaded."}
    wv = store.get(pipeline_id, workspace_id=ctx.workspace_id or "default")
    if wv is None or getattr(wv, "workflow", None) is None:
        return {"has_spec": False, "message": f"Pipeline {pipeline_id!r} not found in this workspace."}
    wf = wv.workflow
    spec = wf.spec

    contract = None
    contract_store = state.get("contract_store")
    if contract_store is not None:
        try:
            contract = contract_store.get_contract(wf.id)
        except Exception:  # noqa: BLE001 — the spec is still worth returning
            contract = None
    in_contract = {g.criterion for g in (contract.guarantees if contract else []) if g.criterion}

    criteria: list[dict[str, Any]] = []
    for c in (spec.acceptance if spec else []):
        meta = SPEC_CHECKS.get(c.check)
        settings = {
            k: v for k, v in c.model_dump(exclude={"id", "text", "check"}).items()
            if v not in (None, "", [])
        }
        criteria.append({
            "id": c.id,
            "text": c.text,
            "checked_by": (meta["label"] if meta else "a person"),
            "check": c.check or None,
            "settings": settings,
            "in_contract": c.id in in_contract,
            "missing": c.missing(),
        })

    verdict = None
    if contract is not None and contract_store is not None:
        try:
            from revoluzen_flow.contracts.gate import last_verdict_for
            verdict = last_verdict_for(contract_store, wf.id, wf, contract)
        except Exception:  # noqa: BLE001
            verdict = None

    return {
        "pipeline": wf.name,
        "has_spec": spec is not None,
        "goal": wf.business_purpose,
        "business_owner": spec.business_owner if spec else "",
        "consumers": spec.consumers if spec else "",
        "sources": list(spec.sources) if spec else [],
        "outcome": spec.outcome if spec else "",
        "criteria": criteria,
        "spec_version": getattr(wv, "version", None),
        "contract": None if contract is None else {
            "guarantees": len(contract.guarantees),
            "from_spec": len(in_contract),
            "behind_spec": bool(contract.spec_digest)
                and contract.spec_digest != spec_digest(wf.business_purpose, spec),
        },
        "last_verdict": None if verdict is None else {
            "verdict": verdict["verdict"],
            "failed": verdict["failed"],
            "run_id": verdict["run_id"],
        },
    }


DEFINITION = ToolDefinition(
    name="get_pipeline_spec",
    tier=ToolTier.READ,
    description=(
        "Get what a pipeline was asked to do: its goal, business owner, who uses "
        "the output, its data sources in the requester's words, the acceptance "
        "criteria (and whether each is checked by the pipeline's contract or by a "
        "person), the expected business outcome, and the last contract verdict on "
        "the current version. Read this before changing a pipeline, so a fix keeps "
        "its acceptance criteria, and to answer 'what is this pipeline for?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "The pipeline id. Defaults to the open or selected pipeline.",
            },
        },
        "required": [],
    },
    output_schema={
        "has_spec": "bool",
        "goal": "str",
        "criteria": "list",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "spec"],
)
