"""A Copilot draft is told what the chosen source really holds.

Without this it guessed: a draft read `return_reason` from a table whose column
is `reason`, and couldn't run. Names and types go to the model, never rows.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from revoluzen_flow.ai.clarify_draft import DraftEndpoint
from revoluzen_flow.ai.draft_choices import DraftChoices, describe_choices_for_planner, source_columns


@pytest.fixture
def state(monkeypatch, tmp_path):
    from revoluzen_flow import main

    database = tmp_path / "shop.db"
    con = sqlite3.connect(database)
    con.execute("create table returns (return_id integer, reason text, refund_amount real)")
    con.execute("insert into returns values (1, 'A secret reason in a row', 9.5)")
    con.commit()
    con.close()
    connection = SimpleNamespace(id="c1", type="sqlite", config={"file_path": str(database)}, credential_id=None, vault_key=None, workspace_id="default")
    store = SimpleNamespace(get=lambda cid, workspace_id=None: connection if cid == "c1" and workspace_id == "default" else None)
    monkeypatch.setitem(main.app_state, "connection_store", store)
    monkeypatch.setitem(main.app_state, "data_dir", str(tmp_path))
    return tmp_path


def _from_connection(connection_id="c1", table="returns"):
    return DraftChoices(source=DraftEndpoint(kind="connection", name="Shop", connection_id=connection_id, connection_type="sqlite", table=table))


def test_a_database_tables_columns_are_read(state):
    columns = source_columns(_from_connection(), "default")
    assert [c["name"] for c in columns] == ["return_id", "reason", "refund_amount"]


def test_a_connection_in_another_workspace_gives_nothing(state):
    assert source_columns(_from_connection(), "other") == []


def test_no_table_chosen_gives_nothing(state):
    assert source_columns(_from_connection(table=""), "default") == []


def test_a_table_that_cant_be_read_gives_nothing_and_the_draft_goes_on(state):
    assert source_columns(_from_connection(table="missing"), "default") == []


def test_a_files_columns_are_read(state):
    (state / "orders.csv").write_text("order_id,status\n1,paid\n", encoding="utf-8")
    choices = DraftChoices(source=DraftEndpoint(kind="file", name="orders.csv", file_path=str(state / "orders.csv"), file_format="csv"))
    assert [c["name"] for c in source_columns(choices, "default")] == ["order_id", "status"]


def test_a_described_source_has_no_columns(state):
    assert source_columns(DraftChoices(source=DraftEndpoint(kind="text", name="our CRM")), "default") == []


def test_the_model_is_told_the_columns_and_not_to_guess(state):
    choices = _from_connection()
    context = describe_choices_for_planner(choices, source_columns(choices, "default"))
    assert '"reason"' in context
    assert "don't guess" in context


def test_no_row_values_reach_the_model(state):
    choices = _from_connection()
    context = describe_choices_for_planner(choices, source_columns(choices, "default"))
    assert "A secret reason" not in context


def test_without_columns_the_context_is_as_before(state):
    context = describe_choices_for_planner(_from_connection())
    assert "don't guess" not in context
    assert "columns" not in context
