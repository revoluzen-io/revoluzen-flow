"""Copilot roles: Discover, Build, Test and Operate, switched per workspace."""

from __future__ import annotations

import asyncio

from revoluzen_flow.ai.roles import GENERAL_TOOLS, TOOL_ROLES, filter_tools, prompt_note, refusal, role_of
from revoluzen_flow.ai.tools import INITIAL_TOOLS

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def test_every_copilot_tool_has_a_role_on_purpose():
    # A new tool must be placed in a role (or the always-on few) by hand; the
    # fallback by tier is for add-ons, not for tools that ship with Flow.
    names = {t.name for t in INITIAL_TOOLS}
    unplaced = names - set(TOOL_ROLES) - set(GENERAL_TOOLS)
    assert not unplaced, f"give these tools a role in ai/roles.py: {sorted(unplaced)}"
    assert role_of("apply_pipeline_draft") == "build"
    assert role_of("lookup_help_topic") is None


def test_switching_a_role_off_takes_its_tools_and_actions_away(authed_client):
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.direct_actions import direct_run_pipeline

    body = authed_client.get("/api/v1/ai/roles").json()
    assert [r["key"] for r in body["roles"]] == ["discover", "build", "test", "operate"]
    assert all(r["enabled"] for r in body["roles"])
    ws = body["workspace_id"]
    assert prompt_note(ws) == ""

    assert authed_client.put("/api/v1/ai/roles", json={}).status_code == 400
    put = authed_client.put("/api/v1/ai/roles", json={"build": False, "operate": False})
    assert put.status_code == 200, put.text
    enabled = {r["key"]: r["enabled"] for r in put.json()["roles"]}
    assert enabled == {"discover": True, "build": False, "test": True, "operate": False}

    names = {t.name for t in filter_tools(INITIAL_TOOLS, ws)}
    assert "modify_pipeline_step" not in names and "apply_pipeline_draft" not in names
    assert "get_incidents" not in names and "list_executions" not in names
    assert {"list_catalog", "test_pipeline_draft", "lookup_help_topic"} <= names

    note = prompt_note(ws)
    assert "Build" in note and "Operate" in note and "Discover" not in note

    assert refusal("test_connection", ws) is None
    said = asyncio.run(direct_run_pipeline('run "orders"', ToolContext(
        tenant_id=ws, user_id="u", workspace_id=ws, environment="dev",
    )))
    assert "Operate role is switched off" in said

    # Back on: everything returns.
    authed_client.put("/api/v1/ai/roles", json={"build": True, "operate": True})
    assert len(filter_tools(INITIAL_TOOLS, ws)) == len(INITIAL_TOOLS)
    assert refusal("run_pipeline", ws) is None
