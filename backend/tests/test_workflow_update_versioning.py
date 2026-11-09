"""Updates, immutable historical versions, and structured plan differences."""
import pytest
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)
from tests.test_workflow_steps_persist import create_workflow, read_workflow


@pytest.mark.parametrize("mutation", ["rename", "add", "remove", "params"])
def test_update_persists_and_preserves_old_version(authed_client, mutation):
    wid = create_workflow(authed_client)
    original = read_workflow(authed_client, wid)
    changed = read_workflow(authed_client, wid)
    if mutation == "rename":
        changed["name"] = "Renamed workflow"
    elif mutation == "add":
        changed["steps"].append({"id": "sort", "type": "sort", "params": {"columns": ["region"]}})
        changed["connections"].append({"from_step": "agg", "to_step": "sort"})
    elif mutation == "remove":
        changed["steps"] = changed["steps"][:1]
        changed["connections"] = []
    else:
        changed["steps"][1]["params"]["condition"] = "amount > 500"
    response = authed_client.put(f"/api/workflows/{wid}", json={
        "workflow": changed, "change_summary": mutation})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    actual = read_workflow(authed_client, wid)
    for field in ("name", "steps", "connections"):
        # Newly inserted steps and edges acquire model defaults.
        if field == "name" or mutation != "add":
            assert actual[field] == changed[field]
    if mutation == "add":
        assert actual["steps"][-1]["id"] == "sort"
        assert actual["steps"][-1]["params"] == {"columns": ["region"]}
        assert actual["connections"][-1]["to_step"] == "sort"
    assert read_workflow(authed_client, wid, version=1) == original
    versions = authed_client.get(f"/api/workflows/{wid}/versions")
    assert versions.status_code == 200, versions.text
    assert {v["version"] for v in versions.json()} == {1, 2}


def test_diff_shows_removed_step(authed_client):
    wid = create_workflow(authed_client)
    changed = read_workflow(authed_client, wid)
    changed["steps"] = changed["steps"][:1]
    changed["connections"] = []
    saved = authed_client.put(f"/api/workflows/{wid}", json={"workflow": changed})
    assert saved.status_code == 200, saved.text
    diff = authed_client.get(f"/api/workflows/{wid}/diff", params={"v1": 1, "v2": 2})
    assert diff.status_code == 200, diff.text
    assert set(diff.json()["removed_steps"]) == {"flt", "agg"}


def test_missing_historical_version_returns_404(authed_client):
    wid = create_workflow(authed_client)
    response = authed_client.get(f"/api/workflows/{wid}", params={"version": 999})
    assert response.status_code == 404, response.text


def _version_numbers(authed_client, wid) -> set:
    versions = authed_client.get(f"/api/workflows/{wid}/versions")
    assert versions.status_code == 200, versions.text
    return {v["version"] for v in versions.json()}


def test_saving_without_a_change_adds_no_version(authed_client):
    """Autosave, the save before a run and a second click on Save send the
    pipeline as it already is. Each used to become a version, so the versions
    kept could all be copies of one definition."""
    wid = create_workflow(authed_client)
    same = read_workflow(authed_client, wid)
    for summary in ("Auto-save", "Pre-step save", "Save with metadata"):
        response = authed_client.put(f"/api/workflows/{wid}", json={
            "workflow": same, "change_summary": summary})
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        assert response.json()["unchanged"] is True
        assert response.json()["workflow"]["steps"] == same["steps"]
    assert _version_numbers(authed_client, wid) == {1}
    assert read_workflow(authed_client, wid)["updated_at"] == same["updated_at"]


def test_restoring_an_old_version_adds_it_as_the_newest(authed_client):
    """Restore sends an old version back. It becomes the newest version and
    the versions after the restored one are kept."""
    wid = create_workflow(authed_client)
    original = read_workflow(authed_client, wid)
    changed = read_workflow(authed_client, wid)
    changed["steps"][1]["params"]["condition"] = "amount > 500"
    edited = authed_client.put(f"/api/workflows/{wid}", json={"workflow": changed})
    assert edited.json()["version"] == 2

    restore = {"workflow": read_workflow(authed_client, wid, version=1),
               "change_summary": "Restored to version 1"}
    restored = authed_client.put(f"/api/workflows/{wid}", json=restore)
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 3
    assert restored.json()["unchanged"] is False
    assert read_workflow(authed_client, wid)["steps"] == original["steps"]
    assert _version_numbers(authed_client, wid) == {1, 2, 3}

    # Restoring it again changes nothing, so it adds nothing.
    again = authed_client.put(f"/api/workflows/{wid}", json=restore)
    assert again.status_code == 200, again.text
    assert again.json()["version"] == 3
    assert again.json()["unchanged"] is True
    assert _version_numbers(authed_client, wid) == {1, 2, 3}
