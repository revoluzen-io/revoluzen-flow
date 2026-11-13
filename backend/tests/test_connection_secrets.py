"""Passwords and keys typed into a connection's own settings stay secret.

Until November 2026 a connection that did not link a saved credential kept its
password in clear in the database, and every Connections API response sent it
back to the browser. Now the value is encrypted at rest with the encryptor
Credentials use, only the server decrypts it (for drivers), and the API shows a
placeholder. Saving the edit form with the placeholder keeps the stored value.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import revoluzen_flow.main as main_module
from revoluzen_flow.api import connections as connections_api
from revoluzen_flow.auth.deps import require_auth
from revoluzen_flow.connections.credentials import resolve_credentials
from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER, is_secret_field
from revoluzen_flow.connections.models import Connection, ConnectionReport
from revoluzen_flow.connections.store import ConnectionStore
from revoluzen_flow.nodes.db_source import _get_connection_config
from revoluzen_flow.security.encryptor import fresh_encryptor_for_tests
from revoluzen_flow.security.migrate_existing import _migrate_connections, encrypt_connection_secrets
from revoluzen_flow.storage.database import Database

PASSWORD = "synthetic-db-password-7Q"
URL = "/api/connections/"
FORM = Path(__file__).parents[2] / "frontend" / "src" / "components" / "pages" / "ConnectionsPage.tsx"
HELPER = Path(__file__).parents[2] / "frontend" / "src" / "utils" / "storedSecrets.ts"


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
    return connection_store


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(connections_api.router)
    app.dependency_overrides[require_auth] = lambda: SimpleNamespace(
        id="u1", email="tester@example.test", role="admin", is_active=True,
    )
    app.dependency_overrides[connections_api._safe_workspace_id] = lambda: "default"
    with TestClient(app) as test_client:
        yield test_client


def _create(client, **config):
    body = {
        "name": "Orders DB",
        "type": "postgresql",
        "config": {"host": "db.example.test", "database": "orders", "username": "reader",
                   "password": PASSWORD, **config},
    }
    response = client.post(URL, json=body)
    assert response.status_code == 200, response.text
    return response


def _raw(db, connection_id) -> str:
    return db.fetchone("SELECT data FROM connections WHERE id = ?", (connection_id,))["data"]


def _legacy_row(db, connection_id, conn_type="postgresql", **config):
    """A connection as it was saved before this change: secrets as typed."""
    data = Connection(id=connection_id, name=f"Legacy {connection_id}", type=conn_type, config=config).model_dump(mode="json")
    db.insert_json(
        "connections", connection_id, data,
        name=data["name"], type=conn_type, project_id=None, workspace_id="default",
        created_at=data["created_at"], updated_at=data["updated_at"],
    )


# ── At rest ─────────────────────────────────────────────────────────────


def test_a_typed_password_is_encrypted_on_disk(client, db, encryptor):
    connection_id = _create(client).json()["id"]
    raw = _raw(db, connection_id)
    assert PASSWORD not in raw
    stored = json.loads(raw)["config"]
    assert stored["password"].startswith("ENC:v1:")
    assert encryptor.decrypt_value(stored["password"]) == PASSWORD
    assert stored["host"] == "db.example.test"  # the rest stays readable in DB tooling
    for path in (Path(db.db_path), Path(db.db_path + "-wal")):
        if path.exists():
            assert PASSWORD.encode() not in path.read_bytes(), path.name


def test_saving_again_keeps_one_readable_layer(client, store, db, encryptor):
    connection_id = _create(client).json()["id"]
    for name in ("Orders DB (1)", "Orders DB (2)", "Orders DB (3)"):
        store.update(connection_id, {"name": name})
    token = json.loads(_raw(db, connection_id))["config"]["password"]
    assert encryptor._decrypt_token(token) == PASSWORD


# ── In API responses ────────────────────────────────────────────────────


def test_no_response_carries_the_password_or_its_ciphertext(client):
    created = _create(client)
    connection_id = created.json()["id"]
    responses = {
        "create": created,
        "get": client.get(f"{URL}{connection_id}"),
        "list": client.get(URL),
        "update": client.put(f"{URL}{connection_id}", json={"name": "Orders DB (renamed)"}),
    }
    for step, response in responses.items():
        assert response.status_code == 200, (step, response.text)
        assert PASSWORD not in response.text, step
        assert "ENC:v1:" not in response.text, step
    assert responses["get"].json()["config"]["password"] == SECRET_PLACEHOLDER
    assert responses["list"].json()[0]["config"]["password"] == SECRET_PLACEHOLDER
    assert responses["get"].json()["config"]["host"] == "db.example.test"


def test_an_empty_secret_stays_empty_so_the_form_can_tell_it_is_not_set(client):
    created = _create(client, password="").json()
    assert created["config"]["password"] == ""


def test_a_credential_field_map_is_shown_as_it_is(client):
    created = _create(client, credential_field_map={"password": "db_password"}).json()
    assert created["config"]["credential_field_map"] == {"password": "db_password"}


def test_a_uri_is_masked_only_where_it_carries_the_password(client):
    mongo = client.post(URL, json={"name": "Mongo", "type": "mongodb", "config": {
        "uri": f"mongodb://reader:{PASSWORD}@db.example.test/app"}}).json()
    graph = client.post(URL, json={"name": "Graph", "type": "neo4j", "config": {
        "uri": "bolt://graph.example.test:7687", "password": PASSWORD}}).json()
    assert mongo["config"]["uri"] == SECRET_PLACEHOLDER
    assert graph["config"]["uri"] == "bolt://graph.example.test:7687"
    assert graph["config"]["password"] == SECRET_PLACEHOLDER


# ── Editing ─────────────────────────────────────────────────────────────


def test_saving_the_edit_form_unchanged_keeps_the_password(client, store):
    connection_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{connection_id}").json()["config"]
    response = client.put(f"{URL}{connection_id}", json={
        "name": "Orders DB", "config": {**shown, "host": "db2.example.test"},
    })
    assert response.status_code == 200, response.text
    assert PASSWORD not in response.text
    saved = store.get(connection_id).config
    assert saved["password"] == PASSWORD
    assert saved["host"] == "db2.example.test"


def test_a_new_password_replaces_the_stored_one(client, store):
    connection_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{connection_id}").json()["config"]
    client.put(f"{URL}{connection_id}", json={"config": {**shown, "password": "synthetic-new-password"}})
    assert store.get(connection_id).config["password"] == "synthetic-new-password"


def test_an_emptied_password_is_cleared(client, store):
    connection_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{connection_id}").json()["config"]
    client.put(f"{URL}{connection_id}", json={"config": {**shown, "password": ""}})
    assert store.get(connection_id).config["password"] == ""


def test_a_placeholder_with_nothing_stored_behind_it_is_dropped(client, store):
    connection_id = _create(client, password="").json()["id"]
    client.put(f"{URL}{connection_id}", json={"config": {"host": "db.example.test", "token": SECRET_PLACEHOLDER}})
    assert "token" not in store.get(connection_id).config


def test_a_placeholder_on_a_new_connection_is_not_saved_as_the_password(client, store):
    connection_id = _create(client, password=SECRET_PLACEHOLDER).json()["id"]
    assert "password" not in store.get(connection_id).config


# ── Server-side use: tests, runs, reports ───────────────────────────────


def test_the_connection_test_gets_the_real_password(client, store, db, encryptor, monkeypatch):
    connection_id = _create(client).json()["id"]
    seen = {}

    def fake_test(conn_type, config):
        seen.update(config)
        return {"success": True, "message": "reachable"}

    monkeypatch.setattr(connections_api._tester, "test_connection", fake_test)
    response = client.post(f"{URL}{connection_id}/test")
    assert response.status_code == 200, response.text
    assert seen["password"] == PASSWORD
    assert PASSWORD not in response.text
    # Recording the result saved the row again: still one layer, still readable.
    assert encryptor._decrypt_token(json.loads(_raw(db, connection_id))["config"]["password"]) == PASSWORD


def test_an_unsaved_test_never_sends_the_placeholder(client, monkeypatch):
    seen = {}

    def fake_test(conn_type, config):
        seen.update(config)
        return {"success": False, "message": "no password"}

    monkeypatch.setattr(connections_api._tester, "test_connection", fake_test)
    response = client.post(f"{URL}test-inline", json={
        "type": "postgresql", "config": {"host": "db.example.test", "password": SECRET_PLACEHOLDER},
    })
    assert response.status_code == 200, response.text
    assert seen == {"host": "db.example.test"}


def test_the_run_paths_get_the_real_password(client, store):
    connection_id = _create(client).json()["id"]
    node_config, conn_type = _get_connection_config(connection_id)  # sources, sinks, the pool's factories
    assert (node_config["password"], conn_type) == (PASSWORD, "postgresql")
    assert resolve_credentials(store.get(connection_id))["password"] == PASSWORD  # catalog, columns, Copilot test


def test_report_execution_gets_the_real_password(client, store, monkeypatch):
    connection_id = _create(client).json()["id"]
    report = store.create_report(ConnectionReport(
        connection_id=connection_id, name="Everything", query_template="SELECT 1 AS one",
    ))
    seen = {}

    def fake_driver(db_type, config, query):
        seen.update(config)
        return ["one"], [{"one": 1}], 1

    monkeypatch.setattr(store, "_try_native_driver", fake_driver)
    result = store.run_report(connection_id, report.id, {})
    assert result.status == "success", result.error
    assert seen["password"] == PASSWORD


# ── Rows saved before this change ───────────────────────────────────────


def test_the_startup_pass_encrypts_old_rows_and_can_run_twice(db, store, encryptor):
    _legacy_row(db, "legacy1", host="db.example.test", password=PASSWORD, token="synthetic-token")
    _legacy_row(db, "nothing_secret", host="db.example.test")
    assert PASSWORD in _raw(db, "legacy1")

    scanned, updated, failed = encrypt_connection_secrets(db, encryptor)
    assert (scanned, updated, failed) == (2, 1, 0)
    first = _raw(db, "legacy1")
    assert PASSWORD not in first and "synthetic-token" not in first
    assert json.loads(first)["config"]["host"] == "db.example.test"
    assert store.get("legacy1").config["password"] == PASSWORD
    assert store.get("legacy1").config["token"] == "synthetic-token"

    # The next server start reads every row and writes nothing.
    assert encrypt_connection_secrets(db, encryptor) == (2, 0, 0)
    assert _raw(db, "legacy1") == first


def test_rows_saved_by_the_store_need_no_pass(client, db, encryptor):
    connection_id = _create(client).json()["id"]
    before = _raw(db, connection_id)
    assert encrypt_connection_secrets(db, encryptor) == (1, 0, 0)
    assert _raw(db, connection_id) == before


def test_a_dry_run_writes_nothing(db, encryptor):
    _legacy_row(db, "legacy1", password=PASSWORD)
    before = _raw(db, "legacy1")
    assert _migrate_connections(db, encryptor, dry_run=True, backup_writer=lambda _payload: None) == (1, 1, 0)
    assert _raw(db, "legacy1") == before


def test_a_broken_row_is_skipped_and_the_rest_are_encrypted(db, encryptor):
    _legacy_row(db, "good", password=PASSWORD)
    db.execute_write(
        "INSERT INTO connections (id, name, type, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("broken", "Broken", "postgresql", "{not json", "2026-11-10", "2026-11-10"),
    )
    assert encrypt_connection_secrets(db, encryptor) == (2, 1, 1)
    assert PASSWORD not in _raw(db, "good")
    assert _raw(db, "broken") == "{not json"


def test_a_value_this_install_cannot_decrypt_is_kept_not_lost(db, store, encryptor):
    foreign = fresh_encryptor_for_tests().encrypt_value(PASSWORD)
    _legacy_row(db, "moved", host="db.example.test", password=foreign)
    assert store.get("moved").config["password"] == foreign  # no error; kept as stored
    assert store.list_all()[0]["config"]["password"] == SECRET_PLACEHOLDER
    assert encrypt_connection_secrets(db, encryptor) == (1, 0, 0)
    assert json.loads(_raw(db, "moved"))["config"]["password"] == foreign


def test_a_stored_secret_is_read_and_masked_whatever_its_name(db, store, encryptor):
    """Encrypted while its name counted as secret (before a type change, say):
    drivers still get the value and the API still hides it."""
    uri = "bolt://reader:synthetic@graph.example.test:7687"
    _legacy_row(db, "retyped", conn_type="neo4j", uri=encryptor.encrypt_value(uri))
    assert store.get("retyped").config["uri"] == uri
    assert store.list_all()[0]["config"]["uri"] == SECRET_PLACEHOLDER


def test_without_an_encryptor_the_pass_does_nothing(db):
    _legacy_row(db, "legacy1", password=PASSWORD)
    assert encrypt_connection_secrets(db, None) == (0, 0, 0)


# ── The form and the server agree on what is secret ─────────────────────


def _form_fields() -> list[tuple[str, str, str]]:
    """(connection type, field key, field type) for every field on the form."""
    text = FORM.read_text(encoding="utf-8")
    start = text.index("export const CONNECTION_FIELDS")
    block = text[start:text.index("\n};", start)]
    fields, conn_type = [], None
    for line in block.splitlines():
        heading = re.match(r"^  (\w+): \[", line)
        if heading:
            conn_type = heading.group(1)
        for key, kind in re.findall(r"\{ key: '(\w+)'[^{}]*?type: '(\w+)'", line):
            fields.append((conn_type, key, kind))
    return fields


#: Text areas that hold a secret; the others (headers, extra JSON, Kafka's CA
#: and client certificates) do not.
SECRET_TEXT_AREAS = {"credentials_json", "ssl_key_pem"}

needs_form = pytest.mark.skipif(not FORM.exists(), reason=f"Connections form not found at {FORM}")


@needs_form
def test_every_password_field_on_the_form_is_kept_secret():
    fields = _form_fields()
    secret = [(t, k) for t, k, kind in fields if kind == "password" or k in SECRET_TEXT_AREAS]
    assert len(secret) > 90  # a parse that finds nothing must not pass
    assert [(t, k) for t, k in secret if not is_secret_field(k, t)] == []


@pytest.mark.skipif(not HELPER.exists(), reason=f"frontend helper not found at {HELPER}")
def test_the_form_and_the_server_use_the_same_placeholder():
    """The forms recognise a stored secret by this exact value. If the two
    drift apart they stop offering Replace and show the mask as if it were a
    typed value."""
    text = HELPER.read_text(encoding="utf-8")
    found = re.search(r"export const SECRET_PLACEHOLDER = '((?:\\u[0-9a-fA-F]{4}|[^'\\])+)'\.repeat\((\d+)\);", text)
    assert found, "SECRET_PLACEHOLDER not found in the frontend helper"
    assert json.loads(f'"{found.group(1)}"') * int(found.group(2)) == SECRET_PLACEHOLDER


@needs_form
def test_no_plain_field_on_the_form_is_masked():
    plain = [(t, k) for t, k, kind in _form_fields() if kind != "password" and k not in SECRET_TEXT_AREAS]
    assert len(plain) > 100
    assert [(t, k) for t, k in plain if is_secret_field(k, t)] == []
