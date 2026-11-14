"""Incidents and fixes by policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from revoluzen_flow.incidents import service
from revoluzen_flow.incidents.policy import AutoRecoverPolicy, load_policy, save_policy
from revoluzen_flow.incidents.store import IncidentStore
from revoluzen_flow.storage.database import Database

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


@pytest.fixture
def state(tmp_path):
    db = Database(str(tmp_path / "incidents.db"))
    db.execute("CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, data TEXT, created_at TEXT)")
    db.commit()
    return {"db": db, "incident_store": IncidentStore(db), "data_dir": str(tmp_path)}


def _outcome(state, status, *, run="r1", trigger="schedule", error="", step="load orders"):
    return service.record_run_outcome(
        state, workflow_id="wf1", workflow_name="Orders", execution_id=run, status=status,
        workspace_id="default", triggered_by=trigger, error_message=error, failed_step=step,
    )


def test_a_scheduled_failure_opens_an_incident_and_the_next_success_resolves_it(state):
    inc = _outcome(state, "error", error="connection refused by host")
    assert inc.status == "open" and inc.failure_class == "dependency"
    again = _outcome(state, "error", run="r2", error="connection refused by host")
    assert again.id == inc.id and again.occurrences == 2 and again.last_run_id == "r2"
    done = _outcome(state, "success", run="r3")
    assert done.status == "resolved" and "later schedule run succeeded" in done.resolution
    kinds = [e.kind for e in state["incident_store"].events(inc.id)]
    assert kinds == ["opened", "failed_again", "resolved"]


def test_runs_started_from_the_editor_are_not_tracked(state):
    assert _outcome(state, "error", trigger="manual", error="boom") is None
    assert state["incident_store"].open_for_workflow("wf1", "default") is None


def test_the_installs_paths_are_taken_out_of_the_error(state):
    inc = _outcome(state, "error", error=f"cannot read {state['data_dir']}/uploads/orders.csv: timed out")
    assert state["data_dir"] not in inc.error and "<data>/uploads/orders.csv" in inc.error


def test_the_policy_is_off_by_default_and_never_resumes_failures_a_retry_cannot_fix(state):
    assert load_policy(state).enabled is False
    with pytest.raises(ValidationError, match="never resumed automatically"):
        AutoRecoverPolicy(enabled=True, failure_classes=["transient", "data_quality"])


def test_the_policy_resumes_a_transient_failure_a_limited_number_of_times(state, monkeypatch):
    scheduled = []
    monkeypatch.setattr(service, "_start_timer", lambda delay, fn, kwargs: scheduled.append((delay, kwargs)))
    save_policy(AutoRecoverPolicy(enabled=True, failure_classes=["transient"], max_attempts=2, delay_seconds=30), state)

    inc = _outcome(state, "error", run="r1", error="operation timed out")
    _outcome(state, "error", run="r2", trigger="auto_recover", error="operation timed out")
    _outcome(state, "error", run="r3", trigger="auto_recover", error="operation timed out")
    assert [(d, k["run_id"]) for d, k in scheduled] == [(30, "r1"), (30, "r2")]
    events = state["incident_store"].events(inc.id)
    assert any(e.kind == "auto_recover_skipped" and "used up" in e.detail["reason"] for e in events)

    done = _outcome(state, "success", run="r4", trigger="auto_recover")
    assert "auto-recover policy" in done.resolution


def test_a_data_failure_is_left_for_a_person(state, monkeypatch):
    scheduled = []
    monkeypatch.setattr(service, "_start_timer", lambda *a: scheduled.append(a))
    save_policy(AutoRecoverPolicy(enabled=True), state)
    inc = _outcome(state, "error", error="null value in column order_id violates not-null")
    assert inc.failure_class == "data_quality" and scheduled == []
    assert state["incident_store"].events(inc.id)[-1].kind == "auto_recover_skipped"


def test_a_resume_that_is_no_longer_needed_does_not_run(state):
    inc = _outcome(state, "error", run="r1", error="timed out")
    _outcome(state, "error", run="r2", error="timed out")
    service._auto_resume(app_state=state, incident_id=inc.id, workflow_id="wf1", workspace_id="default", run_id="r1")
    assert state["incident_store"].events(inc.id)[-1].detail["reason"].startswith("a newer run failed")
    _outcome(state, "success", run="r3")
    service._auto_resume(app_state=state, incident_id=inc.id, workflow_id="wf1", workspace_id="default", run_id="r2")
    assert "resolved before the resume was due" in state["incident_store"].events(inc.id)[-1].detail["reason"]


def test_incidents_through_the_api(authed_client, data_dir):
    from revoluzen_flow.main import app_state
    from revoluzen_flow.notifications.run_events import emit_run_notification

    r = authed_client.post("/api/workflows", json={
        "name": "incident-orders",
        "steps": [
            {"id": "src", "type": "csv_source", "label": "orders", "params": {"file_path": f"{data_dir}/missing.csv"}, "position": {"x": 0, "y": 0}},
            {"id": "f", "type": "filter", "label": "big orders", "params": {"mode": "expression", "condition": "1=1"}, "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "src", "to_step": "f"}],
    })
    wid = r.json().get("id") or r.json()["workflow"]["id"]
    emit_run_notification(
        notification_store=app_state.get("notification_store"), user_store=app_state.get("user_store"),
        workflow_id=wid, workflow_name="incident-orders", execution_id="run-a", status="error",
        workspace_id="default", triggered_by="schedule", error_message="connection refused", failed_step="big orders",
    )
    listed = authed_client.get("/api/incidents").json()
    assert listed["count"] == 1
    iid = listed["incidents"][0]["id"]
    detail = authed_client.get(f"/api/incidents/{iid}").json()
    assert detail["incident"]["failure_class"] == "dependency"
    assert detail["context"]["upstream"] == ["orders"]
    assert detail["context"]["changes_since"], "versions saved with no success since should be listed"
    assert authed_client.get(f"/api/incidents/{iid}", headers={"X-Workspace-Id": "someone-else"}).status_code in (403, 404)

    assert authed_client.post(f"/api/incidents/{iid}/notes", json={"note": "source team paged"}).status_code == 200
    resolved = authed_client.post(f"/api/incidents/{iid}/resolve", json={"note": "file restored"})
    assert resolved.status_code == 200 and "file restored" in resolved.json()["incident"]["resolution"]

    policy = authed_client.put("/api/incidents/policy", json={"enabled": True, "failure_classes": ["transient"]})
    assert policy.status_code == 200 and policy.json()["policy"]["enabled"] is True
    assert authed_client.put("/api/incidents/policy", json={"enabled": True, "failure_classes": ["fatal"]}).status_code == 422
    authed_client.put("/api/incidents/policy", json={"enabled": False})
