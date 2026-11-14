"""A published pipeline cannot be changed or removed by accident.

Saving makes a draft beside it, what runs on its own stays on the published
version, and deleting it, or what it depends on, is refused until someone
unpublishes it on the record.
"""
from __future__ import annotations

import uuid

import pytest

from revoluzen_flow.ir.schema import PipelineStatus, Step, StepType, Workflow
from revoluzen_flow.ir.versioning import PublishedVersionMissing, definition_hash, published_pin
from tests.conftest_fixtures_v2 import (  # noqa: F401
    admin_token, app_v2, authed_client, client, data_dir, db_fixture, role_clients,
)

pytestmark = pytest.mark.usefixtures("db_fixture")


def _steps(sql: str = "SELECT 1 AS a") -> list[dict]:
    return [
        {"id": "src", "type": "csv_source", "label": "Read", "params": {"file_path": "orders.csv"}},
        {"id": "sql", "type": "transform", "label": "Shape", "params": {"query": sql}},
    ]


def _make(client, name: str, sql: str = "SELECT 1 AS a", **extra) -> str:
    r = client.post("/api/workflows", json={
        "name": f"{name} {uuid.uuid4().hex[:6]}",
        "business_purpose": "Because it is needed",
        "steps": _steps(sql),
        "connections": [{"from_step": "src", "to_step": "sql"}],
        **extra,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _store():
    from revoluzen_flow.state import get_workflow_store
    return get_workflow_store()


def _publish(client, wid: str, note: str = "") -> dict:
    """Publish as /test would have left it: a pass for this exact definition."""
    store = _store()
    latest = store.get(wid)
    store.update_status(
        wid, PipelineStatus.TESTING,
        test_results={"status": "success", "definition_hash": definition_hash(latest.workflow)},
    )
    r = client.post(f"/api/workflows/{wid}/publish", json={"note": note})
    assert r.status_code == 200, r.text
    return r.json()


def _save(client, wid: str, sql: str) -> dict:
    wf = client.get(f"/api/workflows/{wid}").json()["workflow"]
    for step in wf["steps"]:
        if step["id"] == "sql":
            step["params"]["query"] = sql
    r = client.put(f"/api/workflows/{wid}", json={"workflow": wf, "change_summary": "Edit"})
    assert r.status_code == 200, r.text
    return r.json()


def _sql_of(version) -> str:
    return next(s.params["query"] for s in version.workflow.steps if s.id == "sql")


class TestSavingDoesNotChangeWhatIsLive:
    def test_publish_pins_the_version(self, authed_client):
        wid = _make(authed_client, "Pin")
        result = _publish(authed_client, wid)
        assert result["published_version"] == _store().get(wid).version
        body = authed_client.get(f"/api/workflows/{wid}").json()
        assert body["published_version"] == result["published_version"]
        assert body["has_unpublished_changes"] is False

    def test_a_save_is_a_draft_and_the_live_version_keeps_running(self, authed_client):
        wid = _make(authed_client, "Draft", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]

        saved = _save(authed_client, wid, "SELECT 'draft' AS a")

        assert saved["version"] == pinned + 1
        assert saved["published_version"] == pinned
        assert saved["has_unpublished_changes"] is True
        assert saved["workflow"]["status"] == "published"
        live = _store().live_version(wid)
        assert live.version == pinned and _sql_of(live) == "SELECT 'live' AS a"
        assert _sql_of(_store().get(wid)) == "SELECT 'draft' AS a"

    def test_a_writer_that_knows_nothing_of_publishing_cannot_unpublish(self, authed_client):
        """As a Copilot change does: a fresh Workflow with the same id."""
        wid = _make(authed_client, "Copilot", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        store = _store()
        before = store.get(wid).workflow

        store.save(Workflow(
            id=wid, name=before.name, workspace_id=before.workspace_id,
            steps=[Step(id="src", type=StepType.CSV_SOURCE, label="Only step")],
        ), change_summary="Modified via Copilot")

        latest = store.get(wid)
        assert latest.workflow.status == PipelineStatus.PUBLISHED
        assert latest.workflow.published_version == pinned
        assert latest.workflow.has_unpublished_changes is True
        assert _sql_of(store.live_version(wid)) == "SELECT 'live' AS a"

    def test_the_client_cannot_move_the_pin(self, authed_client):
        wid = _make(authed_client, "Tamper", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["published_version"] = 999
        wf["has_unpublished_changes"] = False
        for step in wf["steps"]:
            if step["id"] == "sql":
                step["params"]["query"] = "SELECT 'sneaky' AS a"
        authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf})
        body = authed_client.get(f"/api/workflows/{wid}").json()
        assert body["published_version"] == pinned and body["has_unpublished_changes"] is True

    def test_housekeeping_is_not_a_draft(self, authed_client):
        """A rename, a move on the canvas, or switching it off changes no step."""
        wid = _make(authed_client, "Housekeeping")
        pinned = _publish(authed_client, wid)["published_version"]
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["name"] = f"Renamed {uuid.uuid4().hex[:6]}"
        wf["steps"][0]["position"] = {"x": 900, "y": 40}
        assert authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf}).status_code == 200
        r = authed_client.post(f"/api/workflows/{wid}/toggle-active", json={"active": False})
        assert r.status_code == 200, r.text

        body = authed_client.get(f"/api/workflows/{wid}").json()
        assert body["has_unpublished_changes"] is False
        assert body["published_version"] == pinned
        assert body["version"] > pinned

    def test_the_live_version_has_the_pipelines_name_and_switch(self, authed_client):
        wid = _make(authed_client, "Switch", "SELECT 'live' AS a")
        _publish(authed_client, wid)
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["name"] = f"New name {uuid.uuid4().hex[:6]}"
        for step in wf["steps"]:
            if step["id"] == "sql":
                step["params"]["query"] = "SELECT 'draft' AS a"
        authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf})
        authed_client.post(f"/api/workflows/{wid}/toggle-active", json={"active": False})

        live = _store().live_version(wid)

        assert _sql_of(live) == "SELECT 'live' AS a"
        assert live.workflow.name == wf["name"]
        assert live.workflow.is_active_dev is False and live.workflow.is_active_prod is False

    def test_going_back_by_hand_clears_the_draft(self, authed_client):
        wid = _make(authed_client, "Back", "SELECT 'live' AS a")
        _publish(authed_client, wid)
        assert _save(authed_client, wid, "SELECT 'draft' AS a")["has_unpublished_changes"] is True
        assert _save(authed_client, wid, "SELECT 'live' AS a")["has_unpublished_changes"] is False

    def test_discard_goes_back_and_keeps_the_work_in_history(self, authed_client):
        wid = _make(authed_client, "Discard", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        draft_version = _save(authed_client, wid, "SELECT 'draft' AS a")["version"]

        r = authed_client.post(f"/api/workflows/{wid}/discard-draft")

        assert r.status_code == 200, r.text
        assert r.json()["has_unpublished_changes"] is False and r.json()["published_version"] == pinned
        assert _sql_of(_store().get(wid)) == "SELECT 'live' AS a"
        assert _sql_of(_store().get(wid, draft_version)) == "SELECT 'draft' AS a"

    def test_nothing_to_discard_is_refused(self, authed_client):
        wid = _make(authed_client, "Nothing")
        _publish(authed_client, wid)
        assert authed_client.post(f"/api/workflows/{wid}/discard-draft").status_code == 400

    def test_publishing_again_moves_it_forward(self, authed_client):
        wid = _make(authed_client, "Again", "SELECT 'one' AS a")
        _publish(authed_client, wid)
        version = _save(authed_client, wid, "SELECT 'two' AS a")["version"]
        assert _publish(authed_client, wid)["published_version"] == version
        assert _sql_of(_store().live_version(wid)) == "SELECT 'two' AS a"
        assert _store().get(wid).workflow.has_unpublished_changes is False

    def test_a_pass_only_counts_for_what_was_tested(self, authed_client):
        wid = _make(authed_client, "Stale test", "SELECT 'tested' AS a")
        store = _store()
        store.update_status(
            wid, PipelineStatus.TESTING,
            test_results={"status": "success", "definition_hash": definition_hash(store.get(wid).workflow)},
        )
        _save(authed_client, wid, "SELECT 'not tested' AS a")
        r = authed_client.post(f"/api/workflows/{wid}/publish")
        assert r.status_code == 400 and "changed since" in r.text

    def test_the_real_test_route_lets_it_publish(self, authed_client, data_dir):
        """The fingerprint must be of what was stored, not of the steps after
        the run, which the executor adds its own settings to."""
        with open(f"{data_dir}/lock_orders.csv", "w", encoding="utf-8") as handle:
            handle.write("id,amount\n1,10\n2,20\n")
        r = authed_client.post("/api/workflows", json={
            "name": f"Real test {uuid.uuid4().hex[:6]}",
            "business_purpose": "Because it is needed",
            "steps": [
                {"id": "src", "type": "csv_source", "label": "Read", "params": {"file_path": "lock_orders.csv"}},
                {"id": "out", "type": "csv_sink", "label": "Write", "params": {"file_path": "out/lock.csv"}},
            ],
            "connections": [{"from_step": "src", "to_step": "out"}],
        })
        wid = r.json()["id"]

        tested = authed_client.post(f"/api/workflows/{wid}/test")
        assert tested.status_code == 200 and tested.json()["status"] == "success", tested.text

        published = authed_client.post(f"/api/workflows/{wid}/publish")
        assert published.status_code == 200, published.text
        assert authed_client.get(f"/api/workflows/{wid}").json()["has_unpublished_changes"] is False

    def test_testing_a_published_pipeline_does_not_publish_the_draft(self, authed_client):
        wid = _make(authed_client, "Test draft", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        published_at = _store().get(wid).workflow.published_at
        _save(authed_client, wid, "SELECT 'draft' AS a")
        store = _store()

        store.update_status(wid, PipelineStatus.TESTING)
        assert _sql_of(store.live_version(wid)) == "SELECT 'live' AS a"
        store.update_status(wid, PipelineStatus.PUBLISHED, test_results={"status": "success"}, restoring=True)

        latest = store.get(wid)
        assert latest.workflow.status == PipelineStatus.PUBLISHED
        assert latest.workflow.published_version == pinned
        assert latest.workflow.published_at == published_at

    def test_a_pipeline_published_before_the_pin_existed(self, authed_client):
        """It has no pin, and its newest version is what has been running."""
        wid = _make(authed_client, "Legacy", "SELECT 'live' AS a")
        store = _store()
        store.update_status(wid, PipelineStatus.PUBLISHED)
        row = store._db.fetchone("SELECT version, data FROM workflow_versions WHERE workflow_id = ? ORDER BY version DESC LIMIT 1", (wid,))
        import json
        data = json.loads(row["data"])
        data["workflow"].pop("published_version", None)
        store._db.execute_write(
            "UPDATE workflow_versions SET data = ? WHERE workflow_id = ? AND version = ?",
            (json.dumps(data), wid, row["version"]),
        )

        assert published_pin(store.get(wid)) == row["version"]
        listed = next(w for w in store.list_all() if w["id"] == wid)
        assert listed["published_version"] == row["version"]

        saved = _save(authed_client, wid, "SELECT 'draft' AS a")

        assert saved["published_version"] == row["version"] and saved["has_unpublished_changes"] is True
        assert _sql_of(store.live_version(wid)) == "SELECT 'live' AS a"

    def test_a_missing_published_version_stops_rather_than_runs_a_draft(self, authed_client):
        wid = _make(authed_client, "Missing", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        _save(authed_client, wid, "SELECT 'draft' AS a")
        store = _store()
        store._db.execute_write("DELETE FROM workflow_versions WHERE workflow_id = ? AND version = ?", (wid, pinned))

        with pytest.raises(PublishedVersionMissing):
            store.live_version(wid)
        assert authed_client.post(f"/api/execute/workflow/{wid}").status_code == 409

    def test_the_live_version_is_never_pruned(self, authed_client, monkeypatch):
        from revoluzen_flow import runtime_config
        monkeypatch.setattr(runtime_config, "VERSION_RETENTION_COUNT", 2)
        monkeypatch.setattr(runtime_config, "AUTOSAVE_RETENTION_COUNT", 2)
        wid = _make(authed_client, "Prune", "SELECT 0 AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        for n in range(1, 7):
            _save(authed_client, wid, f"SELECT {n} AS a")
        assert _store().get(wid, pinned) is not None

    def test_history_marks_the_live_version(self, authed_client):
        wid = _make(authed_client, "History")
        pinned = _publish(authed_client, wid)["published_version"]
        _save(authed_client, wid, "SELECT 2 AS a")
        history = authed_client.get(f"/api/workflows/{wid}/history").json()
        assert history["published_version"] == pinned and history["has_unpublished_changes"] is True
        assert [v["version"] for v in history["versions"] if v["live"]] == [pinned]

    def test_a_copy_of_a_published_pipeline_starts_as_a_draft(self, authed_client):
        wid = _make(authed_client, "Original")
        _publish(authed_client, wid)
        clone = authed_client.post(f"/api/workflows/{wid}/clone").json()["id"]
        body = authed_client.get(f"/api/workflows/{clone}").json()
        assert body["published_version"] is None and body["workflow"]["status"] == "draft"


class TestWhatRunsOnItsOwn:
    def test_a_run_with_no_flag_uses_the_published_version(self, authed_client):
        wid = _make(authed_client, "Run", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        _save(authed_client, wid, "SELECT 'draft' AS a")
        seen = []
        import revoluzen_flow.api.execution as execution

        async def fake_run(v, **kwargs):
            seen.append(v)
            return {"status": "success", "step_results": {}}

        original = execution.run_saved_workflow
        execution.run_saved_workflow = fake_run
        try:
            assert authed_client.post(f"/api/execute/workflow/{wid}").status_code == 200
            assert authed_client.post(f"/api/execute/workflow/{wid}?draft=true").status_code == 200
        finally:
            execution.run_saved_workflow = original
        assert seen[0].version == pinned and _sql_of(seen[0]) == "SELECT 'live' AS a"
        assert _sql_of(seen[1]) == "SELECT 'draft' AS a"

    def test_a_scheduled_run_uses_the_published_version(self, authed_client):
        from revoluzen_flow.main import app_state
        from revoluzen_flow.scheduling.scheduler import PipelineScheduler

        wid = _make(authed_client, "Sched", "SELECT 'live' AS a")
        pinned = _publish(authed_client, wid)["published_version"]
        _save(authed_client, wid, "SELECT 'draft' AS a")
        store = app_state["store"]
        seen: list[int] = []
        real = store.live_version

        def spy(*args, **kwargs):
            version = real(*args, **kwargs)
            seen.append(version.version)
            return version

        store.live_version = spy
        scheduler = PipelineScheduler.__new__(PipelineScheduler)
        scheduler._running_jobs = {}
        scheduler._run_threads = {}
        try:
            scheduler._run_pipeline("sched-1", wid, "Nightly", "default")
        except Exception:
            pass  # the run itself may fail on the sample file; which version it took is the point
        finally:
            del store.live_version
        assert seen and seen[0] == pinned


class TestDeleting:
    def test_delete_is_refused_even_permanent(self, authed_client):
        wid = _make(authed_client, "Delete")
        _publish(authed_client, wid)
        for url in (f"/api/workflows/{wid}", f"/api/workflows/{wid}?permanent=true"):
            r = authed_client.delete(url)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "published_pipeline"
            assert "Unpublish it first" in r.json()["detail"]["message"]
        assert authed_client.get(f"/api/workflows/{wid}").status_code == 200

    def test_archive_is_refused(self, authed_client):
        wid = _make(authed_client, "Archive")
        _publish(authed_client, wid)
        r = authed_client.post(f"/api/workflows/{wid}/archive")
        assert r.status_code == 409 and "Unpublish" in r.json()["detail"]["message"]

    def test_after_unpublishing_it_can_go_to_the_bin(self, authed_client):
        wid = _make(authed_client, "Unpublish")
        _publish(authed_client, wid)
        r = authed_client.post(f"/api/workflows/{wid}/revoke", json={"note": "Replaced by the new load"})
        assert r.status_code == 200, r.text
        body = authed_client.get(f"/api/workflows/{wid}").json()
        assert body["published_version"] is None and body["workflow"]["status"] == "draft"
        assert authed_client.delete(f"/api/workflows/{wid}").status_code == 200
        authed_client.delete(f"/api/workflows/bin/{wid}")

    def test_unpublishing_pauses_the_schedules(self, authed_client):
        wid = _make(authed_client, "Pause")
        _publish(authed_client, wid)
        created = authed_client.post("/api/schedules", json={
            "workflow_id": wid, "name": "Nightly", "schedule_type": "cron",
            "cron_expression": "0 2 * * *", "enabled": True, "acknowledge_side_effects": True,
        })
        assert created.status_code in (200, 201), created.text
        r = authed_client.post(f"/api/workflows/{wid}/revoke")
        assert r.json()["schedules_paused"] == 1
        assert authed_client.get(f"/api/schedules/{created.json()['id']}").json()["enabled"] is False

    def test_one_that_a_published_pipeline_runs_cannot_be_deleted(self, authed_client):
        child = _make(authed_client, "Child")
        r = authed_client.post("/api/workflows", json={
            "name": f"Parent {uuid.uuid4().hex[:6]}",
            "business_purpose": "Runs the child",
            "steps": [{"id": "run", "type": "execute_pipeline", "label": "Run it", "params": {"pipeline_id": child}}],
        })
        parent = r.json()["id"]
        _publish(authed_client, parent)

        refused = authed_client.delete(f"/api/workflows/{child}")

        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "used_by_published_pipeline"
        assert refused.json()["detail"]["pipelines"][0]["id"] == parent


@pytest.mark.parametrize("role,expected", [("viewer", 403), ("analyst", 403), ("data_engineer", 200)])
def test_who_can_unpublish(authed_client, role_clients, role, expected):
    wid = _make(authed_client, f"Role {role}")
    _publish(authed_client, wid)
    assert role_clients[role].post(f"/api/workflows/{wid}/revoke").status_code == expected


class TestWhatItDependsOn:
    def _published_using(self, client, params: dict, step_type: str = "db_source") -> str:
        r = client.post("/api/workflows", json={
            "name": f"Uses {uuid.uuid4().hex[:6]}",
            "business_purpose": "Reads something",
            "steps": [{"id": "s", "type": step_type, "label": "Read", "params": params}],
        })
        assert r.status_code in (200, 201), r.text
        wid = r.json()["id"]
        _publish(client, wid)
        return wid

    def _connection(self, client, **extra) -> dict:
        r = client.post("/api/connections", json={
            "name": f"Conn {uuid.uuid4().hex[:6]}", "type": "postgresql",
            "config": {"host": "db.example.com", "port": 5432, "database": "sales"}, **extra,
        })
        assert r.status_code in (200, 201), r.text
        return r.json()

    def test_a_connection_in_use_cannot_be_deleted(self, authed_client):
        connection = self._connection(authed_client)
        wid = self._published_using(authed_client, {"connection_id": connection["id"], "query": "SELECT 1"})

        refused = authed_client.delete(f"/api/connections/{connection['id']}")

        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "used_by_published_pipeline" and detail["pipelines"][0]["id"] == wid
        assert "unpublish" in detail["message"]
        assert authed_client.get(f"/api/connections/{connection['id']}").status_code == 200

    def test_a_draft_using_it_does_not_block(self, authed_client):
        connection = self._connection(authed_client)
        r = authed_client.post("/api/workflows", json={
            "name": f"Draft use {uuid.uuid4().hex[:6]}", "steps": [
                {"id": "s", "type": "db_source", "params": {"connection_id": connection["id"], "query": "SELECT 1"}},
            ],
        })
        assert r.status_code in (200, 201)
        assert authed_client.delete(f"/api/connections/{connection['id']}").status_code == 200

    def test_the_live_version_counts_not_the_draft(self, authed_client):
        connection = self._connection(authed_client)
        wid = self._published_using(authed_client, {"connection_id": connection["id"], "query": "SELECT 1"})
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["steps"][0]["params"]["connection_id"] = "another"
        authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf})

        assert authed_client.delete(f"/api/connections/{connection['id']}").status_code == 409

    def test_after_unpublishing_it_can_be_deleted(self, authed_client):
        connection = self._connection(authed_client)
        wid = self._published_using(authed_client, {"connection_id": connection["id"], "query": "SELECT 1"})
        authed_client.post(f"/api/workflows/{wid}/revoke")
        assert authed_client.delete(f"/api/connections/{connection['id']}").status_code == 200

    def test_a_credential_used_through_a_connection(self, authed_client):
        created = authed_client.post("/api/credentials", json={
            "name": f"Cred {uuid.uuid4().hex[:6]}", "type": "postgresql", "config": {"password": "s3cret-value"},
        })
        assert created.status_code in (200, 201), created.text
        credential = created.json()
        connection = self._connection(authed_client, credential_id=credential["id"])
        wid = self._published_using(authed_client, {"connection_id": connection["id"], "query": "SELECT 1"})

        refused = authed_client.delete(f"/api/credentials/{credential['id']}")

        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["pipelines"][0]["id"] == wid

    def test_an_uploaded_file_in_use_cannot_be_deleted(self, authed_client):
        from revoluzen_flow.datastore.models import StorageObject
        from revoluzen_flow.datastore.store import get_store as get_datastore

        path = f"workspaces/default/uploads/{uuid.uuid4().hex[:6]}.csv"
        obj = StorageObject(workspace_id="default", name="orders.csv", path=path)
        get_datastore().save_object(obj)
        wid = self._published_using(authed_client, {"file_path": path}, step_type="csv_source")

        refused = authed_client.delete(f"/api/storage/file/{obj.id}")

        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["pipelines"][0]["id"] == wid
        assert get_datastore().get_object(obj.id, workspace_id="default").deleted_at is None

    def test_a_managed_table_in_use_cannot_be_dropped(self, authed_client):
        from revoluzen_flow.datastore.models import StorageTable
        from revoluzen_flow.datastore.store import get_store as get_datastore

        name = f"orders_{uuid.uuid4().hex[:6]}"
        table = StorageTable(
            workspace_id="default", schema_name="sales", name=name, path=f"tables/sales/{name}",
        )
        get_datastore().save_table(table)
        wid = self._published_using(
            authed_client, {"schema_name": "sales", "table_name": name}, step_type="local_table_source",
        )

        refused = authed_client.delete(f"/api/storage/tables/{table.id}")

        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["pipelines"][0]["id"] == wid
        assert get_datastore().get_table(table.id, workspace_id="default") is not None
