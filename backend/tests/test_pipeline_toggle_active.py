"""Deactivate on the Pipelines page.

The button called a route that did not exist, so it always failed. And the
scheduler read a different flag from the one the page shows, so a route that set
only the page's flag would have shown Paused while the schedule kept firing. These
tests go through real HTTP and the real scheduler loop to catch both.
"""

from __future__ import annotations

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
)


def _save_pipeline(authed_client, name):
    r = authed_client.post("/api/workflows", json={
        "name": name,
        "steps": [{
            "id": "src",
            "type": "source",
            "label": "Orders file",
            "params": {"connector_type": "csv", "file_path": "orders.csv", "format": "csv", "header": True},
            "position": {"x": 0, "y": 0},
        }],
        "connections": [],
    })
    assert r.status_code in (200, 201), r.text
    wf = r.json()
    return wf.get("id") or wf["workflow"]["id"]


def _toggle(http, workflow_id, active, **extra):
    return http.post(f"/api/workflows/{workflow_id}/toggle-active", json={"active": active, **extra})


def _listed(authed_client, workflow_id):
    return next(w for w in authed_client.get("/api/workflows/").json() if w["id"] == workflow_id)


def _saved(authed_client, workflow_id):
    return authed_client.get(f"/api/workflows/{workflow_id}").json()["workflow"]


@pytest.fixture
def scheduled_pipeline(authed_client):
    """A saved pipeline with an enabled schedule, and what the scheduler does with it."""
    from revoluzen_flow.scheduling.scheduler import PipelineScheduler

    workflow_id = _save_pipeline(authed_client, "toggle-scheduled")
    created = authed_client.post("/api/schedules/", json={
        "workflow_id": workflow_id, "schedule_type": "cron", "cron_expression": "*/5 * * * *", "enabled": True,
    })
    assert created.status_code == 200, created.text
    schedule_id = created.json()["id"]

    scheduler = PipelineScheduler(check_interval_seconds=3600)
    fired: list[str] = []
    scheduler._is_due = lambda *_: True
    scheduler._execute_schedule = lambda sid, _sched: fired.append(sid)

    def fires() -> bool:
        fired.clear()
        scheduler._check_schedules()
        return schedule_id in fired

    yield workflow_id, fires
    authed_client.delete(f"/api/schedules/{schedule_id}")


def test_deactivate_shows_as_paused_on_the_pipelines_page(authed_client):
    workflow_id = _save_pipeline(authed_client, "toggle-listed")
    assert _listed(authed_client, workflow_id)["is_active_dev"] is True

    off = _toggle(authed_client, workflow_id, False, env="dev")
    assert off.status_code == 200, off.text
    assert off.json()["is_active"] is False
    assert _listed(authed_client, workflow_id)["is_active_dev"] is False

    on = _toggle(authed_client, workflow_id, True, env="dev")
    assert on.status_code == 200, on.text
    assert _listed(authed_client, workflow_id)["is_active_dev"] is True


def test_a_schedule_stops_firing_while_deactivated(authed_client, scheduled_pipeline):
    workflow_id, fires = scheduled_pipeline
    assert fires()

    assert _toggle(authed_client, workflow_id, False).status_code == 200
    assert not fires()

    assert _toggle(authed_client, workflow_id, True).status_code == 200
    assert fires()


def test_a_pause_recorded_in_either_flag_stops_the_schedule(authed_client, scheduled_pipeline):
    """Older data can hold just one of the two flags."""
    from revoluzen_flow.main import app_state

    workflow_id, fires = scheduled_pipeline
    store = app_state["store"]
    workflow = store.get(workflow_id).workflow
    workflow.is_active_dev = False
    store.save(workflow)
    assert not fires()


def test_activate_clears_a_pause_held_in_only_one_flag(authed_client):
    """Otherwise the page offers Deactivate while endpoints still answer 423."""
    from revoluzen_flow.main import app_state

    workflow_id = _save_pipeline(authed_client, "toggle-half")
    store = app_state["store"]
    workflow = store.get(workflow_id).workflow
    workflow.is_active_prod = False
    store.save(workflow)

    assert _toggle(authed_client, workflow_id, True).status_code == 200
    saved = _saved(authed_client, workflow_id)
    assert saved["is_active_dev"] is True
    assert saved["is_active_prod"] is True


def test_a_pipeline_can_still_be_run_by_hand_while_deactivated(authed_client):
    workflow_id = _save_pipeline(authed_client, "toggle-manual")
    assert _toggle(authed_client, workflow_id, False).status_code == 200
    run = authed_client.post(f"/api/execute/workflow/{workflow_id}")
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "success", run.text


def test_saving_the_canvas_keeps_the_pipeline_off(authed_client):
    workflow_id = _save_pipeline(authed_client, "toggle-canvas")
    assert _toggle(authed_client, workflow_id, False).status_code == 200
    saved = authed_client.put(f"/api/workflows/{workflow_id}", json={"workflow": {
        "id": workflow_id,
        "name": "toggle-canvas",
        "steps": [],
        "connections": [],
    }})
    assert saved.status_code == 200, saved.text
    assert _saved(authed_client, workflow_id)["is_active_dev"] is False


def test_restoring_an_old_version_does_not_switch_the_pipeline_on(authed_client):
    """Restore sends the old version's whole blob, flags included."""
    workflow_id = _save_pipeline(authed_client, "toggle-restore")
    old_version = _saved(authed_client, workflow_id)
    assert old_version["is_active_dev"] is True
    assert _toggle(authed_client, workflow_id, False).status_code == 200

    restored = authed_client.put(f"/api/workflows/{workflow_id}", json={
        "workflow": old_version, "change_summary": "Restored to version 1",
    })
    assert restored.status_code == 200, restored.text
    assert _saved(authed_client, workflow_id)["is_active_dev"] is False


def test_each_change_is_a_new_version_whose_hash_still_verifies(authed_client):
    """The flags are part of the content hash. Writing them over the latest
    version in place would make rollback report it as tampered with."""
    from revoluzen_flow.main import app_state

    workflow_id = _save_pipeline(authed_client, "toggle-hash")
    store = app_state["store"]
    before = store.get(workflow_id).version

    assert _toggle(authed_client, workflow_id, False).status_code == 200
    latest = store.get(workflow_id)
    assert latest.version == before + 1
    assert latest.change_summary.startswith("Deactivated")
    match, stored, _ = store.verify_version_hash(workflow_id, latest.version)
    assert match and stored


def test_asking_for_the_state_it_is_already_in_adds_no_version(authed_client):
    from revoluzen_flow.main import app_state

    workflow_id = _save_pipeline(authed_client, "toggle-noop")
    store = app_state["store"]
    before = store.get(workflow_id).version
    assert _toggle(authed_client, workflow_id, True).status_code == 200
    assert store.get(workflow_id).version == before


def test_the_change_is_on_the_pipeline_lifecycle(authed_client):
    workflow_id = _save_pipeline(authed_client, "toggle-lifecycle")
    assert _toggle(authed_client, workflow_id, False).status_code == 200
    events = authed_client.get(f"/api/workflows/{workflow_id}/lifecycle").json()
    assert "deactivated" in str(events)


def test_unknown_pipeline_is_404(authed_client):
    assert _toggle(authed_client, "no-such-pipeline", False).status_code == 404


def test_a_pipeline_in_another_workspace_is_404(authed_client):
    from revoluzen_flow.ir.schema import Workflow
    from revoluzen_flow.main import app_state

    foreign = Workflow(id="toggle-foreign", name="toggle-foreign", workspace_id="another-workspace")
    app_state["store"].save(foreign)
    assert _toggle(authed_client, "toggle-foreign", False).status_code == 404
    assert app_state["store"].get("toggle-foreign").workflow.is_active_dev is True


def test_anonymous_callers_are_refused(client, authed_client):
    workflow_id = _save_pipeline(authed_client, "toggle-anonymous")
    r = _toggle(client, workflow_id, False)
    assert r.status_code in (401, 403), r.text


@pytest.mark.parametrize("role", ["viewer", "analyst"])
def test_read_only_roles_are_refused(authed_client, role_clients, role):
    if role not in role_clients:
        pytest.skip(f"no {role} client")
    workflow_id = _save_pipeline(authed_client, f"toggle-{role}")
    r = _toggle(role_clients[role], workflow_id, False)
    assert r.status_code == 403, r.text
    assert _listed(authed_client, workflow_id)["is_active_dev"] is True


def test_a_body_without_active_is_refused(authed_client):
    workflow_id = _save_pipeline(authed_client, "toggle-empty")
    r = authed_client.post(f"/api/workflows/{workflow_id}/toggle-active", json={})
    assert r.status_code == 422, r.text
