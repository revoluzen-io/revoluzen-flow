"""Saving from the canvas must not wipe what the canvas doesn't send.

The editor's Save sends id, name, steps, connections and parameters. Everything
else used to fall back to the model default, so one save moved a pipeline to the
Default project and erased its business purpose, README, tags and author.
"""
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def _project(authed_client) -> str:
    created = authed_client.post("/api/projects/", json={"name": "Finance"})
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _pipeline(authed_client, project_id) -> str:
    created = authed_client.post("/api/workflows/", json={
        "name": "Revenue by Region",
        "project_id": project_id,
        "business_purpose": "Revenue by region for the weekly sales review",
        "readme": "# Runbook",
        "tags": ["finance"],
        "steps": [{"id": "src", "type": "source", "params": {"file_path": "orders.csv"}}],
        "connections": [],
    })
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _read(authed_client, wid) -> dict:
    return authed_client.get(f"/api/workflows/{wid}").json()["workflow"]


def _canvas_save(authed_client, wid, name="Revenue by Region"):
    """The exact shape of nodesToWorkflow in the frontend store."""
    body = {
        "workflow": {
            "id": wid,
            "name": name,
            "steps": [{"id": "src", "type": "source", "params": {"file_path": "orders.csv"}}],
            "connections": [],
            "parameters": [],
        },
        "change_summary": "Auto-save",
    }
    saved = authed_client.put(f"/api/workflows/{wid}", json=body)
    assert saved.status_code == 200, saved.text


def test_canvas_save_keeps_project_and_documentation(authed_client):
    project_id = _project(authed_client)
    wid = _pipeline(authed_client, project_id)
    _canvas_save(authed_client, wid)
    saved = _read(authed_client, wid)
    assert saved["project_id"] == project_id
    assert saved["business_purpose"] == "Revenue by region for the weekly sales review"
    assert saved["readme"] == "# Runbook"
    assert saved["tags"] == ["finance"]


def test_canvas_save_keeps_the_author(authed_client):
    wid = _pipeline(authed_client, "default")
    before = _read(authed_client, wid)
    assert before["owner_name"]
    _canvas_save(authed_client, wid)
    saved = _read(authed_client, wid)
    assert saved["owner_name"] == before["owner_name"]
    assert saved["owner_id"] == before["owner_id"]


def test_canvas_save_still_applies_what_it_does_send(authed_client):
    wid = _pipeline(authed_client, "default")
    _canvas_save(authed_client, wid, name="Renamed")
    assert _read(authed_client, wid)["name"] == "Renamed"


def test_a_field_sent_empty_is_still_cleared(authed_client):
    """Leaving a field out keeps it; sending it empty is how a client clears it."""
    wid = _pipeline(authed_client, "default")
    wf = _read(authed_client, wid)
    wf["tags"] = []
    saved = authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf})
    assert saved.status_code == 200, saved.text
    assert _read(authed_client, wid)["tags"] == []


def test_deleting_a_pipeline_removes_its_schedule(authed_client):
    wid = _pipeline(authed_client, "default")
    created = authed_client.post("/api/schedules/", json={"workflow_id": wid, "schedule_type": "cron", "cron_expression": "*/5 * * * *"})
    assert created.status_code == 200, created.text
    assert authed_client.delete(f"/api/workflows/{wid}").status_code == 200
    left = authed_client.get("/api/schedules/", params={"workflow_id": wid}).json()
    assert [s for s in left if s["workflow_id"] == wid] == []


def test_save_bumps_the_modified_time(authed_client):
    # A save that changes nothing adds no version and keeps the time
    # (test_workflow_update_versioning), so this one renames the pipeline.
    wid = _pipeline(authed_client, "default")
    before = _read(authed_client, wid)["updated_at"]
    _canvas_save(authed_client, wid, name="Revenue by Region, weekly")
    assert _read(authed_client, wid)["updated_at"] > before
