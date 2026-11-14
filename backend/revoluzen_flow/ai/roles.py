"""Copilot roles: what the Copilot may do in a workspace (2026-11-11).

The Copilot's tools fall into four roles, each switched on or off per
workspace by an admin:

* **Discover**: read what is here: connections, catalog, pipelines,
  storage, a step's columns, suggested join keys, specs, rule sets.
* **Build**: draft and change pipelines, connectors, alert rules, reports.
* **Test**: check a draft before it runs: validate it, test-run a draft,
  test a connection.
* **Operate**: watch and act on running work: runs, schedules,
  incidents, health, findings; start and stop runs.

A few general tools (help, the workspace overview, history recall, the
user's own role) are always on: without them the Copilot cannot even say
what it is not allowed to do. Everything is on by default; switching a role
off takes its tools out of the Copilot's hands in that workspace, and the
Copilot is told which roles are off so it says so instead of guessing.

Roles narrow what the Copilot may do; they never widen what the person may
do. A user's own rank still applies to everything the Copilot does for them.
Stored in the ``workspace_settings`` row (key ``ai_roles``), next to the AI
pricing (ai/cost_rates.py).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

ROLES: tuple[str, ...] = ("discover", "build", "test", "operate")

ROLE_INFO: dict[str, dict[str, str]] = {
    "discover": {"label": "Discover", "does": "Read connections, the catalog, pipelines, storage, a step's columns, specs and rule sets, and suggest join keys."},
    "build": {"label": "Build", "does": "Draft and change pipelines, connectors, alert rules and reports. Changes still wait for a person to apply them."},
    "test": {"label": "Test", "does": "Validate a draft, test-run it on sample rows, and test a connection."},
    "operate": {"label": "Operate", "does": "Read runs, schedules, incidents, health and findings, and start or stop a run."},
}

# Always available: the Copilot needs them to orient itself and to explain
# what it may not do.
GENERAL_TOOLS: frozenset[str] = frozenset({
    "get_user_role", "get_workspace_overview", "lookup_help_topic", "recall_history",
})

TOOL_ROLES: dict[str, str] = {
    # Discover
    "list_catalog": "discover", "inspect_connections": "discover", "list_pipelines": "discover",
    "list_projects": "discover", "list_storage": "discover", "list_templates": "discover",
    "summarize_pipeline": "discover", "explain_step": "discover", "inspect_step_columns": "discover",
    "suggest_join_keys": "discover", "get_pipeline_spec": "discover", "list_quality_rule_sets": "discover",
    "lookup_business_terms": "discover",
    "web_search": "discover", "web_fetch": "discover",
    # Build
    "draft_pipeline_from_intent": "build", "modify_pipeline_step": "build", "apply_pipeline_draft": "build",
    "draft_connector_from_openapi": "build", "draft_connector_from_samples": "build",
    "draft_alert_rule": "build", "compose_report": "build",
    # Test
    "test_pipeline_draft": "test", "validate_pipeline": "test", "test_connection": "test",
    # Operate
    "list_executions": "operate", "get_running_executions": "operate", "get_next_scheduled": "operate",
    "list_schedules": "operate", "get_incidents": "operate", "get_installation_health": "operate",
    "list_steward_findings": "operate", "get_security_posture": "operate", "list_alerts": "operate",
    "query_metrics": "operate",
}

# The Copilot's direct actions (ai/direct_actions.py), which every chat path
# reaches without the tool loop.
ACTION_ROLES: dict[str, str] = {
    "test_connection": "test",
    "run_pipeline": "operate",
    "cancel_run": "operate",
}

_SETTINGS_KEY = "ai_roles"


def role_of(tool: Any) -> str | None:
    """The role a tool belongs to; None for the always-on general tools. A
    tool not in the map (an add-on) is Discover when it only reads, Build
    when it writes."""
    name = getattr(tool, "name", tool)
    if name in GENERAL_TOOLS:
        return None
    if name in TOOL_ROLES:
        return TOOL_ROLES[name]
    tier = getattr(getattr(tool, "tier", None), "value", getattr(tool, "tier", None))
    return "discover" if str(tier or "read") == "read" else "build"


def _db():
    try:
        from revoluzen_flow.main import app_state  # type: ignore
        return app_state.get("db")
    except Exception:  # noqa: BLE001
        return None


def load_roles(workspace_id: str, db: Any = None) -> dict[str, bool]:
    """Which roles are on in this workspace. All on when nothing is saved."""
    roles = {r: True for r in ROLES}
    db = db if db is not None else _db()
    if db is None:
        return roles
    try:
        from revoluzen_flow.ai.cost_rates import _ensure_table, _read_blob
        _ensure_table(db)
        saved = (_read_blob(db, workspace_id or "default") or {}).get(_SETTINGS_KEY)
    except Exception:  # noqa: BLE001 — unreadable settings leave the Copilot as it was
        logger.warning("could not read Copilot roles for %s", workspace_id, exc_info=True)
        return roles
    if isinstance(saved, dict):
        for r in ROLES:
            if isinstance(saved.get(r), bool):
                roles[r] = saved[r]
    return roles


def save_roles(workspace_id: str, roles: dict[str, Any], *, by: str = "", db: Any = None) -> dict[str, bool]:
    db = db if db is not None else _db()
    if db is None:
        raise RuntimeError("Database not available")
    from revoluzen_flow.ai.cost_rates import _ensure_table, _read_blob
    _ensure_table(db)
    current = load_roles(workspace_id, db=db)
    for r in ROLES:
        if isinstance(roles.get(r), bool):
            current[r] = roles[r]
    blob = _read_blob(db, workspace_id) or {}
    blob[_SETTINGS_KEY] = current
    db.execute(
        """
        INSERT INTO workspace_settings (workspace_id, settings, updated_at, updated_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            settings = excluded.settings,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (workspace_id, json.dumps(blob), datetime.now(timezone.utc).isoformat(), by or "system"),
    )
    db.commit()
    return current


def filter_tools(tools: Iterable[Any], workspace_id: str, db: Any = None) -> list[Any]:
    """The tools whose role is on in this workspace, and the general ones."""
    on = load_roles(workspace_id, db=db)
    return [t for t in tools if (role_of(t) is None or on.get(role_of(t), True))]


def off_roles(workspace_id: str, db: Any = None) -> list[str]:
    on = load_roles(workspace_id, db=db)
    return [r for r in ROLES if not on[r]]


def refusal(action: str, workspace_id: str, db: Any = None) -> str | None:
    """A reply for a direct action whose role is off here; None when allowed."""
    role = ACTION_ROLES.get(action)
    if role is None or load_roles(workspace_id, db=db).get(role, True):
        return None
    label = ROLE_INFO[role]["label"]
    return (
        f"I can't do that here: the Copilot's {label} role is switched off in this workspace. "
        "An admin can switch it on in Settings, under Copilot roles."
    )


def prompt_note(workspace_id: str, db: Any = None) -> str:
    """What the model is told about switched-off roles; empty when all are on."""
    off = off_roles(workspace_id, db=db)
    if not off:
        return ""
    lines = [f"- {ROLE_INFO[r]['label']}: {ROLE_INFO[r]['does']}" for r in off]
    return (
        "COPILOT ROLES SWITCHED OFF IN THIS WORKSPACE (an admin's choice). You have no tools for:\n"
        + "\n".join(lines)
        + "\nIf asked for any of this, say that this role is switched off here and that an admin can "
          "switch it on in Settings, under Copilot roles. Do not describe it as done or attempt it another way."
    )


__all__ = [
    "ACTION_ROLES", "GENERAL_TOOLS", "ROLES", "ROLE_INFO", "TOOL_ROLES",
    "filter_tools", "load_roles", "off_roles", "prompt_note", "refusal", "role_of", "save_roles",
]
