"""A saved credential keeps working after it is used and after it is edited.

The store used to save the form it read: encrypted secrets went back through
encryption, so one use or one rename left a secret that decrypted to ciphertext.
And the edit form sends back the masked values it was shown, which replaced the
real secret with the mask.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from revoluzen_flow.credentials.models import Credential
from revoluzen_flow.credentials.store import CredentialStore
from revoluzen_flow.security.encryptor import Encryptor, fresh_encryptor_for_tests

PASSWORD = "synthetic-password"


class _Db:
    def __init__(self):
        self.rows = {}

    def insert_json(self, table, key, data, **_columns):
        self.rows[key] = copy.deepcopy(data)

    def get_json(self, table, key):
        return copy.deepcopy(self.rows.get(key))

    def list_json(self, table):
        return [copy.deepcopy(row) for row in self.rows.values()]


@pytest.fixture
def encryptor():
    return fresh_encryptor_for_tests()


@pytest.fixture
def store(encryptor, monkeypatch):
    store = CredentialStore(db=_Db())
    monkeypatch.setattr(store, "_get_encryptor", lambda: encryptor)
    return store


def _saved(store, **config):
    return store.create(Credential(name="Partner login", type="cloud", config={"username": "partner", "password": PASSWORD, **config}))


def _layers(store, credential_id, field="password"):
    return store._db.rows[credential_id]["config"][field].count("ENC:v1:")


def test_a_secret_is_stored_encrypted_once(store):
    credential = _saved(store)
    assert _layers(store, credential.id) == 1
    assert store.get_raw(credential.id).config["password"] == PASSWORD


def test_using_a_credential_leaves_its_secret_readable(store):
    credential = _saved(store)
    for _ in range(3):
        store.mark_used(credential.id)
    assert store.get_raw(credential.id).config["password"] == PASSWORD
    assert _layers(store, credential.id) == 1
    assert store.get_raw(credential.id).last_used is not None


def test_changing_only_the_name_leaves_the_secret_readable(store):
    credential = _saved(store)
    store.update(credential.id, {"name": "Partner login (new name)"})
    raw = store.get_raw(credential.id)
    assert raw.name == "Partner login (new name)"
    assert raw.config["password"] == PASSWORD
    assert _layers(store, credential.id) == 1


def test_saving_the_edit_form_unchanged_keeps_the_secret(store):
    """The form pre-fills what the list returned, so that is what comes back."""
    credential = _saved(store, client_secret="synthetic-client-secret")
    shown = next(row for row in store.list_all() if row["id"] == credential.id)["config"]
    assert shown["password"] != PASSWORD
    store.update(credential.id, {"config": shown, "expires_at": datetime(2027, 1, 1, tzinfo=timezone.utc)})
    raw = store.get_raw(credential.id)
    assert raw.config["password"] == PASSWORD
    assert raw.config["client_secret"] == "synthetic-client-secret"
    assert raw.config["username"] == "partner"
    assert raw.expires_at is not None


def test_a_new_password_replaces_the_old_one(store):
    credential = _saved(store)
    shown = next(row for row in store.list_all() if row["id"] == credential.id)["config"]
    store.update(credential.id, {"config": {**shown, "password": "synthetic-new-password"}})
    raw = store.get_raw(credential.id)
    assert raw.config["password"] == "synthetic-new-password"
    assert raw.config["username"] == "partner"
    assert _layers(store, credential.id) == 1


def test_a_field_removed_in_the_form_is_removed(store):
    credential = _saved(store, token="synthetic-token")
    store.update(credential.id, {"config": {"username": "partner"}})
    assert "token" not in store.get_raw(credential.id).config
    assert "password" not in store.get_raw(credential.id).config


def test_an_update_never_hands_back_the_plain_secret(store):
    credential = _saved(store)
    updated = store.update(credential.id, {"name": "Renamed"})
    assert PASSWORD not in str(updated.config)
    assert store._mask_config(updated.config)["password"].endswith("***")
    assert not store._mask_config(updated.config)["password"].startswith(PASSWORD[:2])


def test_a_row_already_encrypted_several_times_is_repaired(store, encryptor):
    credential = _saved(store)
    row = store._db.rows[credential.id]
    for _ in range(4):
        row["config"]["password"] = encryptor.encrypt_value(row["config"]["password"])
    assert _layers(store, credential.id) == 1  # the outer layer is all that shows
    assert store.get_raw(credential.id).config["password"] == PASSWORD
    store.mark_used(credential.id)
    assert encryptor._decrypt_token(store._db.rows[credential.id]["config"]["password"]) == PASSWORD


def test_a_secret_that_starts_with_the_prefix_survives(store):
    credential = store.create(Credential(name="Odd", type="custom", config={"password": "ENC:v1:not-a-token"}))
    assert store.get_raw(credential.id).config["password"] == "ENC:v1:not-a-token"


def test_a_key_from_another_install_still_refuses(encryptor):
    other = fresh_encryptor_for_tests()
    with pytest.raises(RuntimeError, match="master key"):
        other.decrypt_value(encryptor.encrypt_value(PASSWORD))


@pytest.mark.parametrize("field", ["secret_access_key", "aws_secret_access_key", "session_token", "account_key", "sas_token", "connection_string"])
def test_cloud_secrets_are_encrypted_at_rest(store, field):
    credential = store.create(Credential(name="Cloud", type="cloud", config={field: "synthetic"}))
    assert store._db.rows[credential.id]["config"][field].startswith("ENC:v1:")
    assert store.get_raw(credential.id).config[field] == "synthetic"
    assert Encryptor._is_sensitive_field(field)
