"""
lookup_business_terms tool — read-only.

The workspace's business glossary: its own terms ("active customer") and
metrics ("net revenue"), each with a definition, other names, and the SQL
and columns a person wrote for it. The terms a request mentions are already
put in front of the Copilot; this tool is for looking one up by another
wording, or listing what is defined.

Sends no row values: definitions written by people.
"""

from __future__ import annotations

from typing import Any

from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier

_LIST_LIMIT = 40


async def _handler(inputs: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        from revoluzen_flow.main import app_state  # type: ignore
    except Exception:  # noqa: BLE001
        app_state = {}
    store = app_state.get("glossary_store")
    if store is None:
        return {"terms": [], "message": "The business glossary is unavailable on this install."}
    from revoluzen_flow.glossary.store import match_terms

    terms = store.list(ctx.workspace_id or ctx.tenant_id or "default")
    if not terms:
        return {"terms": [], "message": "This workspace has no business glossary yet (Insights > Glossary)."}
    query = (inputs.get("query") or "").strip()
    if query:
        hits = match_terms(terms, query, limit=_LIST_LIMIT)
        q = query.lower()
        hits += [t for t in terms if t not in hits and (q in t.name.lower() or q in t.definition.lower()
                                                         or any(q in s.lower() for s in t.synonyms))]
        terms = hits
    keep = ("name", "kind", "definition", "synonyms", "sql", "where", "owner")
    return {
        "terms": [{k: getattr(t, k) for k in keep} for t in terms[:_LIST_LIMIT]],
        "count": len(terms),
        "rule": "Use these meanings when drafting or explaining. If one does not fit the data, say so.",
    }


DEFINITION = ToolDefinition(
    name="lookup_business_terms",
    tier=ToolTier.READ,
    description=(
        "Look up the workspace's business glossary: its own terms and metrics "
        "(e.g. 'active customer', 'net revenue') with their definitions, other "
        "names, and the SQL and columns written for them. Use it when a request "
        "uses business words whose meaning you were not given, before drafting."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A word or phrase to look up. Empty lists the glossary."},
        },
        "required": [],
    },
    output_schema="dict",  # the result's keys depend on the request
    handler=_handler,
    requires_idempotency_key=False,
    tags=["glossary", "read", "business"],
)
