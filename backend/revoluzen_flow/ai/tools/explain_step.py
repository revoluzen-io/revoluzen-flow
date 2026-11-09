"""
explain_step tool — read-only.

Returns the role, the sanitized configuration, AND the options the step
type accepts, for one step inside a saved pipeline. Lets the agent answer
"what does the Filter step do?" / "why is the join keyed on customer_id?"
/ "what file is the Source reading?" without leaking credentials or data.

Why the options are here (docs/repair-evaluation.md, "the join fix needs
the step's options"): on all three recorded model runs the assistant fixed
a renamed join column by writing the new name into `join_key`. That is
wrong — once the two sides use different names the step needs
`key_mode: mapped_keys` and a `key_pairs` entry. The model had no way to
know, because nothing showed it what a Join accepts. `param_schema()` on
the node class says so in one line, and now travels with the step.

Sanitization rules (per docs/ai-boundary-contract.md §2):
  - connection_id and connector_type are returned (they're discovery info,
    not secrets).
  - Anything matching a credential-like key name (password, secret, token,
    api_key, auth_*) is redacted to "***".
  - Free-form params (file_path, query, condition, expression) flow as-is
    so the agent can explain what the step does, but we cap each value
    length to keep token cost predictable.

Every return path returns the whole declared shape. It used to return a
short {found: False, message: ...} dict on the three "cannot answer that"
paths, and `normalize_tool_output` rejected it with "missing required key
step_type" — so a step id that did not exist raised a schema error instead
of saying the step did not exist, and `message` was dropped as an undeclared
extra even on the paths that set it.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier


_CREDENTIAL_HINTS = (
    "password", "secret", "token", "api_key", "apikey",
    "auth_token", "access_key", "private_key", "client_secret",
    "connection_string",  # may embed user:pass
)
_MAX_VALUE_LEN = 600
# A node's full param schema can run to dozens of entries with long
# placeholder text. The agent needs the choices and what they mean, not the
# UI's layout hints, so each option is trimmed and the list is capped.
_MAX_OPTIONS = 40
_MAX_DESC_LEN = 240


def _redact_value(key: str, value: Any) -> Any:
    k = (key or "").lower()
    if any(hint in k for hint in _CREDENTIAL_HINTS):
        return "***"
    if isinstance(value, str) and len(value) > _MAX_VALUE_LEN:
        return value[:_MAX_VALUE_LEN] + f"… (truncated, {len(value)} chars total)"
    return value


def _step_type(s) -> str:
    t = getattr(s, "type", None) or getattr(s, "step_type", None) or "unknown"
    return getattr(t, "value", t) if t else "unknown"


def _blank(pipeline_id: str, step_id: str, message: str) -> dict[str, Any]:
    """The declared shape with nothing in it, plus why.

    Returned instead of a short dict so `normalize_tool_output` passes and
    the agent reads the reason rather than a schema error.
    """
    return {
        "pipeline_id": pipeline_id,
        "step_id": step_id,
        "found": False,
        "step_type": "",
        "label": "",
        "params": {},
        "options": [],
        "upstream_step_ids": [],
        "downstream_step_ids": [],
        "message": message,
    }


def _options_for(step_type: str, current: dict[str, Any]) -> list[dict[str, Any]]:
    """What this step type accepts, and which of those the step has set.

    Read from the node class's own `param_schema()`, the same declaration
    the editor's config panel renders, so the agent and the person are
    looking at one description of the step.
    """
    try:
        from revoluzen_flow.ir.schema import StepType
        from revoluzen_flow.nodes.registry import NodeRegistry

        node_cls = NodeRegistry.get(StepType(step_type))
        schema = node_cls.param_schema() or []
    except Exception:
        # An unregistered or custom type has no declaration to show. The
        # step's own params are still returned, so the agent is no worse
        # off than before this existed.
        return []

    out: list[dict[str, Any]] = []
    for entry in schema[:_MAX_OPTIONS]:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = str(entry["name"])
        option: dict[str, Any] = {
            "name": name,
            "label": entry.get("label") or name,
            "type": entry.get("type") or "",
            # Whether the step sets this today. Lets the agent see that a
            # Join is on `same_key` without inferring it from an absence.
            "set_on_this_step": name in current,
        }
        if entry.get("options"):
            option["choices"] = entry["options"]
        if entry.get("default") not in (None, ""):
            option["default"] = entry["default"]
        desc = entry.get("description")
        if desc:
            text = str(desc)
            option["description"] = (
                text[:_MAX_DESC_LEN] + "…" if len(text) > _MAX_DESC_LEN else text
            )
        out.append(option)
    return out


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pipeline_id = inputs.get("pipeline_id", "")
    step_id = inputs.get("step_id", "")
    if not pipeline_id and ctx.selected_ids:
        pipeline_id = ctx.selected_ids[0]
    if not pipeline_id and ctx.visible_ids and len(ctx.visible_ids) == 1:
        pipeline_id = ctx.visible_ids[0]
    if not pipeline_id:
        raise ValueError("pipeline_id is required (no pipeline selected on the current page)")
    if not step_id:
        raise ValueError("step_id is required")

    try:
        from revoluzen_flow.main import app_state  # type: ignore
        store = app_state.get("workflow_store")
    except Exception:
        store = None

    if store is None:
        return _blank(pipeline_id, step_id, "Workflow store unavailable; cannot inspect.")

    workspace_id = ctx.workspace_id or "default"
    try:
        wv = store.get(pipeline_id, workspace_id=workspace_id)
    except TypeError:
        wv = store.get(pipeline_id)
    if wv is None or wv.workflow is None:
        return _blank(
            pipeline_id, step_id,
            f"Pipeline {pipeline_id!r} not found in this workspace.",
        )

    wf = wv.workflow
    steps = list(getattr(wf, "steps", []) or [])
    target = next((s for s in steps if getattr(s, "id", "") == step_id), None)
    if target is None:
        known = ", ".join(repr(getattr(s, "id", "")) for s in steps[:12]) or "none"
        return _blank(
            pipeline_id, step_id,
            f"Step {step_id!r} is not in pipeline {pipeline_id!r}. "
            f"Steps in this pipeline: {known}.",
        )

    raw_params = getattr(target, "params", {}) or {}
    sanitized_params: dict[str, Any] = {}
    for k, v in raw_params.items():
        sanitized_params[k] = _redact_value(k, v)

    # Upstream / downstream connectivity — useful context for "why is this
    # step here?" / "what feeds into it?" without a second tool call.
    upstream_ids: list[str] = []
    downstream_ids: list[str] = []
    for c in (getattr(wf, "connections", []) or []):
        if getattr(c, "to_step", None) == step_id:
            upstream_ids.append(getattr(c, "from_step", ""))
        if getattr(c, "from_step", None) == step_id:
            downstream_ids.append(getattr(c, "to_step", ""))

    step_type = _step_type(target)
    return {
        "pipeline_id": pipeline_id,
        "step_id": step_id,
        "found": True,
        "step_type": step_type,
        "label": getattr(target, "label", "") or "",
        "params": sanitized_params,
        "options": _options_for(step_type, raw_params),
        "upstream_step_ids": upstream_ids,
        "downstream_step_ids": downstream_ids,
        "message": "",
    }


DEFINITION = ToolDefinition(
    name="explain_step",
    tier=ToolTier.READ,
    description=(
        "Drill into a single step (node) inside a saved pipeline and return "
        "its type, label, sanitized configuration, which steps feed into or "
        "out of it, AND the full list of options that step type accepts — "
        "each with its choices, its default, and what it means. "
        "Use this when the user asks about a specific step, and ALWAYS "
        "before reconfiguring one, so the edit uses a parameter the step "
        "actually has: a Join whose two sides now use different column "
        "names needs key_mode='mapped_keys' and key_pairs, not a new "
        "join_key. Credentials are redacted; large strings are truncated. "
        "Prefer this over summarize_pipeline when the user is asking about "
        "ONE node."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pipeline_id": {
                "type": "string",
                "description": "Pipeline UUID. Optional when a single pipeline is selected/open.",
            },
            "step_id": {
                "type": "string",
                "description": "ID of the step within the pipeline. Required.",
            },
        },
        "required": ["step_id"],
    },
    output_schema={
        "pipeline_id": "str",
        "step_id": "str",
        "found": "bool",
        "step_type": "str",
        "label": "str",
        "params": "dict",
        "options": "list",
        "upstream_step_ids": "list",
        "downstream_step_ids": "list",
        "message": "str",
    },
    handler=_handler,
    requires_idempotency_key=False,
    tags=["pipeline", "read", "drill-down"],
)
