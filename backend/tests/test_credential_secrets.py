"""Every secret in a saved credential is encrypted at rest and never returned.

Until November 2026 the Credentials store encrypted only the encryptor's fixed
list of key names, and masked an even shorter one by showing the first two
characters. A field the person named themselves (``db_password``,
``api_token``) was kept in clear and returned as typed, and the form's
"sensitive" switch never reached the server. Now a field is secret by its name
or because the person marked it. It is encrypted with the master key,
decrypted only by ``get_raw`` for drivers, and shown everywhere else as a
placeholder with no character of the value.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import revoluzen_flow.main as main_module
from revoluzen_flow.api import credentials as credentials_api
from revoluzen_flow.auth.deps import require_auth
from revoluzen_flow.connections.inline_secrets import SECRET_FIELDS, SECRET_PLACEHOLDER
from revoluzen_flow.connections.models import Connection
from revoluzen_flow.connections.store import ConnectionStore
from revoluzen_flow.credentials.models import Credential
from revoluzen_flow.credentials.sensitive_fields import name_says_secret
from revoluzen_flow.credentials.store import CredentialStore
from revoluzen_flow.nodes.db_source import _get_connection_config
from revoluzen_flow.security.encryptor import fresh_encryptor_for_tests
from revoluzen_flow.security.migrate_existing import (
    _encrypt_credential_rows,
    _migrate_credentials,
    encrypt_credential_secrets,
)
from revoluzen_flow.storage.database import Database

DB_PASSWORD = "synthetic-db-password-Kq"
API_TOKEN = "synthetic-api-token-Zx"
PIN = "synthetic-pin-4417"
SECRETS = (("db_password", DB_PASSWORD), ("api_token", API_TOKEN), ("pin", PIN))
URL = "/api/credentials/"
HELPER = Path(__file__).parents[2] / "frontend" / "src" / "utils" / "storedSecrets.ts"
ADMIN = SimpleNamespace(id="u1", email="admin@example.test", name="Admin", role="admin", is_active=True)


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
    credentials = CredentialStore(db=db)
    connections = ConnectionStore(db=db)
    monkeypatch.setattr(credentials, "_get_encryptor", lambda: encryptor)
    monkeypatch.setattr(connections, "_get_encryptor", lambda: encryptor)
    state = {"db": db, "encryptor": encryptor, "credential_store": credentials, "connection_store": connections}
    for key, value in state.items():
        monkeypatch.setitem(main_module.app_state, key, value)
    return credentials


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(credentials_api.router)
    app.dependency_overrides[require_auth] = lambda: ADMIN
    app.dependency_overrides[credentials_api._write.dependency] = lambda: ADMIN
    app.dependency_overrides[credentials_api._safe_workspace_id] = lambda: "default"
    with TestClient(app) as test_client:
        yield test_client


def _create(client, **body):
    payload = {
        "name": "Warehouse login",
        "type": "database",
        "config": {"host": "db.example.test", "username": "reader",
                   "db_password": DB_PASSWORD, "api_token": API_TOKEN, "pin": PIN},
        "sensitive_fields": ["pin"],
        **body,
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    return response


def _raw_text(db, credential_id) -> str:
    return db.fetchone("SELECT data FROM credentials WHERE id = ?", (credential_id,))["data"]


def _raw(db, credential_id) -> dict:
    return json.loads(_raw_text(db, credential_id))


def _legacy_row(db, credential_id, config, sensitive_fields=None):
    """A credential as the old store saved it."""
    data = Credential(id=credential_id, name=f"Legacy {credential_id}", type="database", config=config).model_dump(mode="json")
    if sensitive_fields is None:
        data.pop("sensitive_fields", None)  # rows from before the field existed
    else:
        data["sensitive_fields"] = sensitive_fields
    db.insert_json(
        "credentials", credential_id, data,
        name=data["name"], type="database", project_id="", workspace_id="default",
        created_at=data["created_at"], updated_at=data["updated_at"],
    )


# ── At rest ─────────────────────────────────────────────────────────────


def test_named_and_marked_secrets_are_encrypted_on_disk(client, db, encryptor):
    credential_id = _create(client).json()["id"]
    stored = _raw(db, credential_id)["config"]
    for field, value in SECRETS:
        assert stored[field].startswith("ENC:v1:"), field
        assert encryptor.decrypt_value(stored[field]) == value
    assert (stored["host"], stored["username"]) == ("db.example.test", "reader")
    for path in (Path(db.db_path), Path(db.db_path + "-wal")):
        if path.exists():
            content = path.read_bytes()
            for _field, value in SECRETS:
                assert value.encode() not in content, (path.name, value)


def test_marked_fields_are_kept_in_the_forms_spelling(client, db):
    credential_id = _create(client, sensitive_fields=["PIN", " pin ", "not_in_config"]).json()["id"]
    assert _raw(db, credential_id)["sensitive_fields"] == ["pin"]


def test_drivers_get_the_real_values(client, store):
    credential_id = _create(client).json()["id"]
    raw = store.get_raw(credential_id).config
    assert [raw[field] for field, _ in SECRETS] == [value for _, value in SECRETS]


def test_a_linked_connection_gets_the_real_password(client):
    credential_id = _create(client).json()["id"]
    main_module.app_state["connection_store"].create(Connection(
        id="c_wh", name="Warehouse", type="postgresql", credential_id=credential_id,
        config={"host": "db.example.test", "database": "dw", "credential_field_map": {"password": "db_password"}},
    ))
    config, _ = _get_connection_config("c_wh")
    assert config["password"] == DB_PASSWORD


# ── In API responses ────────────────────────────────────────────────────


def test_no_response_carries_a_secret_or_any_of_its_characters(client):
    created = _create(client)
    credential_id = created.json()["id"]
    responses = {
        "create": created,
        "get": client.get(f"{URL}{credential_id}"),
        "list": client.get(URL),
        "update": client.put(f"{URL}{credential_id}", json={"name": "Warehouse login (renamed)"}),
    }
    for step, response in responses.items():
        assert response.status_code == 200, (step, response.text)
        for _field, value in SECRETS:
            assert value not in response.text, (step, value)
            assert value[:2] + "***" not in response.text, (step, value)
        assert "ENC:v1:" not in response.text, step
    shown = responses["get"].json()
    assert all(shown["config"][field] == SECRET_PLACEHOLDER for field, _ in SECRETS)
    assert shown["config"]["host"] == "db.example.test"
    assert shown["sensitive_fields"] == ["api_token", "db_password", "pin"]
    listed = responses["list"].json()[0]
    assert listed["sensitive_fields"] == ["api_token", "db_password", "pin"]
    assert listed["username"] == "reader"


def test_reading_one_credential_needs_a_signed_in_user():
    route = next(
        r for r in credentials_api.router.routes
        if getattr(r, "path", "") == "/api/credentials/{credential_id}" and "GET" in getattr(r, "methods", set())
    )
    assert any(dependency.call is require_auth for dependency in route.dependant.dependencies)


# ── Editing ─────────────────────────────────────────────────────────────


def test_saving_the_edit_form_unchanged_keeps_every_secret(client, store):
    credential_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{credential_id}").json()
    response = client.put(f"{URL}{credential_id}", json={
        "name": "Warehouse login",
        "config": {**shown["config"], "host": "db2.example.test"},
        "sensitive_fields": shown["sensitive_fields"],
    })
    assert response.status_code == 200, response.text
    raw = store.get_raw(credential_id).config
    assert [raw[field] for field, _ in SECRETS] == [value for _, value in SECRETS]
    assert raw["host"] == "db2.example.test"


def test_a_new_value_replaces_the_stored_one(client, store):
    credential_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{credential_id}").json()
    client.put(f"{URL}{credential_id}", json={"config": {**shown["config"], "pin": "synthetic-new-pin"}})
    assert store.get_raw(credential_id).config["pin"] == "synthetic-new-pin"


def test_an_update_without_marks_keeps_them(client, db):
    credential_id = _create(client).json()["id"]
    client.put(f"{URL}{credential_id}", json={"name": "Renamed"})
    stored = _raw(db, credential_id)
    assert stored["sensitive_fields"] == ["pin"]
    assert stored["config"]["pin"].startswith("ENC:v1:")


def test_a_kept_secret_stays_secret_whatever_the_marks_say(client, db):
    credential_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{credential_id}").json()
    client.put(f"{URL}{credential_id}", json={"config": shown["config"], "sensitive_fields": []})
    stored = _raw(db, credential_id)
    assert stored["config"]["pin"].startswith("ENC:v1:")
    assert "pin" in stored["sensitive_fields"]


def test_a_field_unmarked_with_a_new_value_is_stored_as_typed(client, db):
    credential_id = _create(client).json()["id"]
    shown = client.get(f"{URL}{credential_id}").json()
    client.put(f"{URL}{credential_id}", json={"config": {**shown["config"], "pin": "4417"}, "sensitive_fields": []})
    stored = _raw(db, credential_id)
    assert stored["config"]["pin"] == "4417"
    assert stored["config"]["db_password"].startswith("ENC:v1:")  # secret by name, marks or not


def test_a_page_opened_before_the_upgrade_still_saves_without_losing_secrets(store):
    """It showed a two-character mask, or the stored ciphertext for fields the
    old mask did not cover."""
    created = store.create(Credential(name="Old page", type="database", config={
        "password": DB_PASSWORD, "client_secret": "synthetic-client-secret",
    }))
    old_page = {"password": DB_PASSWORD[:2] + "***", "client_secret": store.get(created.id).config["client_secret"]}
    store.update(created.id, {"config": old_page})
    raw = store.get_raw(created.id).config
    assert raw == {"password": DB_PASSWORD, "client_secret": "synthetic-client-secret"}


def test_a_placeholder_with_nothing_stored_is_dropped(store):
    created = store.create(Credential(name="New", type="api", config={"host": "h", "token": SECRET_PLACEHOLDER}))
    assert "token" not in store.get_raw(created.id).config
    store.update(created.id, {"config": {"host": "h", "api_token": SECRET_PLACEHOLDER}})
    assert "api_token" not in store.get_raw(created.id).config


# ── Rows saved before this change ───────────────────────────────────────


def test_the_startup_pass_encrypts_what_the_old_rules_left_in_clear_and_can_run_twice(db, store, encryptor):
    # The old store encrypted `password` but kept these two as typed.
    _legacy_row(db, "old1", {
        "host": "db.example.test", "password": encryptor.encrypt_value(DB_PASSWORD),
        "db_password": DB_PASSWORD, "api_token": API_TOKEN,
    })
    _legacy_row(db, "nothing_secret", {"host": "db.example.test", "username": "reader"})
    assert API_TOKEN in _raw_text(db, "old1")

    assert encrypt_credential_secrets(db, encryptor) == (2, 1, 0)
    first = _raw_text(db, "old1")
    assert DB_PASSWORD not in first and API_TOKEN not in first
    raw = store.get_raw("old1").config
    assert (raw["password"], raw["db_password"], raw["api_token"]) == (DB_PASSWORD, DB_PASSWORD, API_TOKEN)

    # The next server start reads every row and writes nothing.
    assert encrypt_credential_secrets(db, encryptor) == (2, 0, 0)
    assert _raw_text(db, "old1") == first


def test_the_startup_pass_honours_marked_fields(db, encryptor):
    _legacy_row(db, "marked", {"pin": PIN}, sensitive_fields=["pin"])
    assert encrypt_credential_secrets(db, encryptor) == (1, 1, 0)
    assert _raw(db, "marked")["config"]["pin"].startswith("ENC:v1:")


def test_a_dry_run_writes_nothing_and_a_broken_row_is_skipped(db, encryptor):
    _legacy_row(db, "good", {"api_token": API_TOKEN})
    db.execute_write(
        "INSERT INTO credentials (id, name, type, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("broken", "Broken", "database", "{not json", "2026-11-10", "2026-11-10"),
    )
    before = _raw_text(db, "good")
    assert _encrypt_credential_rows(db, encryptor, dry_run=True, backup_writer=lambda _payload: None) == (2, 1, 1)
    assert _raw_text(db, "good") == before
    assert encrypt_credential_secrets(db, encryptor) == (2, 1, 1)
    assert API_TOKEN not in _raw_text(db, "good")
    assert _raw_text(db, "broken") == "{not json"


def test_without_an_encryptor_the_pass_does_nothing(db):
    _legacy_row(db, "old", {"api_token": API_TOKEN})
    assert encrypt_credential_secrets(db, None) == (0, 0, 0)


def test_the_operator_script_covers_every_secret_field(db, encryptor):
    _legacy_row(db, "old", {"api_token": API_TOKEN, "host": "h"})
    con = sqlite3.connect(db.db_path)
    con.row_factory = sqlite3.Row
    try:
        assert _migrate_credentials(con, encryptor, dry_run=False, backup_writer=lambda _payload: None)[1] == 1
        con.commit()
    finally:
        con.close()
    stored = _raw(db, "old")["config"]
    assert stored["api_token"].startswith("ENC:v1:") and stored["host"] == "h"


# ── Which names hold a secret ───────────────────────────────────────────


SECRET_NAMES = [
    "password", "db_password", "smtp_password", "passphrase", "client_secret", "app_secret",
    "api_key", "apikey", "api_token", "bearer_token", "github_token", "access_token",
    "refresh_token", "private_key", "private_key_pem", "account_key", "secret_key", "key",
    "sas_token", "connection_string", "credentials_json", "service_account_json",
    "webhook_url", "custom_header_value", "DB Password", "API Token",
]
PLAIN_NAMES = [
    "username", "user", "host", "port", "database", "region", "provider", "base_url",
    "client_id", "access_key_id", "api_key_id", "hmac_key_id", "key_id", "token_url",
    "token_type", "secret_name", "api_key_header", "endpoint", "account", "tenant_id", "Client ID",
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_names_that_hold_a_secret(name):
    assert name_says_secret(name)


@pytest.mark.parametrize("name", PLAIN_NAMES)
def test_names_that_do_not(name):
    assert not name_says_secret(name)


@pytest.mark.skipif(not HELPER.exists(), reason=f"frontend helper not found at {HELPER}")
def test_the_form_locks_the_same_names_the_server_protects():
    """The form locks its switch on these names; if the two rules drift
    apart the server still protects its names, but the form would mislead."""
    text = HELPER.read_text(encoding="utf-8")
    always = set(re.findall(r"'([a-z_]+)'", re.search(r"const ALWAYS_SECRET = new Set\(\[(.*?)\]\);", text).group(1)))
    words = re.compile(re.search(r"const SECRET_WORDS = /(.+?)/;", text).group(1))
    ending = re.compile(re.search(r"const NOT_SECRET_ENDING = /(.+?)/;", text).group(1))

    def form_rule(name: str) -> bool:
        key = name.strip().lower().replace(" ", "_")
        if not key:
            return False
        if key in always:
            return True
        if ending.search(key):
            return False
        return key == "key" or key.endswith("_key") or bool(words.search(key))

    names = sorted(set(SECRET_FIELDS) | set(SECRET_NAMES) | set(PLAIN_NAMES))
    assert [name for name in names if form_rule(name) != name_says_secret(name)] == []
