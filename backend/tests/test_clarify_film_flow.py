"""The filmed request, over HTTP, against a running app with no AI provider.

The same calls the browser makes, in the same order: the request, the card, the
new connection made in the chat, the answers, Apply to canvas, and a run. Nothing
is stubbed but the AI provider, so a route, a request body or a stored field that
the unit tests can't see would fail here.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

FILM_PROMPT = "Build a pipeline that shows returns by reason"
SYNTHETIC_SECRET = "synthetic-password-not-real-42"


@pytest.fixture(autouse=True)
def no_ai_provider(monkeypatch):
    from revoluzen_flow.api import planner
    monkeypatch.setattr(planner, "is_ai_available", lambda user_id=None, workspace_id=None: False)


@pytest.fixture(scope="module")
def demo_workspace(authed_client, data_dir):
    """What the film's workspace holds before the request."""
    r = authed_client.post("/api/storage/upload", files={
        "file": ("tickets.csv", "id,team\n1,alpha\n2,beta\n", "text/csv"),
    })
    assert r.status_code == 200, r.text

    stock = os.path.join(data_dir, "stock.db")
    with sqlite3.connect(stock) as conn:
        conn.execute("CREATE TABLE stock (id INTEGER)")
    conn.close()
    for body in (
        {"name": "Stockroom database", "type": "sqlite", "config": {"file_path": "stock.db"}, "environment": "dev"},
        {"name": "Partner price API", "type": "rest_api",
         "config": {"base_url": "https://example.invalid", "api_key": SYNTHETIC_SECRET}, "environment": "dev"},
    ):
        r = authed_client.post("/api/connections/", json=body)
        assert r.status_code == 200, r.text

    warehouse = os.path.join(data_dir, "warehouse.db")
    with sqlite3.connect(warehouse) as conn:
        conn.execute("CREATE TABLE returns (id INTEGER, reason TEXT)")
        conn.executemany(
            "INSERT INTO returns VALUES (?, ?)",
            [(1, "damaged"), (2, "damaged"), (3, "wrong size")],
        )
    conn.close()
    return authed_client


def _chat(client, messages):
    r = client.post("/api/planner/chat?preview=true", json=messages)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_filmed_request_from_prompt_to_run(demo_workspace):
    client = demo_workspace

    # The card, listing what this workspace really has.
    first = _chat(client, [{"role": "user", "content": FILM_PROMPT}])
    assert first["intent"] == "clarify_first"
    assert first["workflow"] is None
    card = first["clarification"]
    source, destination = card["questions"]
    assert source["question"] == "Where should I read the data from?"
    assert source["chips"] == [
        "Stockroom database (SQLite)",
        "Partner price API (REST API)",
        "tickets.csv (uploaded file)",
        "New connection",
    ]
    assert destination["chips"][0] == "New managed table in this workspace"
    assert destination["options"][0]["table_name"] == "returns_by_reason"
    assert destination["chips"][-1] == "New connection"
    assert SYNTHETIC_SECRET not in json.dumps(first)

    # The inline form: Test, Save, then the table list, through the same APIs
    # the Connections page uses.
    tested = client.post("/api/connections/test-inline", json={
        "type": "sqlite", "config": {"file_path": "warehouse.db"},
    })
    assert tested.status_code == 200, tested.text
    assert tested.json()["success"] is True

    saved = client.post("/api/connections/", json={
        "name": "Returns database", "type": "sqlite", "config": {"file_path": "warehouse.db"},
        "credential_id": None, "tags": [], "project_id": None, "environment": "dev",
        "capabilities": ["read", "write"],
    })
    assert saved.status_code == 200, saved.text
    connection_id = saved.json()["id"]

    catalog = client.get(f"/api/connections/{connection_id}/catalog").json()
    assert [(i["name"], i["parent"]) for i in catalog["items"]] == [("returns", "main")]

    # Send answers.
    second = _chat(client, [
        {"role": "user", "content": FILM_PROMPT},
        {"role": "assistant", "content": first["reply"]},
        {
            "role": "user",
            "content": "Read from Returns database, table returns.\n"
                       "Write to a new managed table called returns_by_reason.",
            "choices": {
                "source": {"kind": "connection", "connection_id": connection_id, "table": "returns", "schema": "main"},
                "destination": {"kind": "new_table", "table_name": "returns_by_reason"},
            },
        },
    ])
    assert "clarification" not in second
    draft = second["workflow"]
    assert [s["type"] for s in draft["steps"]] == ["source", "aggregate", "local_table_sink"]
    assert draft["steps"][0]["params"]["connection_id"] == connection_id
    assert draft["steps"][0]["params"]["table"] == "returns"
    assert draft["steps"][1]["params"]["group_by"] == ["reason"]
    assert draft["steps"][2]["params"]["table_name"] == "returns_by_reason"
    assert "It reads from Returns database (table returns)" in second["reply"]
    assert "returns_by_reason" in second["reply"]

    # Apply to canvas, then run it: rows really move.
    applied = client.post("/api/planner/apply-draft", json={
        "name": draft["name"], "description": draft.get("description", ""),
        "steps": draft["steps"], "connections": draft["connections"],
    })
    assert applied.status_code == 200, applied.text
    workflow = applied.json()["workflow"]

    ran = client.post(f"/api/workflows/{workflow['id']}/test")
    assert ran.status_code == 200, ran.text
    assert ran.json()["status"] == "success", ran.json().get("test_results")
    logs = {s["step_id"]: s for s in ran.json()["execution"]["step_logs"]}
    assert logs[draft["steps"][0]["id"]]["rows_processed"] == 3
    assert logs[draft["steps"][1]["id"]]["rows_processed"] == 2


def test_uploaded_file_chip_reaches_the_draft(demo_workspace):
    client = demo_workspace
    prompt = "Build a pipeline that shows open tickets by team"
    first = _chat(client, [{"role": "user", "content": prompt}])
    source = first["clarification"]["questions"][0]
    # The prompt's own words put the matching upload first.
    assert source["chips"][0] == "tickets.csv (uploaded file)"
    option = source["options"][0]

    second = _chat(client, [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": first["reply"]},
        {"role": "user", "content": "Read from tickets.csv.", "choices": {
            "source": {"kind": "file", "file_id": option["file_id"], "file_name": option["file_name"]},
            "destination": {"kind": "new_table", "table_name": "tickets_by_team"},
        }},
    ])
    step = second["workflow"]["steps"][0]
    assert step["params"]["connector_type"] == "csv"
    assert step["params"]["file_path"].startswith("uploads/")
    assert step["params"]["file_path"].endswith(".csv")


def test_a_connection_id_from_another_workspace_is_ignored(demo_workspace):
    client = demo_workspace
    first = _chat(client, [{"role": "user", "content": FILM_PROMPT}])
    second = _chat(client, [
        {"role": "user", "content": FILM_PROMPT},
        {"role": "assistant", "content": first["reply"]},
        {"role": "user", "content": "x", "choices": {
            "source": {"kind": "connection", "connection_id": "not-in-this-workspace", "table": "t"},
        }},
    ])
    for step in (second.get("workflow") or {}).get("steps", []):
        assert step["params"].get("connection_id") != "not-in-this-workspace"
