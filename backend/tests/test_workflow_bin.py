"""The pipeline Bin: deleting moves a pipeline to the Bin, and it can be restored.

Before the Bin, Delete removed a pipeline, its versions and its schedules at
once. Now `DELETE /api/workflows/{id}` moves them to `workflow_trash`, a restore
brings the pipeline back as a draft with its schedules paused, and anything left
in the Bin past `REVOLUZEN_FLOW_BIN_RETENTION_DAYS` is removed by a daily job.
`?permanent=true` keeps the old behaviour.

The first half tests the store and the orchestration around it directly; the
second half goes through the HTTP API.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from revoluzen_flow.ir import workflow_bin
from revoluzen_flow.ir.schema import PipelineStatus, Workflow
from revoluzen_flow.ir.versioning import WorkflowIdInUse
from revoluzen_flow.scheduling.models import Schedule

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client, role_clients,
)


def _iso(delta_days: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).isoformat()


# ── Store and orchestration ────────────────────────────────────────────────

class TestStore:
    def test_the_tables_exist_on_a_fresh_database(self, workflow_store):
        from revoluzen_flow.storage.database import SCHEMA_VERSION
        names = {r["name"] for r in workflow_store._db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"workflow_trash", "workflow_trash_meta"} <= names
        assert SCHEMA_VERSION >= 33

    def test_trash_moves_every_version_and_hides_the_pipeline(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow, change_summary="v1")
        sample_workflow.description = "second"
        workflow_store.save(sample_workflow, change_summary="v2")

        entry = workflow_store.trash(
            "test-wf-001", deleted_by_id="u1", deleted_by_name="Asha",
            schedules=[{"id": "s1"}],
        )

        assert entry is not None
        assert entry["id"] == "test-wf-001"
        assert entry["name"] == "Test Pipeline"
        assert entry["version_count"] == 2
        assert entry["status_before"] == "draft"
        assert entry["deleted_by_name"] == "Asha"
        # Gone for every reader of workflow_versions.
        assert workflow_store.get("test-wf-001") is None
        assert workflow_store.list_all() == []
        # And listed in the Bin, without the schedule snapshot.
        listed = workflow_store.list_trash()
        assert [e["id"] for e in listed] == ["test-wf-001"]
        assert "schedules" not in listed[0]
        assert workflow_store.get_trash_entry("test-wf-001", include_schedules=True)["schedules"] == [{"id": "s1"}]

    def test_trash_of_an_unknown_pipeline_does_nothing(self, workflow_store):
        assert workflow_store.trash("nope") is None
        assert workflow_store.list_trash() == []

    def test_the_bin_is_scoped_to_the_workspace(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Alpha", workspace_id="ws-a"))
        # Another workspace cannot bin it, see it or restore it.
        assert workflow_store.trash("wf-a", workspace_id="ws-b") is None
        assert workflow_store.get("wf-a") is not None
        workflow_store.trash("wf-a", workspace_id="ws-a")
        assert workflow_store.list_trash("ws-b") == []
        assert workflow_store.get_trash_entry("wf-a", "ws-b") is None
        assert workflow_store.restore_from_trash("wf-a", "ws-b") is None
        assert [e["id"] for e in workflow_store.list_trash("ws-a")] == ["wf-a"]

    def test_restore_comes_back_as_a_draft_with_a_version_note(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow, change_summary="v1")
        workflow_store.update_status("test-wf-001", PipelineStatus.PUBLISHED)
        workflow_store.trash("test-wf-001")

        entry = workflow_store.restore_from_trash("test-wf-001", restored_by="Asha")

        assert entry["id"] == "test-wf-001"
        restored = workflow_store.get("test-wf-001")
        assert restored.workflow.status == PipelineStatus.DRAFT
        versions = workflow_store.get_versions("test-wf-001")
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[-1]["change_summary"] == "Restored from the Bin"
        assert versions[-1]["created_by"] == "Asha"
        assert workflow_store.list_trash() == []

    def test_restore_refuses_when_a_live_pipeline_has_the_id(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        workflow_store.trash("test-wf-001")
        workflow_store.save(Workflow(id="test-wf-001", name="Someone else"))
        with pytest.raises(WorkflowIdInUse):
            workflow_store.restore_from_trash("test-wf-001")
        # Still in the Bin, untouched.
        assert workflow_store.get_trash_entry("test-wf-001") is not None

    def test_restore_can_correct_name_project_and_folder(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Alpha", project_id="p1", folder_id="f1"))
        workflow_store.trash("wf-a")
        workflow_store.restore_from_trash("wf-a", name="Alpha (2)", project_id="default", folder_id=None)
        wf = workflow_store.get("wf-a").workflow
        assert (wf.name, wf.project_id, wf.folder_id) == ("Alpha (2)", "default", None)

    def test_restore_leaves_name_project_and_folder_alone_by_default(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Alpha", project_id="p1", folder_id="f1"))
        workflow_store.trash("wf-a")
        workflow_store.restore_from_trash("wf-a")
        wf = workflow_store.get("wf-a").workflow
        assert (wf.name, wf.project_id, wf.folder_id) == ("Alpha", "p1", "f1")

    def test_a_permanent_delete_leaves_nothing_in_the_bin(self, workflow_store, sample_workflow):
        workflow_store.save(sample_workflow)
        assert workflow_store.delete("test-wf-001") is True
        assert workflow_store.list_trash() == []

    def test_purging_one_removes_it_for_good(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Alpha"))
        workflow_store.trash("wf-a")
        assert workflow_store.purge_trash("wf-a")["id"] == "wf-a"
        assert workflow_store.list_trash() == []
        assert workflow_store.restore_from_trash("wf-a") is None
        assert workflow_store.purge_trash("wf-a") is None
        # No version rows left behind either.
        assert workflow_store._db.fetchall("SELECT 1 FROM workflow_trash") == []

    def test_the_bin_exports_with_the_rest_of_the_database(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Alpha"))
        workflow_store.trash("wf-a")
        tables = workflow_store._db.export_all()["_tables"]
        assert [r["workflow_id"] for r in tables["workflow_trash_meta"]] == ["wf-a"]
        assert len(tables["workflow_trash"]) == 1


class TestOrchestration:
    def test_a_normal_delete_bins_the_pipeline_and_its_schedules_leave_the_live_table(
        self, workflow_store, schedule_store, sample_workflow,
    ):
        workflow_store.save(sample_workflow)
        schedule_store.create(Schedule(
            workflow_id="test-wf-001", schedule_type="cron", cron_expression="*/5 * * * *",
        ))

        result = workflow_bin.delete_pipeline(
            "test-wf-001", "default", store=workflow_store, schedule_store=schedule_store,
            actor_id="u1", actor_name="Asha",
        )

        assert result["binned"] is True
        assert workflow_store.get("test-wf-001") is None
        # Left live, the schedule would fire and fail every time.
        assert schedule_store.list_by_workflow("test-wf-001") == []
        entry = workflow_store.get_trash_entry("test-wf-001", include_schedules=True)
        assert entry["deleted_by_name"] == "Asha"
        assert len(entry["schedules"]) == 1

    def test_a_permanent_delete_skips_the_bin(self, workflow_store, schedule_store, sample_workflow):
        workflow_store.save(sample_workflow)
        result = workflow_bin.delete_pipeline(
            "test-wf-001", "default", store=workflow_store, schedule_store=schedule_store, permanent=True,
        )
        assert result["binned"] is False and result["entry"] is None
        assert workflow_store.list_trash() == []

    def test_deleting_something_that_is_not_there_returns_none(self, workflow_store):
        assert workflow_bin.delete_pipeline("nope", "default", store=workflow_store) is None

    def test_restore_brings_schedules_back_paused(self, workflow_store, schedule_store, sample_workflow):
        workflow_store.save(sample_workflow)
        schedule_store.create(Schedule(
            workflow_id="test-wf-001", schedule_type="cron", cron_expression="*/5 * * * *", enabled=True,
        ))
        workflow_bin.delete_pipeline("test-wf-001", "default", store=workflow_store, schedule_store=schedule_store)

        result = workflow_bin.restore_pipeline(
            "test-wf-001", "default", store=workflow_store, schedule_store=schedule_store,
        )

        assert result["status"] == "draft"
        assert result["schedules_restored"] == 1
        assert any("paused" in note for note in result["notes"])
        back = schedule_store.list_by_workflow("test-wf-001")
        assert len(back) == 1 and back[0]["enabled"] is False

    def test_restore_suffixes_a_name_that_is_taken_again(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Revenue"))
        workflow_bin.delete_pipeline("wf-a", "default", store=workflow_store)
        workflow_store.save(Workflow(id="wf-b", name="Revenue"))   # the name is free while it is binned

        result = workflow_bin.restore_pipeline("wf-a", "default", store=workflow_store)

        assert result["name"] == "Revenue (2)"
        assert result["renamed_from"] == "Revenue"
        assert any("Revenue (2)" in note for note in result["notes"])
        assert workflow_store.get("wf-a").workflow.name == "Revenue (2)"
        assert workflow_store.get("wf-b").workflow.name == "Revenue"

    def test_restore_moves_to_default_when_the_project_or_folder_is_gone(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="Alpha", project_id="p1", folder_id="f1"))
        workflow_bin.delete_pipeline("wf-a", "default", store=workflow_store)

        result = workflow_bin.restore_pipeline(
            "wf-a", "default", store=workflow_store,
            project_lookup=lambda pid: None, folder_lookup=lambda fid: None,
        )

        assert result["project_id"] == "default" and result["folder_id"] is None
        assert len(result["notes"]) == 2
        wf = workflow_store.get("wf-a").workflow
        assert (wf.project_id, wf.folder_id) == ("default", None)

    def test_restore_moves_out_of_an_archived_project(self, workflow_store):
        class Project:
            status = "archived"

        workflow_store.save(Workflow(id="wf-a", name="Alpha", project_id="p1"))
        workflow_bin.delete_pipeline("wf-a", "default", store=workflow_store)
        result = workflow_bin.restore_pipeline(
            "wf-a", "default", store=workflow_store, project_lookup=lambda pid: Project(),
        )
        assert result["project_id"] == "default"

    def test_restore_keeps_a_project_and_folder_that_still_exist(self, workflow_store):
        class Project:
            status = "active"

        class Folder:
            project_id = "p1"
            workspace_id = "default"

        workflow_store.save(Workflow(id="wf-a", name="Alpha", project_id="p1", folder_id="f1"))
        workflow_bin.delete_pipeline("wf-a", "default", store=workflow_store)
        result = workflow_bin.restore_pipeline(
            "wf-a", "default", store=workflow_store,
            project_lookup=lambda pid: Project(), folder_lookup=lambda fid: Folder(),
        )
        assert (result["project_id"], result["folder_id"]) == ("p1", "f1")
        assert result["notes"] == []

    def test_restore_errors(self, workflow_store):
        with pytest.raises(workflow_bin.NotInBin):
            workflow_bin.restore_pipeline("nope", "default", store=workflow_store)
        workflow_store.save(Workflow(id="wf-a", name="Alpha"))
        workflow_bin.delete_pipeline("wf-a", "default", store=workflow_store)
        workflow_store.save(Workflow(id="wf-a", name="Taken id"))
        with pytest.raises(workflow_bin.IdInUse):
            workflow_bin.restore_pipeline("wf-a", "default", store=workflow_store)

    def test_expired_entries_are_purged_and_recent_ones_are_not(self, workflow_store):
        for wid, name in (("wf-old", "Old"), ("wf-new", "New")):
            workflow_store.save(Workflow(id=wid, name=name))
            workflow_bin.delete_pipeline(wid, "default", store=workflow_store)
        workflow_store._db.execute(
            "UPDATE workflow_trash_meta SET deleted_at = ? WHERE workflow_id = 'wf-old'", (_iso(-31),))
        workflow_store._db.commit()

        purged = workflow_bin.purge_expired(store=workflow_store, retention_days=30)

        assert [e["id"] for e in purged] == ["wf-old"]
        assert [e["id"] for e in workflow_store.list_trash()] == ["wf-new"]
        assert workflow_store._db.fetchall("SELECT 1 FROM workflow_trash WHERE workflow_id = 'wf-old'") == []

    def test_retention_below_one_day_purges_nothing(self, workflow_store):
        workflow_store.save(Workflow(id="wf-old", name="Old"))
        workflow_bin.delete_pipeline("wf-old", "default", store=workflow_store)
        workflow_store._db.execute("UPDATE workflow_trash_meta SET deleted_at = ?", (_iso(-3650),))
        workflow_store._db.commit()
        assert workflow_bin.purge_expired(store=workflow_store, retention_days=0) == []
        assert len(workflow_store.list_trash()) == 1

    def test_emptying_the_bin_only_touches_that_workspace(self, workflow_store):
        workflow_store.save(Workflow(id="wf-a", name="A", workspace_id="ws-a"))
        workflow_store.save(Workflow(id="wf-b", name="B", workspace_id="ws-b"))
        workflow_bin.delete_pipeline("wf-a", "ws-a", store=workflow_store)
        workflow_bin.delete_pipeline("wf-b", "ws-b", store=workflow_store)
        assert workflow_bin.empty_bin("ws-a", store=workflow_store) == 1
        assert [e["id"] for e in workflow_store.list_trash()] == ["wf-b"]

    def test_purge_at(self):
        assert workflow_bin.purge_at("2026-01-01T00:00:00+00:00", 30) == "2026-01-31T00:00:00+00:00"
        assert workflow_bin.purge_at("2026-01-01T00:00:00+00:00", 0) is None
        assert workflow_bin.purge_at("not a date", 30) is None

    def test_the_public_entry_has_a_purge_date_and_no_schedule_snapshot(self):
        entry = {"id": "x", "deleted_at": "2026-01-01T00:00:00+00:00", "schedules": [{"id": "s"}]}
        out = workflow_bin.public_entry(entry, 30)
        assert out["purge_at"] == "2026-01-31T00:00:00+00:00" and "schedules" not in out


# ── The HTTP API ──────────────────────────────────────────────────────────

def _make(authed_client, name, **extra) -> str:
    created = authed_client.post("/api/workflows/", json={
        "name": name,
        "steps": [{"id": "src", "type": "source", "params": {"file_path": "orders.csv"}}],
        "connections": [],
        **extra,
    })
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _binned_ids(client) -> list[str]:
    response = client.get("/api/workflows/bin")
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


class TestApi:
    def test_the_bin_route_is_not_read_as_a_pipeline_id(self, authed_client):
        body = authed_client.get("/api/workflows/bin").json()
        assert set(body) >= {"retention_days", "count", "items"}

    def test_delete_moves_a_pipeline_to_the_bin_and_restore_brings_it_back(self, authed_client):
        wid = _make(authed_client, "Bin API: roundtrip")

        deleted = authed_client.delete(f"/api/workflows/{wid}")
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["deleted"] is True and body["binned"] is True
        assert body["retention_days"] == 30 and body["purge_at"]

        assert authed_client.get(f"/api/workflows/{wid}").status_code == 404
        assert wid in _binned_ids(authed_client)
        item = next(i for i in authed_client.get("/api/workflows/bin").json()["items"] if i["id"] == wid)
        assert item["name"] == "Bin API: roundtrip" and item["purge_at"] and item["deleted_by_name"]
        assert "schedules" not in item

        restored = authed_client.post(f"/api/workflows/bin/{wid}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored"] is True and restored.json()["status"] == "draft"
        back = authed_client.get(f"/api/workflows/{wid}")
        assert back.status_code == 200
        assert back.json()["workflow"]["status"] == "draft"
        assert wid not in _binned_ids(authed_client)

    def test_permanent_delete_skips_the_bin(self, authed_client):
        wid = _make(authed_client, "Bin API: permanent")
        deleted = authed_client.delete(f"/api/workflows/{wid}", params={"permanent": "true"})
        assert deleted.status_code == 200 and deleted.json()["binned"] is False
        assert wid not in _binned_ids(authed_client)
        assert authed_client.post(f"/api/workflows/bin/{wid}/restore").status_code == 404

    def test_deleting_a_pipeline_that_does_not_exist_is_still_404(self, authed_client):
        assert authed_client.delete("/api/workflows/no-such-id").status_code == 404

    def test_restore_renames_when_the_name_was_taken_in_the_meantime(self, authed_client):
        wid = _make(authed_client, "Bin API: same name")
        authed_client.delete(f"/api/workflows/{wid}")
        other = _make(authed_client, "Bin API: same name")      # free while the first is binned
        restored = authed_client.post(f"/api/workflows/bin/{wid}/restore").json()
        assert restored["name"] == "Bin API: same name (2)"
        assert restored["renamed_from"] == "Bin API: same name"
        assert restored["notes"]
        assert authed_client.get(f"/api/workflows/{other}").json()["workflow"]["name"] == "Bin API: same name"

    def test_delete_forever_removes_one_entry(self, authed_client):
        wid = _make(authed_client, "Bin API: forever")
        authed_client.delete(f"/api/workflows/{wid}")
        gone = authed_client.delete(f"/api/workflows/bin/{wid}")
        assert gone.status_code == 200 and gone.json()["purged"] is True
        assert wid not in _binned_ids(authed_client)
        assert authed_client.post(f"/api/workflows/bin/{wid}/restore").status_code == 404
        assert authed_client.delete(f"/api/workflows/bin/{wid}").status_code == 404

    def test_schedules_leave_on_delete_and_come_back_paused(self, authed_client):
        wid = _make(authed_client, "Bin API: schedules")
        created = authed_client.post("/api/schedules/", json={
            "workflow_id": wid, "schedule_type": "cron", "cron_expression": "*/5 * * * *"})
        assert created.status_code == 200, created.text

        authed_client.delete(f"/api/workflows/{wid}")
        live = authed_client.get("/api/schedules/", params={"workflow_id": wid}).json()
        assert [s for s in live if s["workflow_id"] == wid] == []

        restored = authed_client.post(f"/api/workflows/bin/{wid}/restore").json()
        assert restored["schedules_restored"] == 1
        back = [s for s in authed_client.get("/api/schedules/", params={"workflow_id": wid}).json()
                if s["workflow_id"] == wid]
        assert len(back) == 1 and back[0]["enabled"] is False

    def test_an_admin_can_empty_the_bin_and_a_data_engineer_cannot(self, authed_client, role_clients):
        wid = _make(authed_client, "Bin API: to empty")
        authed_client.delete(f"/api/workflows/{wid}")

        engineer = role_clients.get("data_engineer")
        if engineer is None:
            pytest.skip("data_engineer client not provisioned")
        assert engineer.delete("/api/workflows/bin").status_code == 403
        assert wid in _binned_ids(authed_client)

        emptied = authed_client.delete("/api/workflows/bin")
        assert emptied.status_code == 200 and emptied.json()["purged"] >= 1
        assert _binned_ids(authed_client) == []

    def test_a_viewer_can_see_the_bin_but_not_change_it(self, authed_client, role_clients):
        viewer = role_clients.get("viewer")
        if viewer is None:
            pytest.skip("viewer client not provisioned")
        wid = _make(authed_client, "Bin API: viewer")
        authed_client.delete(f"/api/workflows/{wid}")
        assert viewer.get("/api/workflows/bin").status_code == 200
        assert viewer.post(f"/api/workflows/bin/{wid}/restore").status_code == 403
        assert viewer.delete(f"/api/workflows/bin/{wid}").status_code == 403
        assert wid in _binned_ids(authed_client)
        authed_client.delete(f"/api/workflows/bin/{wid}")

    def test_deleting_a_folder_with_force_sends_its_pipelines_to_the_bin(self, authed_client):
        project = authed_client.post("/api/projects/", json={"name": "Bin API project"})
        assert project.status_code == 200, project.text
        project_id = project.json()["id"]
        folder = authed_client.post("/api/folders", json={"name": "Bin API folder", "project_id": project_id})
        assert folder.status_code == 200, folder.text
        folder_id = folder.json()["id"]
        wid = _make(authed_client, "Bin API: in a folder", project_id=project_id, folder_id=folder_id)

        deleted = authed_client.delete(f"/api/folders/{folder_id}", params={"force": "true"})

        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_workflow_count"] == 1
        assert authed_client.get(f"/api/workflows/{wid}").status_code == 404
        assert wid in _binned_ids(authed_client)
        # The folder is gone, so the restore says where it went.
        restored = authed_client.post(f"/api/workflows/bin/{wid}/restore").json()
        assert restored["folder_id"] is None
        assert any("folder" in note for note in restored["notes"])

    def test_the_purge_loop_does_nothing_when_retention_is_off(self, app_v2, monkeypatch):
        from revoluzen_flow import main, runtime_config
        monkeypatch.setattr(runtime_config, "BIN_RETENTION_DAYS", 0)
        # Returns at once instead of sleeping until 03:50 UTC.
        asyncio.run(asyncio.wait_for(main._workflow_bin_purge_loop(), timeout=5))
