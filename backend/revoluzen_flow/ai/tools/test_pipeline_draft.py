"""
test_pipeline_draft tool — read-only.

Runs a draft edit on sample data and reports what happened, so the agent can
see whether its fix worked and find the NEXT fault, instead of handing a
person an untested guess.

This closes the second finding in `docs/repair-evaluation.md`: models stop
after the first fix. The engine error names only the first broken step, and
until now the agent had no way to run its own draft, so the `two-faults`
case was never completed by any recorded model. With this, the loop is
edit → test → read the next error → edit again.

Trust contract:
  - The draft is model-written and this tool executes it, so
    `ai/draft_checks.py` decides first. If it refuses, nothing runs.
  - Sinks are never run. Nothing is written to any destination, and the
    saved pipeline is never touched — the draft executes under its own id.
  - Column names, types and row counts only. Row VALUES never leave here.
  - The verdict is recorded against a hash of the draft, so an answer
    claiming "the fix works" can be checked against a run that actually
    happened and against that exact draft. A model cannot supply its own.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import Any

from revoluzen_flow.ai.draft_checks import _all_strings, is_sink, step_kind, unsafe_to_test_reason
from revoluzen_flow.ai.step_params import guide_for
from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier
from revoluzen_flow.ai.tools.draft_pipeline_from_intent import get_draft

_TIMEOUT_S = 90
# Rows are read so a computed column is evaluated, and then discarded. Reading
# only a few would leave a bad value further down the sample unseen.
_PREVIEW_ROWS = 1000
_MAX_COLUMNS = 50
# A plain CAST passes on the values in the sample, then fails on the first new one.
_PLAIN_CAST = re.compile(r"(?<![a-z_])cast\s*\(", re.IGNORECASE)
_EVIDENCE_MAX = 64

PASSED = "runs"
FAILED = "failed"
NOT_RUN = "not_run"

# Verdicts by draft id. Same process and lifetime as the draft store.
_EVIDENCE: dict[str, dict[str, Any]] = {}


def get_test_evidence(draft_id: str) -> dict[str, Any] | None:
    """The recorded result of testing this draft, if it was tested unchanged.

    Keyed on a hash of the draft's steps and connections, so editing a draft
    after testing it invalidates the evidence rather than carrying a stale
    pass forward.
    """
    evidence = _EVIDENCE.get(draft_id)
    draft = get_draft(draft_id)
    if evidence is None or draft is None:
        return None
    if evidence.get("draft_hash") != _draft_hash(draft):
        return None
    return evidence


def _draft_hash(ir: dict[str, Any]) -> str:
    return json.dumps(
        {"steps": ir.get("steps") or [], "connections": ir.get("connections") or []},
        sort_keys=True, default=str,
    )


def _workflow_from(ir: dict[str, Any], pipeline_id: str, workspace_id: str) -> Any:
    from revoluzen_flow.ir.schema import Workflow

    payload = {k: v for k, v in copy.deepcopy(ir).items() if not k.startswith("_")}
    # A separate id keeps the draft's step cache away from the saved
    # pipeline's, so testing a draft cannot poison the real pipeline's cache.
    payload["id"] = f"{pipeline_id}-draft"
    payload["workspace_id"] = workspace_id
    return Workflow(**payload)


def _without_sinks(workflow: Any) -> Any:
    kept = copy.deepcopy(workflow)
    sinks = {s.id for s in kept.steps if is_sink({"type": s.type})}
    kept.steps = [s for s in kept.steps if s.id not in sinks]
    kept.connections = [
        c for c in kept.connections
        if c.to_step not in sinks and c.from_step not in sinks
    ]
    return kept


def _targets(workflow: Any) -> list[str]:
    """Steps nothing else reads from, so running them runs everything."""
    consumers = {c.from_step for c in workflow.connections}
    return [s.id for s in workflow.steps if s.id not in consumers]


def _clean_error(error: str | None, data_dir: str) -> str:
    # An engine error can carry the install's absolute paths.
    text = (error or "").replace(str(data_dir), "<data>")
    return text.replace(str(data_dir).replace("\\", "/"), "<data>")[:300]


def _run_draft(workflow: Any, data_dir: str, app_state: Any) -> dict[str, Any]:
    from revoluzen_flow.engine.executor import WorkflowExecutor

    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
    runnable = _without_sinks(workflow)
    labels = {s.id: s.label or s.id for s in workflow.steps}
    kinds = {s.id: str(getattr(s.type, "value", s.type)) for s in workflow.steps}
    steps: dict[str, dict[str, Any]] = {}
    failing: dict[str, str] = {}
    for target in _targets(runnable):
        trace = executor.execute_step_trace(runnable, target, preview_limit=_PREVIEW_ROWS)
        for step_id, result in trace.step_results.items():
            error = _clean_error(result.error, data_dir)
            if result.status == "error" and error.startswith("Dependency "):
                # The real failure is recorded against the step that failed.
                continue
            # A relation is lazy. A step can build and then fail when its rows are
            # read, which the run reports as success. Only the first such step is
            # the cause: everything downstream of it fails the same way.
            read_error = _clean_error(result.read_error, data_dir)
            status = "error" if read_error else result.status
            steps[step_id] = {
                "step_id": step_id,
                "label": labels.get(step_id, step_id),
                "status": status,
                "columns": list(result.columns or [])[:_MAX_COLUMNS],
                "row_count": max(result.row_count or 0, 0),
            }
            if result.status == "error":
                failing.setdefault(step_id, error)
            elif read_error and not failing:
                failing[step_id] = read_error
    # A step that runs but returns nothing is the quiet failure this whole
    # evaluation exists to catch, so it is reported even on a pass.
    empty = [
        target for target in _targets(runnable)
        if target in steps
        and steps[target]["status"] == "success"
        and not steps[target]["row_count"]
    ]
    return {
        "steps": list(steps.values()),
        "failures": [
            {"step_id": sid, "error": err, "step_params": guide_for([kinds.get(sid, "")])}
            for sid, err in failing.items()
        ],
        "empty_outputs": empty,
    }


def _warnings(draft: dict[str, Any], saved_steps: list[dict[str, Any]]) -> list[str]:
    """Risks a passing run cannot show, in what this draft changed."""
    saved = {s.get("id"): s.get("params") or {} for s in saved_steps}
    out: list[str] = []
    for step in draft.get("steps") or []:
        params = step.get("params") or {}
        if params == saved.get(step.get("id")) or step_kind(step) in ("source", "destination"):
            continue
        if any(_PLAIN_CAST.search(text) for text in _all_strings(params)):
            out.append(
                f"Step {step.get('id')!r} uses CAST, which fails the run on any value it cannot convert, "
                "including values not in this sample. Use TRY_CAST if unknown values are possible."
            )
    return out


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    draft_id = (inputs.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id is required: the id an edit of the pipeline returned")

    def answer(verdict: str, message: str, pipeline_id: str = "", **rest: Any) -> dict[str, Any]:
        return {
            "draft_id": draft_id,
            "pipeline_id": pipeline_id,
            "verdict": verdict,
            "message": message,
            "steps": rest.get("steps", []),
            "failures": rest.get("failures", []),
            "empty_outputs": rest.get("empty_outputs", []),
            "warnings": rest.get("warnings", []),
            # This repo has no saved tests, so nothing here ever looks at the output.
            "output_checked": False,
        }

    draft = get_draft(draft_id)
    if draft is None:
        return answer(NOT_RUN, f"Draft {draft_id!r} no longer exists. Make the edit again.")
    pipeline_id = draft.get("_modification_of") or ""
    if not pipeline_id:
        return answer(NOT_RUN, "Only an edit of a saved pipeline can be tested this way.")

    try:
        from revoluzen_flow.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
        data_dir = app_state.get("data_dir")
    except Exception:
        store, data_dir, app_state = None, None, None
    if store is None or not data_dir:
        return answer(
            NOT_RUN,
            "The workflow store is unavailable, so the draft cannot be tested.",
            pipeline_id,
        )

    # Reach the draft only through a pipeline this workspace can see.
    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    try:
        saved = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        saved = store.get(pipeline_id)
    if saved is None or saved.workflow is None:
        return answer(NOT_RUN, f"Pipeline {pipeline_id!r} not found in this workspace.", pipeline_id)

    saved_ir = saved.workflow.model_dump()
    reason = unsafe_to_test_reason(draft.get("steps") or [], saved_ir.get("steps") or [])
    if reason:
        return answer(
            NOT_RUN,
            f"This draft was not run: {reason}. A person needs to review it.",
            pipeline_id,
        )

    workflow = _workflow_from(draft, pipeline_id, workspace_id)

    try:
        run = await asyncio.wait_for(
            asyncio.to_thread(_run_draft, workflow, data_dir, app_state),
            timeout=_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return answer(NOT_RUN, f"Testing the draft took longer than {_TIMEOUT_S} seconds.", pipeline_id)

    verdict = FAILED if run["failures"] else PASSED
    if run["failures"]:
        first = run["failures"][0]
        message = f"The draft still fails at step {first['step_id']!r}: {first['error']}"
    else:
        message = (
            "The draft runs on a sample without an error. Destinations were not run and nothing was saved. "
            "NOTHING checked its output, and you cannot see the values. It may still produce wrong rows. "
            "Tell the user it runs and what you changed, ask them to check the output, and do not say it is fixed."
        )
    warnings = _warnings(draft, saved_ir.get("steps") or [])
    if run["empty_outputs"]:
        message += f" These steps produced no rows: {', '.join(run['empty_outputs'])}."

    while len(_EVIDENCE) >= _EVIDENCE_MAX:
        _EVIDENCE.pop(next(iter(_EVIDENCE)))
    _EVIDENCE[draft_id] = {
        "draft_hash": _draft_hash(draft),
        "verdict": verdict,
        "rows_previewed": max((s["row_count"] for s in run["steps"]), default=0),
        "steps_run": len(run["steps"]),
        "empty_outputs": list(run["empty_outputs"]),
    }
    return answer(
        verdict, message, pipeline_id,
        steps=run["steps"], failures=run["failures"], empty_outputs=run["empty_outputs"],
        warnings=warnings,
    )


DEFINITION = ToolDefinition(
    name="test_pipeline_draft",
    tier=ToolTier.READ,
    description=(
        "Run a draft edit of a pipeline on sample data and report whether it "
        "works: each step's status, columns and row count, and the first "
        "error. Destinations are NOT run, nothing is saved, and no row values "
        "are returned. "
        "Call this after every edit. A pipeline often has more than one fault "
        "and the original error names only the first — if this reports "
        "another, edit again and test again. Verdict 'runs' means no error, not "
        "right rows: output_checked says whether anything looked at the output. "
        "Never call a draft fixed on this alone."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {
                "type": "string",
                "description": "The draft_id the last edit of the pipeline returned.",
            },
        },
        "required": ["draft_id"],
    },
    output_schema={
        "draft_id": "str",
        "pipeline_id": "str",
        "verdict": "str",
        "message": "str",
        "steps": "list",
        "failures": "list",
        "empty_outputs": "list",
        "warnings": "list",
        "output_checked": "bool",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "test", "draft"],
)
