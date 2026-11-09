"""A Copilot draft is tried on a sample before anyone sees it.

A model can name a column that doesn't exist, even when told which do. A draft
that can't run is worse than a slower answer, so a missing column goes back to
the model once or twice, and whatever it can't fix is said out loud. The sample
writes nothing, and only column names go back, never rows.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

REQUEST = "Build a pipeline that shows returns by reason"
SECRET_ROW_VALUE = "a reason nobody should see"


def _draft(count_column, *, extra_steps=()):
    steps = [
        {"type": "db_source", "label": "Read returns", "params": {"query": "SELECT 1"}},
        {"type": "aggregate", "label": "Count by reason", "params": {
            "group_by": ["reason"],
            "functions": [{"column": count_column, "function": "COUNT", "alias": "returns"}],
        }},
        *extra_steps,
        {"type": "local_table_sink", "label": "Save", "params": {"schema_name": "default", "table_name": "by_reason"}},
    ]
    return {
        "name": "Returns by Reason", "explanation": "Counts returns.",
        "steps": steps,
        "connections": [{"from_step": i, "to_step": i + 1} for i in range(len(steps) - 1)],
    }


@pytest.fixture(scope="module")
def workspace(authed_client, data_dir):
    path = os.path.join(data_dir, "service.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE returns (return_id INTEGER, reason TEXT)")
        conn.executemany("INSERT INTO returns VALUES (?, ?)", [(1, SECRET_ROW_VALUE), (2, SECRET_ROW_VALUE), (3, "damaged")])
    conn.close()
    saved = authed_client.post("/api/connections/", json={
        "name": "Service database", "type": "sqlite", "config": {"file_path": "service.db"},
        "tags": [], "environment": "dev", "capabilities": ["read", "write"],
    })
    assert saved.status_code == 200, saved.text
    return authed_client, saved.json()["id"]


@pytest.fixture
def model(monkeypatch):
    from revoluzen_flow.api import planner

    state = {"replies": [], "calls": []}

    async def generate(messages, user_id=None, workspace_id=None):
        state["calls"].append(messages)
        return state["replies"].pop(0) if state["replies"] else None

    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: True)
    monkeypatch.setattr(planner, "ai_generate_pipeline", generate)
    return state


def _ask(client, connection_id):
    first = client.post("/api/planner/chat?preview=true", json=[{"role": "user", "content": REQUEST}]).json()
    r = client.post("/api/planner/chat?preview=true", json=[
        {"role": "user", "content": REQUEST},
        {"role": "assistant", "content": first["reply"]},
        {"role": "user", "content": "Read from Service database.", "choices": {
            "source": {"kind": "connection", "connection_id": connection_id, "table": "returns", "schema": "main"},
            "destination": {"kind": "new_table", "table_name": "by_reason"},
        }},
    ])
    assert r.status_code == 200, r.text
    return r.json()


def _counted(reply):
    return next(s for s in reply["workflow"]["steps"] if s["type"] == "aggregate")["params"]["functions"][0]["column"]


def test_a_draft_that_runs_is_left_alone(workspace, model):
    client, connection = workspace
    model["replies"] = [_draft("return_id")]
    reply = _ask(client, connection)
    assert len(model["calls"]) == 1
    assert _counted(reply) == "return_id"
    assert "Check this before" not in reply["reply"]


def test_a_missing_column_goes_back_to_the_model_and_is_corrected(workspace, model):
    client, connection = workspace
    model["replies"] = [_draft("id"), _draft("return_id")]
    reply = _ask(client, connection)
    assert len(model["calls"]) == 2
    told = model["calls"][1][-1]["content"]
    assert '"id"' in told and "isn't there" in told
    # The real columns are in what it was already told.
    assert "return_id" in json.dumps(model["calls"][1][:-1])
    assert _counted(reply) == "return_id"
    assert "Check this before" not in reply["reply"]


def test_the_model_only_ever_hears_column_names(workspace, model):
    client, connection = workspace
    model["replies"] = [_draft("id"), _draft("return_id")]
    _ask(client, connection)
    assert SECRET_ROW_VALUE not in json.dumps(model["calls"])


def test_what_it_cant_fix_is_said_out_loud(workspace, model):
    client, connection = workspace
    model["replies"] = [_draft("id"), _draft("nope"), _draft("still_no")]
    reply = _ask(client, connection)
    assert len(model["calls"]) == 3
    assert "Check this before you run it" in reply["reply"]
    assert "still_no" in reply["reply"]


def test_a_model_that_gives_up_leaves_the_first_draft_and_the_warning(workspace, model):
    client, connection = workspace
    model["replies"] = [_draft("id")]
    reply = _ask(client, connection)
    assert _counted(reply) == "id"
    assert "Check this before you run it" in reply["reply"]


def test_trying_a_draft_writes_nothing(workspace, model):
    client, connection = workspace
    model["replies"] = [_draft("return_id")]
    _ask(client, connection)
    tables = client.get("/api/storage/tables").json()["tables"]
    assert "by_reason" not in [t["name"] for t in tables]


def test_a_draft_with_its_own_sql_is_not_run_unseen(workspace, model, monkeypatch):
    from revoluzen_flow.api import planner
    from revoluzen_flow.engine import executor

    def refuse(*args, **kwargs):
        raise AssertionError("this draft must not run before a person has seen it")

    monkeypatch.setattr(executor.WorkflowExecutor, "execute_workflow", refuse)
    client, connection = workspace
    model["replies"] = [_draft("id", extra_steps=[{"type": "transform", "label": "Own SQL", "params": {"sql": "SELECT 1"}}])]
    reply = _ask(client, connection)
    assert len(model["calls"]) == 1
    assert reply["workflow"] is not None
    assert planner._TRIAL_STEP_TYPES.isdisjoint({"transform", "code_script", "execute_sql_task", "execute_pipeline", "http_request"})
