"""A connection keeps a version each time its settings change.

Founders' request (10 Nov 2026): compare earlier versions with the current
one, use them, and keep versions for connections as well as pipelines. The
history never holds a password: each secret is a keyed fingerprint, so it can
say a password changed and nothing more, and restoring an older version keeps
today's password unless the old one is typed in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import revoluzen_flow.main as main_module
from revoluzen_flow import runtime_config
from revoluzen_flow.api import connections as connections_api
from revoluzen_flow.auth.deps import require_auth
from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
from revoluzen_flow.connections.models import Connection
from revoluzen_flow.connections.store import ConnectionStore
from revoluzen_flow.connections.versions import restore_config
from revoluzen_flow.datastore import usage
from revoluzen_flow.security.encryptor import fresh_encryptor_for_tests
from revoluzen_flow.storage.database import SCHEMA_VERSION, Database

PASSWORD = "synthetic-db-password-7Q"
NEW_PASSWORD = "synthetic-db-password-8R"
URL = "/api/connections/"
EDITOR = "tester@example.test"


@pytest.fixture
def encryptor():
    return fresh_encryptor_for_tests()


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "revoluzen_flow.db"))
    yield database
    database.close()


@pytest.fixture
def store(db, encryptor, monkeypatch):
    connection_store = ConnectionStore(db=db)
    monkeypatch.setattr(connection_store, "_get_encryptor", lambda: encryptor)
    state = {"db": db, "encryptor": encryptor, "connection_store": connection_store, "credential_store": None}
    for key, value in state.items():
        monkeypatch.setitem(main_module.app_state, key, value)
    # No pipelines here; keep the module's usage cache out of it.
    monkeypatch.setattr(usage, "compute_connection_usage_cached", lambda workspace_id: {})
    return connection_store


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(connections_api.router)
    app.dependency_overrides[require_auth] = lambda: SimpleNamespace(
        id="u1", email=EDITOR, role="admin", is_active=True,
    )
    app.dependency_overrides[connections_api._safe_workspace_id] = lambda: "default"
    with TestClient(app) as test_client:
        yield test_client


def _config(host="db1.example.test", password=PASSWORD, **extra):
    config = {"host": host, "database": "orders", "username": "reader", **extra}
    if password is not None:
        config["password"] = password
    return config


def _create(client, *, name="Orders DB", config=None, **fields) -> str:
    body = {"name": name, "type": "postgresql", "config": config if config is not None else _config(), **fields}
    response = client.post(URL, json=body)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _edit(client, connection_id, **body):
    response = client.put(f"{URL}{connection_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _history(client, connection_id) -> dict:
    response = client.get(f"{URL}{connection_id}/versions")
    assert response.status_code == 200, response.text
    return response.json()


def _restore(client, connection_id, version, **body):
    return client.post(f"{URL}{connection_id}/versions/{version}/restore", json=body or None)


def _no_secret_in(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text
    assert "ENC:v1:" not in text  # not even encrypted
    assert "FP:v1:" not in text   # the API shows short tags, never whole fingerprints


# ── What a version keeps ────────────────────────────────────────────────


def test_a_new_connection_is_version_1_by_the_signed_in_user(client):
    connection_id = _create(client)
    [first] = _history(client, connection_id)["versions"]
    assert first["version"] == 1
    assert first["note"] == "Created"
    assert first["created_by"] == EDITOR
    assert first["kind"] == "saved"
    assert first["same_as_current"] is True
    assert first["settings"]["config"]["host"] == "db1.example.test"


def test_each_change_keeps_a_version_that_says_what_changed(client):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=SECRET_PLACEHOLDER))
    _edit(client, connection_id, config=_config(host="db2.example.test", password=NEW_PASSWORD), description="Nightly")
    versions = _history(client, connection_id)["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert versions[1]["note"] == "Host changed"
    assert versions[1]["changed"] == ["host"]
    assert versions[0]["changed"] == ["password", "description"]
    assert versions[0]["note"] == "Password and description changed"
    assert [v["same_as_current"] for v in versions] == [True, False, False]


def test_a_save_that_changes_nothing_keeps_no_version(client, store):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(password=SECRET_PLACEHOLDER))  # the form saved unchanged
    store.update(connection_id, {  # what a connection test writes
        "last_test_at": datetime.now(timezone.utc), "last_test_ok": False, "last_test_error": "refused",
    })
    assert [v["version"] for v in _history(client, connection_id)["versions"]] == [1]


def test_moving_a_connection_keeps_a_version(client, store):
    connection_id = _create(client)
    store.update(connection_id, {"project_id": "p1"}, actor=EDITOR)
    newest = _history(client, connection_id)["versions"][0]
    assert newest["note"] == "Project changed"
    assert newest["settings"]["project_id"] == "p1"


# ── Secrets ─────────────────────────────────────────────────────────────


def test_the_history_never_holds_a_password(client, db):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(password=NEW_PASSWORD))
    rows = db.fetchall("SELECT data FROM connection_versions WHERE connection_id = ?", (connection_id,))
    assert len(rows) == 2
    for row in rows:
        assert PASSWORD not in row["data"] and NEW_PASSWORD not in row["data"]
        assert "ENC:v1:" not in row["data"]
        assert json.loads(row["data"])["config"]["password"].startswith("FP:v1:")
    for path in (Path(db.db_path), Path(db.db_path + "-wal")):
        if path.exists():
            data = path.read_bytes()
            assert PASSWORD.encode() not in data and NEW_PASSWORD.encode() not in data, path.name


def test_the_api_shows_a_secret_as_a_tag_that_tells_same_from_different(client):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=SECRET_PLACEHOLDER))
    _edit(client, connection_id, config=_config(host="db2.example.test", password=NEW_PASSWORD))
    response = client.get(f"{URL}{connection_id}/versions")
    _no_secret_in(response.text, PASSWORD, NEW_PASSWORD)
    body = response.json()
    v3, v2, v1 = (v["settings"]["config"]["password"] for v in body["versions"])
    for tag in (v1, v2, v3):
        assert tag["secret"] is True and len(tag["tag"]) == 12
    assert v1 == v2          # the same password
    assert v2 != v3          # changed
    assert body["current"]["config"]["password"] == v3


def test_a_nested_secret_is_a_fingerprint_too(client, db):
    connection_id = _create(client, config={"host": "db1.example.test", "auth": {"password": PASSWORD}})
    raw = db.fetchone("SELECT data FROM connection_versions WHERE connection_id = ?", (connection_id,))["data"]
    assert PASSWORD not in raw
    tag = _history(client, connection_id)["versions"][0]["settings"]["config"]["auth"]["password"]
    assert tag["secret"] is True


def test_fingerprints_are_keyed_by_the_install():
    one, other = fresh_encryptor_for_tests(), fresh_encryptor_for_tests()
    assert one.fingerprint("same") == one.fingerprint("same")
    assert one.fingerprint("same") != one.fingerprint("different")
    assert one.fingerprint("same") != other.fingerprint("same")


# ── Restore ─────────────────────────────────────────────────────────────


def test_restore_brings_back_the_settings_and_keeps_todays_password(client, store):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=NEW_PASSWORD), description="moved")
    response = _restore(client, connection_id, 1)
    assert response.status_code == 200, response.text
    _no_secret_in(response.text, PASSWORD, NEW_PASSWORD)
    result = response.json()
    assert result["version"] == 3 and result["restored_from"] == 1 and result["unchanged"] is False
    assert result["kept_secrets"] == ["password"]
    assert result["missing_secrets"] == []
    connection = store.get(connection_id)
    assert connection.config["host"] == "db1.example.test"
    assert connection.description == ""
    assert connection.config["password"] == NEW_PASSWORD  # today's, not version 1's
    versions = _history(client, connection_id)["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]  # nothing removed
    assert versions[0]["kind"] == "restored"
    assert versions[0]["note"] == "Restored version 1"
    assert versions[0]["created_by"] == EDITOR


def test_restore_uses_the_old_password_when_it_is_typed(client, store, db):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=NEW_PASSWORD))
    response = _restore(client, connection_id, 1, secrets={"password": PASSWORD})
    assert response.status_code == 200, response.text
    _no_secret_in(response.text, PASSWORD, NEW_PASSWORD)
    assert response.json()["kept_secrets"] == []
    assert store.get(connection_id).config["password"] == PASSWORD
    versions = _history(client, connection_id)["versions"]
    assert versions[0]["settings"]["config"]["password"] == versions[2]["settings"]["config"]["password"]
    assert versions[0]["same_as_current"] and versions[2]["same_as_current"]
    raw = db.fetchone("SELECT data FROM connections WHERE id = ?", (connection_id,))["data"]
    assert PASSWORD not in raw  # stored encrypted, as any save


def test_a_password_the_version_shares_with_today_is_simply_kept(client, store):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=SECRET_PLACEHOLDER))
    result = _restore(client, connection_id, 1).json()
    assert result["kept_secrets"] == [] and result["missing_secrets"] == []
    assert store.get(connection_id).config["password"] == PASSWORD


def test_a_secret_the_connection_no_longer_has_is_reported_missing(client, store):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(password=None))  # password removed
    result = _restore(client, connection_id, 1).json()
    assert result["missing_secrets"] == ["password"]
    assert "password" not in store.get(connection_id).config


def test_typed_values_only_fill_secrets(client, store):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=SECRET_PLACEHOLDER))
    _restore(client, connection_id, 1, secrets={"host": "elsewhere.example.test"})
    assert store.get(connection_id).config["host"] == "db1.example.test"


def test_a_nested_secret_can_be_typed_by_its_dotted_name(client, store):
    connection_id = _create(client, config={"host": "db1.example.test", "auth": {"password": PASSWORD}})
    _edit(client, connection_id, config={"host": "db1.example.test", "auth": {"password": NEW_PASSWORD}})
    result = _restore(client, connection_id, 1, secrets={"auth.password": PASSWORD}).json()
    assert result["kept_secrets"] == []
    assert store.get(connection_id).config["auth"]["password"] == PASSWORD


def test_restoring_what_is_already_there_changes_nothing(client):
    connection_id = _create(client)
    _edit(client, connection_id, config=_config(host="db2.example.test", password=SECRET_PLACEHOLDER))
    result = _restore(client, connection_id, 2).json()
    assert result["unchanged"] is True and result["version"] is None
    assert [v["version"] for v in _history(client, connection_id)["versions"]] == [2, 1]


def test_a_restored_name_that_another_connection_now_has_gets_a_number(client, store):
    connection_id = _create(client, name="Orders DB")
    _edit(client, connection_id, name="Orders DB (old)")
    _create(client, name="Orders DB")
    result = _restore(client, connection_id, 1).json()
    assert result["renamed_to"] == "Orders DB (2)"
    assert store.get(connection_id).name == "Orders DB (2)"


def test_a_linked_credential_that_is_gone_keeps_todays(client, store, monkeypatch):
    credentials = {
        "cred_old": SimpleNamespace(workspace_id="default", config={}),
        "cred_new": SimpleNamespace(workspace_id="default", config={}),
    }
    monkeypatch.setitem(main_module.app_state, "credential_store", SimpleNamespace(
        get_raw=lambda credential_id, **_: credentials.get(credential_id),
    ))
    connection_id = _create(client, credential_id="cred_old")
    _edit(client, connection_id, credential_id="cred_new",
          config=_config(host="db2.example.test", password=SECRET_PLACEHOLDER))
    del credentials["cred_old"]
    result = _restore(client, connection_id, 1).json()
    assert result["kept_settings"] == ["linked credential"]
    connection = store.get(connection_id)
    assert connection.credential_id == "cred_new"
    assert connection.config["host"] == "db1.example.test"


def test_unknown_versions_and_connections_are_not_found(client):
    connection_id = _create(client)
    assert _restore(client, connection_id, 9).status_code == 404
    assert client.get(f"{URL}nope/versions").status_code == 404
    assert _restore(client, "nope", 1).status_code == 404


def test_another_workspace_cannot_read_or_restore_the_history(client, store):
    connection_id = _create(client)
    client.app.dependency_overrides[connections_api._safe_workspace_id] = lambda: "other"
    assert client.get(f"{URL}{connection_id}/versions").status_code == 404
    assert _restore(client, connection_id, 1).status_code == 404


def test_restore_config_rules():
    target = {"host": "a", "password": "FP:v1:old", "token": "FP:v1:tok", "nested": {"secret": "FP:v1:n"}}
    current = {"host": "b", "password": "today", "token": "same-token", "nested": {}}
    current_snap = {"host": "b", "password": "FP:v1:today", "token": "FP:v1:tok", "nested": {}}
    config, kept, missing = restore_config(target, current, current_snap, {})
    assert config == {"host": "a", "password": "today", "token": "same-token", "nested": {}}
    assert kept == ["password"] and missing == ["nested.secret"]


# ── Keeping, deleting, the history around it ────────────────────────────


def test_the_newest_versions_and_the_first_are_kept(client, store, monkeypatch):
    monkeypatch.setattr(runtime_config, "CONNECTION_VERSION_RETENTION_COUNT", 3)
    connection_id = _create(client)
    for n in range(2, 7):
        store.update(connection_id, {"description": f"edit {n}"})
    assert [v["version"] for v in _history(client, connection_id)["versions"]] == [6, 5, 4, 1]
    assert _history(client, connection_id)["keep"] == 3


def test_a_connection_from_before_the_history_keeps_how_it_was_on_its_first_change(client, store, db):
    data = Connection(id="legacy1", name="Legacy", type="postgresql",
                      config={"host": "db1.example.test", "password": PASSWORD}).model_dump(mode="json")
    db.insert_json("connections", "legacy1", data, name="Legacy", type="postgresql", project_id=None,
                   workspace_id="default", created_at=data["created_at"], updated_at=data["updated_at"])
    store.update("legacy1", {"last_test_ok": True})  # a test result is not a change
    assert _history(client, "legacy1")["versions"] == []
    store.update("legacy1", {"config": {"host": "db2.example.test", "password": SECRET_PLACEHOLDER}}, actor=EDITOR)
    versions = _history(client, "legacy1")["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["note"] == "As it was when version history began"
    assert versions[1]["created_by"] == ""  # not known
    # Dated when the connection was last saved, before the change.
    assert datetime.fromisoformat(versions[1]["created_at"]) <= datetime.fromisoformat(versions[0]["created_at"])
    assert versions[0]["note"] == "Host changed" and versions[0]["created_by"] == EDITOR


def test_deleting_a_connection_deletes_its_history(client, store, db):
    connection_id = _create(client)
    _edit(client, connection_id, description="changed")
    assert store.delete(connection_id)
    count = db.fetchone("SELECT COUNT(*) AS n FROM connection_versions WHERE connection_id = ?", (connection_id,))
    assert count["n"] == 0


def test_the_history_lists_the_pipelines_that_use_the_connection(client, monkeypatch):
    connection_id = _create(client)
    monkeypatch.setattr(usage, "compute_connection_usage_cached", lambda workspace_id: {
        connection_id: [{"workflow_id": "wf1", "name": "Nightly orders", "role": "source"}],
    })
    monkeypatch.setitem(main_module.app_state, "workflow_store", SimpleNamespace(
        list_all=lambda workspace_id=None: [{"id": "wf1", "name": "Nightly orders", "status": "published", "deployed_version": 4}],
    ))
    assert _history(client, connection_id)["used_by"] == [{
        "workflow_id": "wf1", "name": "Nightly orders", "role": "source",
        "status": "published", "deployed_version": 4,
    }]


def test_a_backup_export_carries_the_history_without_passwords(client, db):
    _create(client)
    exported = db.export_all()["_tables"]
    assert len(exported["connection_versions"]) == 1
    assert PASSWORD not in json.dumps(exported["connection_versions"])


def test_an_upgraded_database_gets_the_history_table(tmp_path):
    path = str(tmp_path / "upgraded.db")
    first = Database(path)
    first.execute_write("DROP TABLE connection_versions")
    first.execute_write("UPDATE _meta SET value = '34' WHERE key = 'schema_version'")
    first.close()
    second = Database(path)
    try:
        assert second.fetchone("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'connection_versions'")
        assert second.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")["value"] == str(SCHEMA_VERSION)
    finally:
        second.close()
