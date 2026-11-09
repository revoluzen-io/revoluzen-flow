"""
Initial tool registrations for the Revoluzen Flow agent loop.

Per round-3 reviewer ("start with 4 read-only tools; add write tools only
after Step 1.5b governance is complete"), the initial registry holds:
  - 3 READ tools:        summarize_pipeline, inspect_connections, query_metrics
  - 1 SAFE_WRITE tool:   compose_report (draft-only, idempotency required)

NO HIGH_IMPACT_WRITE tools yet. Those land in Step 4 (create_schedule,
send_to_destination) once governance + confirmation gates exist.

Importing this package auto-registers tools into the default registry and
their output schemas into normalize.py.
"""

from __future__ import annotations

from revoluzen_flow.ai.normalize import register_output_schema
from revoluzen_flow.ai.tools.apply_pipeline_draft import DEFINITION as APPLY_PIPELINE_DRAFT
from revoluzen_flow.ai.tools.base import ToolContext, ToolDefinition, ToolTier
from revoluzen_flow.ai.tools.compose_report import DEFINITION as COMPOSE_REPORT
from revoluzen_flow.ai.tools.draft_alert_rule import DEFINITION as DRAFT_ALERT_RULE
from revoluzen_flow.ai.tools.draft_pipeline_from_intent import DEFINITION as DRAFT_PIPELINE_FROM_INTENT
from revoluzen_flow.ai.tools.draft_connector_from_openapi import DEFINITION as DRAFT_CONNECTOR_FROM_OPENAPI
from revoluzen_flow.ai.tools.draft_connector_from_samples import DEFINITION as DRAFT_CONNECTOR_FROM_SAMPLES
from revoluzen_flow.ai.tools.test_connection import DEFINITION as TEST_CONNECTION
from revoluzen_flow.ai.tools.modify_pipeline_step import DEFINITION as MODIFY_PIPELINE_STEP
from revoluzen_flow.ai.tools.get_installation_health import DEFINITION as GET_INSTALLATION_HEALTH
from revoluzen_flow.ai.tools.get_next_scheduled import DEFINITION as GET_NEXT_SCHEDULED
from revoluzen_flow.ai.tools.get_running_executions import DEFINITION as GET_RUNNING_EXECUTIONS
from revoluzen_flow.ai.tools.get_user_role import DEFINITION as GET_USER_ROLE
from revoluzen_flow.ai.tools.list_catalog import DEFINITION as LIST_CATALOG
from revoluzen_flow.ai.tools.inspect_connections import DEFINITION as INSPECT_CONNECTIONS
from revoluzen_flow.ai.tools.list_alerts import DEFINITION as LIST_ALERTS
from revoluzen_flow.ai.tools.list_executions import DEFINITION as LIST_EXECUTIONS
from revoluzen_flow.ai.tools.list_pipelines import DEFINITION as LIST_PIPELINES
from revoluzen_flow.ai.tools.list_projects import DEFINITION as LIST_PROJECTS
from revoluzen_flow.ai.tools.list_schedules import DEFINITION as LIST_SCHEDULES
from revoluzen_flow.ai.tools.get_security_posture import DEFINITION as GET_SECURITY_POSTURE
from revoluzen_flow.ai.tools.list_steward_findings import DEFINITION as LIST_STEWARD_FINDINGS
from revoluzen_flow.ai.tools.list_storage import DEFINITION as LIST_STORAGE
from revoluzen_flow.ai.tools.list_templates import DEFINITION as LIST_TEMPLATES
from revoluzen_flow.ai.tools.lookup_help_topic import DEFINITION as LOOKUP_HELP_TOPIC
from revoluzen_flow.ai.tools.query_metrics import DEFINITION as QUERY_METRICS
from revoluzen_flow.ai.tools.recall_history import DEFINITION as RECALL_HISTORY
from revoluzen_flow.ai.tools.registry import (
    ToolNotFoundError,
    ToolRegistry,
    default_registry,
    reset_default_registry,
)
from revoluzen_flow.ai.tools.summarize_pipeline import DEFINITION as SUMMARIZE_PIPELINE
from revoluzen_flow.ai.tools.validate_pipeline import DEFINITION as VALIDATE_PIPELINE
from revoluzen_flow.ai.tools.explain_step import DEFINITION as EXPLAIN_STEP
from revoluzen_flow.ai.tools.inspect_step_columns import DEFINITION as INSPECT_STEP_COLUMNS
from revoluzen_flow.ai.tools.test_pipeline_draft import DEFINITION as TEST_PIPELINE_DRAFT
from revoluzen_flow.ai.tools.workspace_overview import DEFINITION as GET_WORKSPACE_OVERVIEW
from revoluzen_flow.ai.tools.web_fetch import DEFINITION as WEB_FETCH
from revoluzen_flow.ai.tools.web_search import DEFINITION as WEB_SEARCH

INITIAL_TOOLS: tuple[ToolDefinition, ...] = (
    # Identity / overview first — common entrypoints for "who am I" / "what's here"
    GET_USER_ROLE,
    GET_WORKSPACE_OVERVIEW,
    # List tools — the agent's main read surface across the Revoluzen Flow data model
    LIST_PIPELINES,
    LIST_PROJECTS,
    LIST_SCHEDULES,
    LIST_ALERTS,
    LIST_EXECUTIONS,
    # Storage inventory (files / managed tables / pipeline outputs).
    # One tool covers all three slices so the agent can answer
    # "what files / tables do I have?" without three separate calls.
    LIST_STORAGE,
    INSPECT_CONNECTIONS,
    # Public catalog awareness — agent can answer "what connectors do
    # you support" / "is Snowflake on Plus" without spelunking source.
    LIST_CATALOG,
    # Templates discovery — built-in starters + user-saved templates.
    # Lets the agent recommend a template when the user describes a
    # use case ("I need to sync Postgres to a warehouse").
    LIST_TEMPLATES,
    # Drill-down + metrics
    SUMMARIZE_PIPELINE,
    VALIDATE_PIPELINE,
    EXPLAIN_STEP,
    # Column names and types a step receives. Added 2026-11-03: the repair
    # evaluation showed the agent could only guess at a renamed column.
    INSPECT_STEP_COLUMNS,
    # Run a draft and report what happened. Added 2026-11-03 for the other
    # half of the same finding: models stopped after one fix because the
    # engine error names only the first broken step and the agent had no
    # way to run its own draft to find the next one.
    TEST_PIPELINE_DRAFT,
    QUERY_METRICS,
    # RAG retrieval — workspace history + docs
    RECALL_HISTORY,
    # Atlas lookup — structured product-knowledge map (pages, glossary,
    # how-tos, tools, nodes, connectors, docs). Added May 17 2026.
    # See backend/revoluzen_flow/ai/atlas/ for the topic catalog.
    LOOKUP_HELP_TOPIC,
    # Operational reads — running-state + upcoming schedule visibility +
    # whole-install health rollup (score + punch list + top failures)
    GET_RUNNING_EXECUTIONS,
    GET_NEXT_SCHEDULED,
    GET_INSTALLATION_HEALTH,
    # Security & Diagnostics baseline — the same rows the Settings page
    # shows. Added 2026-10-26 because the Copilot had no way to see them:
    # asked "why is master key permissions failing", it answered from the
    # model's general knowledge of ACLs rather than from this install.
    GET_SECURITY_POSTURE,
    # Steward advisories — duplicate sources, governance, connector health,
    # user-rule matches (read-only; the only live-fed Steward detectors).
    LIST_STEWARD_FINDINGS,
    # Safe-write (draft only) — pipeline build, edit-in-place, alert scaffold, report compose
    DRAFT_PIPELINE_FROM_INTENT,
    MODIFY_PIPELINE_STEP,
    DRAFT_ALERT_RULE,
    COMPOSE_REPORT,
    # Safe-write (draft only) — agentic connector authoring: draft a connector
    # from an OpenAPI spec/URL or sample responses (inert until an admin
    # approves it, which activates it as a Beta connector), and test a saved
    # connection. No secrets ever pass through the LLM.
    DRAFT_CONNECTOR_FROM_OPENAPI,
    DRAFT_CONNECTOR_FROM_SAMPLES,
    TEST_CONNECTION,
    # High-impact write — apply a confirmed pipeline draft (the only mutation
    # tool the agent has; gated by RBAC + dry-run-by-default + confirmation).
    APPLY_PIPELINE_DRAFT,
)

# Opt-in web tools — READ-tier, registered ONLY when the operator sets
# REVOLUZEN_FLOW_AI_WEB_ACCESS=1 (see revoluzen_flow.ai.web). Kept OUT of INITIAL_TOOLS so the
# air-gap default holds: with the flag off the LLM never sees these tools at all.
# (The offline count is whatever INITIAL_TOOLS holds — 32 as of 2026-11-03;
# the old hard-coded "29" here went stale the first time a tool was added.)
OPTIONAL_WEB_TOOLS: tuple[ToolDefinition, ...] = (
    WEB_SEARCH,
    WEB_FETCH,
)


def register_initial_tools(registry: ToolRegistry | None = None) -> ToolRegistry:
    """Register every tool in INITIAL_TOOLS + their output schemas. Idempotent.

    Pass `registry=None` (default) to register into the per-process default;
    pass an explicit ToolRegistry to register into a test-owned instance.

    Output schemas are pulled from each ToolDefinition.output_schema and
    forwarded to revoluzen_flow.ai.normalize so normalize_tool_output enforces them
    on every tool result.

    The opt-in web tools (OPTIONAL_WEB_TOOLS) are RECONCILED against the live
    web-access state (env var OR the admin Settings toggle): registered when
    on, removed when off. Because this runs per agent request, flipping the
    Settings toggle takes effect on the next Copilot turn with no restart.
    """
    target = registry if registry is not None else default_registry()
    for tool in INITIAL_TOOLS:
        target.register(tool)
        register_output_schema(tool.name, tool.output_schema)

    from revoluzen_flow.ai.web import web_access_enabled
    web_on = web_access_enabled()
    for tool in OPTIONAL_WEB_TOOLS:
        if web_on:
            target.register(tool)
            register_output_schema(tool.name, tool.output_schema)
        elif tool.name in target:
            # Toggle was turned off since the last run — drop the tool so the
            # LLM stops seeing it immediately.
            del target.tools[tool.name]
    return target


__all__ = [
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
    "ToolTier",
    "ToolNotFoundError",
    "default_registry",
    "reset_default_registry",
    "register_initial_tools",
    "INITIAL_TOOLS",
    "OPTIONAL_WEB_TOOLS",
    "WEB_FETCH",
    "WEB_SEARCH",
    # Identity / overview
    "GET_USER_ROLE",
    "GET_WORKSPACE_OVERVIEW",
    # List tools
    "LIST_PIPELINES",
    "LIST_PROJECTS",
    "LIST_SCHEDULES",
    "LIST_ALERTS",
    "LIST_EXECUTIONS",
    "LIST_STORAGE",
    "INSPECT_CONNECTIONS",
    "LIST_CATALOG",
    "LIST_TEMPLATES",
    # Drill-down
    "SUMMARIZE_PIPELINE",
    "INSPECT_STEP_COLUMNS",
    "TEST_PIPELINE_DRAFT",
    "QUERY_METRICS",
    "RECALL_HISTORY",
    "LOOKUP_HELP_TOPIC",
    # Operational reads
    "GET_RUNNING_EXECUTIONS",
    "GET_NEXT_SCHEDULED",
    "GET_INSTALLATION_HEALTH",
    "GET_SECURITY_POSTURE",
    "LIST_STEWARD_FINDINGS",
    # Safe-write
    "DRAFT_PIPELINE_FROM_INTENT",
    "MODIFY_PIPELINE_STEP",
    "DRAFT_ALERT_RULE",
    "COMPOSE_REPORT",
    "DRAFT_CONNECTOR_FROM_OPENAPI",
    "DRAFT_CONNECTOR_FROM_SAMPLES",
    "TEST_CONNECTION",
    # High-impact write
    "APPLY_PIPELINE_DRAFT",
]
