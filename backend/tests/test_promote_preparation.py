"""Every managed table made from a file has a preparation to edit.

Promote saves a pipeline that does what it did (this file, the columns
chosen, into this table): Source -> Data Wrangler -> Managed Table Sink, the
shape Clean & promote scaffolds. The table records it and its version.
Promoting into the same table again gives that pipeline a new version; a
pipeline someone built that writes the table is never changed. A table
promoted before Promote saved a preparation can be given one.

Data Prep's Load to Managed Table saves its canvas the same way, through
POST /api/storage/preparations, and a table loaded before it did can get its
kept steps back as a pipeline.

Same loopback-client harness as tests/test_storage_rescan_drop.py.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2,
    DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD,
)

LOOPBACK_BASE = "http://127.0.0.1"
WS = "default"
CSV = b"id,name,amount\n1,Asha,10\n2,Ravi,20\n3,Meera,30\n"


def _reset_admin_password() -> None:
    from revoluzen_flow.main import app_state
    from revoluzen_flow.auth.models import User
    store = app_state.get("user_store")
    if store is None:
        return
    admin = store.get_user_by_email(DEV_ADMIN_EMAIL)
    if admin is None:
        return
    admin.password_hash = User.hash_password(DEV_ADMIN_PASSWORD)
    admin.is_active = True
    store._save_user(admin)


@pytest.fixture(scope="module")
def client(app_v2):
    with TestClient(app_v2, base_url=LOOPBACK_BASE) as anon:
        _reset_admin_password()
        r = anon.post("/api/auth/login", json={"email": DEV_ADMIN_EMAIL, "password": DEV_ADMIN_PASSWORD})
        if r.status_code != 200 or not r.json().get("token"):
            pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
        tok = r.json()["token"]
    c = TestClient(app_v2, base_url=LOOPBACK_BASE)
    c.headers["Authorization"] = f"Bearer {tok}"
    c.headers["X-Workspace-Id"] = WS
    c.cookies.set("session", tok)
    c.cookies.set("revoluzen_flow_session", tok)
    with c:
        yield c


def _upload(client: TestClient, name: str, content: bytes = CSV) -> dict:
    r = client.post("/api/storage/upload", files={"file": (name, content, "text/csv")})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _promote(client: TestClient, obj: dict, table: str, column_map: dict | None = None) -> dict:
    r = client.post("/api/storage/promote-to-table", json={
        "object_id": obj["id"], "schema_name": "prep_test", "table_name": table,
        "column_map": column_map or {},
    })
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _workflow(client: TestClient, wf_id: str) -> dict:
    r = client.get(f"/api/workflows/{wf_id}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    return body.get("workflow", body)


def _steps_by_type(wf: dict) -> dict:
    return {s["type"]: s for s in wf["steps"]}


def test_promote_saves_an_editable_preparation(client):
    obj = _upload(client, f"people-{uuid.uuid4().hex[:6]}.csv")
    table = _promote(client, obj, "people", {"name": "full_name", "amount": "amount"})

    assert table["prep_workflow_id"]
    assert table["prep_workflow_version"] == 1
    assert table["prep_workflow_name"].endswith("data prep")
    assert table["load_history"][0]["source"] == "promote"
    assert table["load_history"][0]["rows"] == 3

    wf = _workflow(client, table["prep_workflow_id"])
    steps = _steps_by_type(wf)
    assert steps["source"]["params"]["file_path"] == obj["path"]
    sub = steps["data_wrangler"]["params"]["steps"]
    assert [s["op"] for s in sub] == ["select", "rename"]
    assert sub[0]["config"]["columns"] == ["name", "amount"]
    assert sub[1]["config"]["rename_map"] == {"name": "full_name"}
    sink = steps["local_table_sink"]["params"]
    assert (sink["schema_name"], sink["table_name"], sink["mode"]) == ("prep_test", "people", "replace")
    # Opens in the Editor's Data Prep view, and knows its table.
    assert wf["metadata"]["scaffolded_from"] == "storage_file"
    assert wf["metadata"]["promoted_table_id"] == table["id"]


def test_promoting_again_gives_that_preparation_a_new_version(client):
    obj = _upload(client, f"again-{uuid.uuid4().hex[:6]}.csv")
    first = _promote(client, obj, "again", {"name": "full_name"})
    second = _promote(client, obj, "again")

    assert second["prep_workflow_id"] == first["prep_workflow_id"]
    assert second["prep_workflow_version"] == 2
    assert [h["version"] for h in second["load_history"][:2]] == [2, 1]
    wf = _workflow(client, second["prep_workflow_id"])
    assert _steps_by_type(wf)["data_wrangler"]["params"]["steps"] == [], "no column choice this time"


def test_a_pipeline_someone_built_is_never_rewritten(client):
    from revoluzen_flow.datastore.store import get_store as get_datastore

    obj = _upload(client, f"built-{uuid.uuid4().hex[:6]}.csv")
    table = _promote(client, obj, "built")
    # Someone's own pipeline is the latest writer of the table.
    r = client.post("/api/workflows/", json={"name": f"Built by hand {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, r.text[:300]
    own_id = r.json()["id"]
    ds = get_datastore()
    row = ds.get_table(table["id"], workspace_id=WS)
    row.prep_workflow_id = own_id
    ds.save_table(row)

    again = _promote(client, obj, "built")
    assert again["prep_workflow_id"] not in (own_id, table["prep_workflow_id"]), "a new preparation, not theirs"
    assert again["prep_workflow_version"] == 1
    versions = client.get(f"/api/workflows/{own_id}/versions").json()
    assert len(versions) == 1, "their pipeline is untouched"


def test_an_older_promoted_table_can_be_made_editable(client):
    from revoluzen_flow.datastore.store import get_store as get_datastore

    obj = _upload(client, f"older-{uuid.uuid4().hex[:6]}.csv")
    table = _promote(client, obj, "older", {"id": "id", "name": "name"})
    # As if promoted before Promote saved a preparation.
    ds = get_datastore()
    row = ds.get_table(table["id"], workspace_id=WS)
    row.prep_workflow_id = None
    row.prep_workflow_version = None
    ds.save_table(row)

    r = client.post(f"/api/storage/tables/{table['id']}/preparation")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["warning"] is None
    assert body["table"]["prep_workflow_version"] == 1, "its columns were kept, so it holds what v1 makes"
    sub = _steps_by_type(_workflow(client, body["workflow_id"]))["data_wrangler"]["params"]["steps"]
    assert [s["op"] for s in sub] == ["select"]
    assert sub[0]["config"]["columns"] == ["id", "name"]

    again = client.post(f"/api/storage/tables/{table['id']}/preparation")
    assert again.status_code == 409


def test_renamed_columns_are_flagged_when_made_editable(client):
    from revoluzen_flow.datastore.store import get_store as get_datastore

    obj = _upload(client, f"renamed-{uuid.uuid4().hex[:6]}.csv")
    table = _promote(client, obj, "renamed", {"name": "full_name"})
    ds = get_datastore()
    row = ds.get_table(table["id"], workspace_id=WS)
    row.prep_workflow_id = None
    ds.save_table(row)

    body = client.post(f"/api/storage/tables/{table['id']}/preparation").json()
    assert body["warning"] and "Rename" in body["warning"]
    assert body["table"]["prep_workflow_version"] is None, "the table does not hold what this version makes"


# ── Data Prep: Load to Managed Table saves the preparation ──────────────

KEEP_ABOVE_ONE = [{
    "id": "s_keep", "op": "filter", "enabled": True, "label": "Filter rows",
    "config": {"mode": "expression", "expression": "id > 1", "rules": [], "combinator": "AND"},
}]
KEEP_ABOVE_TWO = [{
    "id": "s_keep", "op": "filter", "enabled": True, "label": "Filter rows",
    "config": {"mode": "expression", "expression": "id > 2", "rules": [], "combinator": "AND"},
}]


def _canvas(client: TestClient, obj: dict, table: str, steps: list | None = None, schema: str = "prep_test") -> dict:
    """What the editor holds after Prep on a file: the scaffold with the
    user's steps in it, and no id, because it has never been saved."""
    r = client.post("/api/storage/scaffold-cleanup", json={
        "object_id": obj["id"], "target_schema": schema, "target_table": table,
    })
    assert r.status_code == 200, r.text[:300]
    wf = r.json()["workflow"]
    for step in wf["steps"]:
        if step["type"] == "data_wrangler":
            step["params"]["steps"] = steps or []
    wf["id"] = ""
    return wf


def _save_canvas(client: TestClient, wf: dict) -> dict:
    r = client.post("/api/storage/preparations", json={"workflow": wf})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _load(client: TestClient, wf_id: str) -> None:
    r = client.post(f"/api/execute/workflow/{wf_id}?full_run=true&environment=dev&safety_mode=live")
    assert r.status_code == 200, r.text[:500]
    assert r.json()["status"] == "success", r.text[:500]


def _table(client: TestClient, name: str, schema: str = "prep_test") -> dict:
    tables = client.get("/api/storage/tables").json()["tables"]
    return next(t for t in tables if (t["schema_name"], t["name"]) == (schema, name))


def _versions(client: TestClient, wf_id: str) -> list:
    r = client.get(f"/api/workflows/{wf_id}/versions")
    assert r.status_code == 200, r.text[:300]
    return r.json()


def test_a_load_saves_the_preparation_and_the_table_records_it(client):
    obj = _upload(client, f"load-{uuid.uuid4().hex[:6]}.csv")
    saved = _save_canvas(client, _canvas(client, obj, "loaded", KEEP_ABOVE_ONE))

    assert saved["created"] is True
    assert saved["version"] == 1
    assert saved["workflow_name"] == "prep_test.loaded data prep"
    wf = _workflow(client, saved["workflow_id"])
    assert _steps_by_type(wf)["data_wrangler"]["params"]["steps"] == KEEP_ABOVE_ONE
    # It reopens as Data Prep, and knows its file.
    assert wf["metadata"]["scaffolded_from"] == "storage_file"
    assert wf["metadata"]["source_object_id"] == obj["id"]
    assert wf["metadata"]["source_object_name"] == obj["name"]

    _load(client, saved["workflow_id"])
    table = _table(client, "loaded")
    assert table["row_count"] == 2
    assert table["prep_workflow_id"] == saved["workflow_id"]
    assert table["prep_workflow_name"] == saved["workflow_name"]
    assert table["prep_workflow_version"] == 1
    assert table["prep_source_object_id"] == obj["id"]


def test_loading_the_same_table_again_adds_a_version(client):
    obj = _upload(client, f"reload-{uuid.uuid4().hex[:6]}.csv")
    first = _save_canvas(client, _canvas(client, obj, "reloaded", KEEP_ABOVE_ONE))
    _load(client, first["workflow_id"])

    # A fresh Prep on the file, loaded into the table that has a preparation.
    second = _save_canvas(client, _canvas(client, obj, "reloaded", KEEP_ABOVE_TWO))
    assert second["created"] is False
    assert second["workflow_id"] == first["workflow_id"]
    assert second["version"] == 2
    assert second["workflow_name"] == first["workflow_name"]

    _load(client, second["workflow_id"])
    table = _table(client, "reloaded")
    assert table["row_count"] == 1
    assert table["prep_workflow_version"] == 2
    assert [h["version"] for h in table["load_history"][:2]] == [2, 1]


def test_a_load_that_changes_nothing_adds_no_version(client):
    obj = _upload(client, f"same-{uuid.uuid4().hex[:6]}.csv")
    canvas = _canvas(client, obj, "same", KEEP_ABOVE_ONE)
    first = _save_canvas(client, canvas)
    _load(client, first["workflow_id"])

    again = _save_canvas(client, canvas)
    assert again["unchanged"] is True
    assert again["version"] == 1
    assert len(_versions(client, first["workflow_id"])) == 1


def test_data_prep_never_rewrites_a_pipeline_someone_built(client):
    from revoluzen_flow.datastore.store import get_store as get_datastore

    obj = _upload(client, f"own-{uuid.uuid4().hex[:6]}.csv")
    first = _save_canvas(client, _canvas(client, obj, "own_table", KEEP_ABOVE_ONE))
    _load(client, first["workflow_id"])
    # Someone's own pipeline is the latest writer of the table.
    r = client.post("/api/workflows/", json={"name": f"By hand {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, r.text[:300]
    own_id = r.json()["id"]
    ds = get_datastore()
    row = ds.get_table(_table(client, "own_table")["id"], workspace_id=WS)
    row.prep_workflow_id = own_id
    ds.save_table(row)

    again = _save_canvas(client, _canvas(client, obj, "own_table", KEEP_ABOVE_TWO))
    assert again["created"] is True
    assert again["workflow_id"] not in (own_id, first["workflow_id"])
    assert len(_versions(client, own_id)) == 1, "their pipeline is untouched"


def test_data_prep_never_rewrites_a_published_preparation(client):
    from revoluzen_flow.ir.schema import PipelineStatus
    from revoluzen_flow.main import app_state

    obj = _upload(client, f"live-{uuid.uuid4().hex[:6]}.csv")
    first = _save_canvas(client, _canvas(client, obj, "live_table", KEEP_ABOVE_ONE))
    _load(client, first["workflow_id"])
    app_state["store"].update_status(first["workflow_id"], PipelineStatus.PUBLISHED)

    again = _save_canvas(client, _canvas(client, obj, "live_table", KEEP_ABOVE_TWO))
    assert again["created"] is True, "schedules run the latest saved version of a published pipeline"
    assert again["workflow_id"] != first["workflow_id"]
    assert _steps_by_type(_workflow(client, first["workflow_id"]))["data_wrangler"]["params"]["steps"] == KEEP_ABOVE_ONE


def test_two_tables_from_one_file_each_get_a_preparation(client):
    obj = _upload(client, f"both-{uuid.uuid4().hex[:6]}.csv")
    one = _save_canvas(client, _canvas(client, obj, "people", KEEP_ABOVE_ONE, schema="prep_one"))
    two = _save_canvas(client, _canvas(client, obj, "people", KEEP_ABOVE_TWO, schema="prep_two"))

    assert one["workflow_id"] != two["workflow_id"]
    assert one["workflow_name"] == "prep_one.people data prep"
    assert two["workflow_name"] == "prep_two.people data prep"


def test_a_name_the_user_typed_is_kept(client):
    obj = _upload(client, f"named-{uuid.uuid4().hex[:6]}.csv")
    canvas = _canvas(client, obj, "named")
    canvas["name"] = f"Monthly cleanup {uuid.uuid4().hex[:6]}"
    assert _save_canvas(client, canvas)["workflow_name"] == canvas["name"]


def test_a_canvas_with_no_sink_is_saved_under_its_own_name(client):
    obj = _upload(client, f"nosink-{uuid.uuid4().hex[:6]}.csv")
    canvas = _canvas(client, obj, "nosink")
    sink_id = next(s["id"] for s in canvas["steps"] if s["type"] == "local_table_sink")
    canvas["steps"] = [s for s in canvas["steps"] if s["id"] != sink_id]
    canvas["connections"] = [c for c in canvas["connections"] if c["to_step"] != sink_id]

    saved = _save_canvas(client, canvas)
    assert saved["created"] is True
    assert saved["workflow_name"] == canvas["name"]


def test_only_what_data_prep_records_is_kept_from_the_canvas(client):
    obj = _upload(client, f"meta-{uuid.uuid4().hex[:6]}.csv")
    canvas = _canvas(client, obj, "meta_table")
    canvas["metadata"] = {
        "scaffolded_from": "something_else",
        "source_object_id": "obj_from_another_workspace",
        "promoted_table_id": "tbl_someone_elses",
    }

    wf = _workflow(client, _save_canvas(client, canvas)["workflow_id"])
    assert wf["metadata"] == {"scaffolded_from": "storage_file"}


def test_saving_a_preparation_takes_someone_who_can_author_pipelines(client, app_v2):
    from tests.conftest_fixtures_v2 import ROLE_CREDS, _ensure_user, _login

    email, password = ROLE_CREDS["viewer"]
    if not _ensure_user(client, email, password, "viewer"):
        pytest.skip("viewer not provisioned")
    viewer = TestClient(app_v2, base_url=LOOPBACK_BASE)
    token = _login(viewer, email, password)
    if not token:
        pytest.skip("viewer could not sign in")
    viewer.headers["Authorization"] = f"Bearer {token}"
    viewer.headers["X-Workspace-Id"] = WS

    obj = _upload(client, f"viewer-{uuid.uuid4().hex[:6]}.csv")
    table = _promote(client, obj, "viewer_table")
    assert viewer.post("/api/storage/preparations", json={"workflow": _canvas(client, obj, "viewer_new")}).status_code == 403
    assert viewer.post(f"/api/storage/tables/{table['id']}/preparation").status_code == 403
    viewer.close()


# ── A table loaded before Data Prep saved its steps ─────────────────────


def _left_by_an_unsaved_prep(client: TestClient, obj: dict, table: str, *, keep_file_link: bool = False) -> dict:
    """A table as a Data Prep that was never saved left it: its steps and
    its load, and no pipeline or (usually) file."""
    from revoluzen_flow.datastore.store import get_store as get_datastore

    saved = _save_canvas(client, _canvas(client, obj, table, KEEP_ABOVE_ONE))
    _load(client, saved["workflow_id"])
    r = client.delete(f"/api/workflows/{saved['workflow_id']}?permanent=true")
    assert r.status_code == 200, r.text[:300]
    ds = get_datastore()
    row = ds.get_table(_table(client, table)["id"], workspace_id=WS)
    row.prep_workflow_id = None
    row.prep_workflow_version = None
    row.prep_loaded_at = None
    if not keep_file_link:
        row.prep_source_object_id = None
    row.load_history = [{**row.load_history[0], "workflow_id": None, "version": None}]
    ds.save_table(row)
    return row.model_dump(mode="json")


def test_steps_a_table_kept_become_its_preparation(client):
    tag = uuid.uuid4().hex[:6]
    obj = _upload(client, f"byname-{tag}.csv")
    table = _left_by_an_unsaved_prep(client, obj, f"byname_{tag}")

    r = client.post(f"/api/storage/tables/{table['id']}/preparation")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert obj["name"] in body["warning"], "it says which file it picked"
    assert body["workflow_name"] == f"prep_test.byname_{tag} data prep"
    assert body["table"]["prep_workflow_version"] == 1
    assert body["table"]["prep_source_object_id"] == obj["id"]
    assert body["table"]["prep_loaded_at"] == table["load_history"][0]["at"].replace("+00:00", "Z")

    wf = _workflow(client, body["workflow_id"])
    assert _steps_by_type(wf)["data_wrangler"]["params"]["steps"] == KEEP_ABOVE_ONE
    assert wf["metadata"]["scaffolded_from"] == "storage_file"
    assert "promoted_table_id" not in wf["metadata"], "Promote does not own it"

    # It runs, and reloads the table with the same rows.
    _load(client, body["workflow_id"])
    assert _table(client, f"byname_{tag}")["row_count"] == 2


def test_a_file_can_be_matched_by_the_tables_columns(client):
    tag = uuid.uuid4().hex[:6]
    obj = _upload(client, f"columns-{tag}.csv", f"id,col_{tag},amount\n1,a,10\n2,b,20\n".encode())
    table = _left_by_an_unsaved_prep(client, obj, f"renamed_{tag}")

    body = client.post(f"/api/storage/tables/{table['id']}/preparation").json()
    assert body["table"]["prep_source_object_id"] == obj["id"]
    assert "columns" in body["warning"]


def test_a_recorded_file_is_used_without_guessing(client):
    tag = uuid.uuid4().hex[:6]
    obj = _upload(client, f"recorded-{tag}.csv")
    table = _left_by_an_unsaved_prep(client, obj, f"anything_{tag}", keep_file_link=True)

    body = client.post(f"/api/storage/tables/{table['id']}/preparation").json()
    assert body["warning"] is None
    assert body["table"]["prep_source_object_id"] == obj["id"]


def test_when_several_files_could_have_made_a_table_nothing_is_guessed(client):
    tag = uuid.uuid4().hex[:6]
    content = f"id,dup_{tag}\n1,a\n2,b\n".encode()
    first = _upload(client, f"dup-a-{tag}.csv", content)
    second = _upload(client, f"dup-b-{tag}.csv", content)
    table = _left_by_an_unsaved_prep(client, first, f"either_{tag}")

    r = client.post(f"/api/storage/tables/{table['id']}/preparation")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert first["name"] in detail and second["name"] in detail
    assert _table(client, f"either_{tag}")["prep_workflow_id"] is None


def test_when_no_file_matches_it_says_what_to_do(client):
    tag = uuid.uuid4().hex[:6]
    obj = _upload(client, f"gone-{tag}.csv", f"id,gone_{tag}\n1,a\n2,b\n".encode())
    table = _left_by_an_unsaved_prep(client, obj, f"orphan_{tag}")
    assert client.delete(f"/api/storage/file/{obj['id']}").status_code == 200

    r = client.post(f"/api/storage/tables/{table['id']}/preparation")
    assert r.status_code == 400
    assert "Upload the file again" in r.json()["detail"]
