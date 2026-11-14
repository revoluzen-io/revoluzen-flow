"""Version history (schema v34): the kind of save behind each version, who
saved it, names, the keeping rules, and the runs that used each version."""
import sqlite3

from revoluzen_flow import runtime_config
from revoluzen_flow.ir.schema import PipelineStatus
from revoluzen_flow.storage.database import Database
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)
from tests.test_workflow_steps_persist import create_workflow, read_workflow

WID = "test-wf-001"


def _save_again(store, name, kind="saved"):
    """Save the newest version under a new name, the way an edit would."""
    wf = store.get(WID).workflow
    wf.name = name
    return store.save(wf, change_summary=name, kind=kind)


class TestKinds:
    def test_kind_is_recorded_and_anything_unknown_is_a_save(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow, change_summary="first")
        _save_again(workflow_store, "two", kind="auto")
        _save_again(workflow_store, "three", kind="restored")
        _save_again(workflow_store, "four", kind="something-else")
        kinds = [v["kind"] for v in workflow_store.get_versions(WID)]
        assert kinds == ["saved", "auto", "restored", "saved"]


class TestKeepingRules:
    def test_named_review_and_newest_versions_of_each_kind_stay(self, workflow_store, sample_workflow, monkeypatch):
        monkeypatch.setattr(runtime_config, "VERSION_RETENTION_COUNT", 3)
        monkeypatch.setattr(runtime_config, "AUTOSAVE_RETENTION_COUNT", 2)
        workflow_store.save(sample_workflow, change_summary="v1")
        assert workflow_store.set_version_name(WID, 1, "Go-live") is True
        for i in range(2, 7):
            _save_again(workflow_store, f"auto {i}", kind="auto")
        _save_again(workflow_store, "Submitted for review", kind="review")
        for i in range(8, 12):
            _save_again(workflow_store, f"save {i}")
        kept = [v["version"] for v in workflow_store.get_versions(WID)]
        # v1 named, v5-v6 the newest auto-saves, v7 a review step, v9-v11 the newest saves.
        assert kept == [1, 5, 6, 7, 9, 10, 11]
        assert workflow_store.removed_count(WID) == 4

    def test_the_published_version_stays(self, workflow_store, sample_workflow, monkeypatch):
        monkeypatch.setattr(runtime_config, "VERSION_RETENTION_COUNT", 2)
        workflow_store.save(sample_workflow, change_summary="v1")
        _save_again(workflow_store, "v2")
        workflow_store.update_status(WID, PipelineStatus.PUBLISHED, published_by="Siva")
        for i in range(3, 7):
            _save_again(workflow_store, f"v{i}")
        versions = workflow_store.get_versions(WID)
        # Later saves carry the publish date forward; only v2 was published.
        assert [v["version"] for v in versions if v["published"]] == [2]
        assert [v["version"] for v in versions] == [2, 5, 6]

    def test_the_deployed_version_stays(self, workflow_store, sample_workflow, monkeypatch):
        monkeypatch.setattr(runtime_config, "VERSION_RETENTION_COUNT", 1)
        workflow_store.save(sample_workflow, change_summary="v1")
        workflow_store.update_status(WID, PipelineStatus.DRAFT, deployed_version=1)
        for i in range(2, 5):
            _save_again(workflow_store, f"v{i}")
        versions = workflow_store.get_versions(WID)
        assert [v["version"] for v in versions] == [1, 4]
        assert [v["version"] for v in versions if v["deployed"]] == [1]

    def test_no_limit_keeps_everything(self, workflow_store, sample_workflow, monkeypatch):
        monkeypatch.setattr(runtime_config, "VERSION_RETENTION_COUNT", 0)
        monkeypatch.setattr(runtime_config, "AUTOSAVE_RETENTION_COUNT", 0)
        workflow_store.save(sample_workflow, change_summary="v1")
        for i in range(2, 9):
            _save_again(workflow_store, f"v{i}", kind="auto" if i % 2 else "saved")
        assert len(workflow_store.get_versions(WID)) == 8
        assert workflow_store.removed_count(WID) == 0


def test_names_and_kinds_survive_the_bin(workflow_store, sample_workflow):
    workflow_store.save(sample_workflow, change_summary="v1")
    workflow_store.set_version_name(WID, 1, "Before the change")
    _save_again(workflow_store, "Submitted for review", kind="review")
    assert workflow_store.trash(WID) is not None
    assert workflow_store.get_versions(WID) == []
    workflow_store.restore_from_trash(WID)
    versions = workflow_store.get_versions(WID)
    assert versions[0]["name"] == "Before the change"
    assert versions[1]["kind"] == "review"


def test_migration_adds_the_columns_and_runs_twice_safely():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ("workflow_id TEXT, version INTEGER, data JSON, created_by TEXT, change_summary TEXT, "
            "created_at TEXT, workspace_id TEXT, content_hash TEXT, PRIMARY KEY (workflow_id, version)")
    conn.executescript(
        f"CREATE TABLE workflow_versions ({cols});"
        f"CREATE TABLE workflow_trash ({cols});"
        "INSERT INTO workflow_versions VALUES ('w', 1, '{}', 'user', 'Auto-save', '2026-01-01', 'default', '');"
    )
    Database._migrate_v34_version_kinds(None, conn)
    Database._migrate_v34_version_kinds(None, conn)
    for table in ("workflow_versions", "workflow_trash"):
        names = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert {"kind", "version_name"} <= names
    row = conn.execute("SELECT kind, version_name FROM workflow_versions").fetchone()
    # Older rows count as saves: their notes cannot tell a Save from an auto-save.
    assert (row["kind"], row["version_name"]) == ("saved", "")
    assert conn.execute("SELECT COUNT(*) FROM workflow_version_removals").fetchone()[0] == 0


def test_saves_record_the_person_and_the_kind(authed_client):
    wid = create_workflow(authed_client)
    wf = read_workflow(authed_client, wid)
    wf["name"] = "Saved by auto-save"
    saved = authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf, "change_summary": "Auto-save", "kind": "auto"})
    assert saved.status_code == 200, saved.text
    wf["name"] = "Claims to be a review"
    saved = authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf, "change_summary": "Saved", "kind": "review"})
    assert saved.status_code == 200, saved.text
    history = authed_client.get(f"/api/workflows/{wid}/history")
    assert history.status_code == 200, history.text
    body = history.json()
    assert [v["version"] for v in body["versions"]] == [3, 2, 1]
    # Only the approval endpoints write review steps.
    assert [v["kind"] for v in body["versions"]] == ["saved", "auto", "saved"]
    assert all(v["created_by"] not in ("", "user") for v in body["versions"])
    assert body["current_version"] == 3
    assert body["removed"] == 0
    assert body["keep"] == {
        "saves": runtime_config.VERSION_RETENTION_COUNT,
        "auto_saves": runtime_config.AUTOSAVE_RETENTION_COUNT,
    }


def test_runs_are_counted_per_version(authed_client):
    from revoluzen_flow.main import app_state
    from revoluzen_flow.monitoring.store import ExecutionRecord

    wid = create_workflow(authed_client)
    wf = read_workflow(authed_client, wid)
    store = app_state["execution_store"]
    for status in ("error", "success"):
        store.record(ExecutionRecord(
            workflow_id=wid, workspace_id=wf.get("workspace_id") or "default",
            status=status, workflow_version=1,
        ))
    first = authed_client.get(f"/api/workflows/{wid}/history").json()["versions"][-1]
    assert first["version"] == 1
    assert first["runs"] == 2
    assert first["last_run_status"] in ("success", "error")


def test_naming_a_version(authed_client):
    wid = create_workflow(authed_client)
    named = authed_client.put(f"/api/workflows/{wid}/versions/1/name", json={"name": "  Go-live  "})
    assert named.status_code == 200, named.text
    assert named.json()["name"] == "Go-live"
    assert authed_client.get(f"/api/workflows/{wid}/history").json()["versions"][0]["name"] == "Go-live"
    assert authed_client.put(f"/api/workflows/{wid}/versions/9/name", json={"name": "x"}).status_code == 404
    assert authed_client.put(f"/api/workflows/{wid}/versions/1/name", json={"name": "x" * 81}).status_code == 400
    # A name is not a change to the pipeline: no new version.
    assert [v["version"] for v in authed_client.get(f"/api/workflows/{wid}/versions").json()] == [1]
