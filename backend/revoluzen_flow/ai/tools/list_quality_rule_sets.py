"""
list_quality_rule_sets tool — read-only.

The workspace's shared quality rule sets: named Data Quality rules many
pipelines use. When the Copilot drafts a Data Quality step that checks what
a set already checks, it can pick the set (the step's ``rule_sets`` setting,
a list of set ids) instead of typing the rules again, so a later fix to the
set reaches this pipeline too.

Sends no row values: set names, descriptions and rules (a column, a check
and the value the check compares with, all written by a person).
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier

_RULES_SHOWN = 25


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        from revoluzen_flow.main import app_state  # type: ignore
    except Exception:  # noqa: BLE001
        app_state = {}
    store = app_state.get("quality_rule_set_store")
    if store is None:
        return {"rule_sets": [], "message": "Shared rule sets are unavailable on this install."}
    workspace_id = ctx.workspace_id or ctx.tenant_id or "default"
    sets = store.list(workspace_id)
    if not sets:
        return {
            "rule_sets": [],
            "message": "This workspace has no shared rule sets. A Data Quality step's own rules can be "
                       "saved as one from the step's settings.",
        }
    return {
        "rule_sets": [
            {
                "id": rs.id,
                "name": rs.name,
                "description": rs.description,
                "version": rs.version,
                "columns": sorted({str(r.get("column")) for r in rs.rules}),
                "rules": rs.rules[:_RULES_SHOWN],
                "rule_count": len(rs.rules),
            }
            for rs in sets
        ],
        "how_to_use": "Set a data_quality step's params.rule_sets to a list of these ids. The step's input "
                      "must have every column a set checks.",
    }


DEFINITION = ToolDefinition(
    name="list_quality_rule_sets",
    tier=ToolTier.READ,
    description=(
        "List the workspace's shared quality rule sets: named Data Quality rules "
        "that many pipelines use, with the columns each checks. Use it before "
        "writing Data Quality rules: when a set already checks those columns, put "
        "its id in the step's rule_sets setting instead of retyping the rules."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    output_schema={},
    handler=_handler,
    requires_idempotency_key=False,
    tags=["quality", "read", "rules"],
)
