"""A saved credential reaches a connection the way the connection says it should.

The connection form records which field takes which credential key. Only some
readers used to honour that record, so a field filled from a differently named
key tested fine in the form and then came up empty at run time.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from revoluzen_flow.connections.credentials import apply_linked_credential, resolve_credentials


def test_without_a_map_every_key_is_merged_under_its_own_name():
    config = {"host": "db.example.test"}
    apply_linked_credential(config, {"username": "reader", "password": "synthetic"})
    assert config == {"host": "db.example.test", "username": "reader", "password": "synthetic"}


def test_a_map_fills_only_the_fields_it_names():
    config = {"host": "db.example.test", "credential_field_map": {"token": "api_key"}}
    apply_linked_credential(config, {"api_key": "synthetic", "other": "ignored"})
    assert config["token"] == "synthetic"
    assert "api_key" not in config
    assert "other" not in config


def test_a_map_naming_a_missing_key_leaves_the_field_alone():
    config = {"token": "typed", "credential_field_map": {"token": "absent"}}
    apply_linked_credential(config, {"api_key": "synthetic"})
    assert config["token"] == "typed"


def test_a_malformed_map_entry_is_skipped():
    config = {"credential_field_map": {"token": 7, 8: "api_key"}}
    apply_linked_credential(config, {"api_key": "synthetic"})
    assert "token" not in config
    assert 8 not in config


def test_resolve_credentials_and_the_node_reader_agree():
    credential = SimpleNamespace(config={"api_key": "synthetic"}, workspace_id="default")
    connection = SimpleNamespace(
        id="c1", type="rest_api", credential_id="k1", workspace_id="default", vault_key=None,
        config={"base_url": "https://api.example.test", "credential_field_map": {"token": "api_key"}},
    )
    store = SimpleNamespace(get_raw=lambda *a, **k: credential, mark_used=lambda *a, **k: None)
    connections = SimpleNamespace(get=lambda *a, **k: connection)
    state = {"credential_store": store, "connection_store": connections}
    with patch("revoluzen_flow.main.app_state", state):
        resolved = resolve_credentials(connection)
        from revoluzen_flow.nodes.db_source import _get_connection_config
        node_config, _ = _get_connection_config("c1")
    assert resolved["token"] == "synthetic"
    assert node_config["token"] == "synthetic"
