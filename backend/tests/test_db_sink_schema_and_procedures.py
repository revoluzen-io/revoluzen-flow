"""Database Sink names and values, and Stored Procedure mode (2026-11-11).

Found live on 2026-11-08:
  * the sink ignored its Schema field and quoted `sales.orders` as one
    name, writing a table literally called "sales.orders";
  * an append failed when the table did not exist yet;
  * a nested value (a STRUCT or LIST from DuckDB) could not be written;
  * the Database Source and Copy Data forms offered a Stored Procedure mode
    the steps never ran: the Database Source ran whatever was in its Query
    box, and Copy Data asked for a table.

The sink is exercised against a real SQLite connection. Procedures need SQL
Server, MySQL or PostgreSQL, so their calls are checked as the statements
and parameters handed to the driver.
"""

from __future__ import annotations

import datetime
import decimal
import json
import sqlite3
import uuid

import duckdb
import pytest

from revoluzen_flow.nodes.activities import DbSinkNode, _bindable_rows, _table_parts
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.db_source import DbSourceNode, procedure_args, procedure_call


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# ── Table names ────────────────────────────────────────────────────────────

def test_table_parts():
    assert _table_parts("orders") == ["orders"]
    assert _table_parts("sales.orders") == ["sales", "orders"]
    assert _table_parts("db.dbo.Orders") == ["db", "dbo", "Orders"]
    assert _table_parts("orders", "sales") == ["sales", "orders"]
    # With a Schema the table is taken whole.
    assert _table_parts("orders.2024", "sales") == ["sales", "orders.2024"]
    with pytest.raises(ValueError, match="not a valid table name"):
        _table_parts("sales..orders")


def test_a_dotted_name_is_schema_and_table_not_one_name():
    conn = sqlite3.connect(":memory:")
    DbSinkNode({})._write_with_conn(conn, "main.orders", ["id"], [(1,)], "create", "?", "sqlite")
    assert "orders" in _tables(conn)
    assert "main.orders" not in _tables(conn)


def test_the_schema_field_is_used(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH DATABASE '{tmp_path / 'sales.db'}' AS sales")
    DbSinkNode({})._write_with_conn(conn, ["sales", "orders"], ["id"], [(1,)], "create", "?", "sqlite")
    assert conn.execute("SELECT COUNT(*) FROM sales.orders").fetchone() == (1,)
    assert "orders" not in _tables(conn)  # not in main


# ── Append ────────────────────────────────────────────────────────────────

def test_the_first_append_makes_the_table():
    conn = sqlite3.connect(":memory:")
    sink = DbSinkNode({})
    sink._write_with_conn(conn, "t", ["id", "name"], [(1, "a")], "append", "?", "sqlite")
    sink._write_with_conn(conn, "t", ["id", "name"], [(2, "b")], "append", "?", "sqlite")
    assert conn.execute('SELECT COUNT(*) FROM "t"').fetchone() == (2,)


def test_append_to_duckdb_with_no_connection_makes_the_table():
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=True)
    ctx.set_result("src", ctx.conn.sql("SELECT 1 AS id"))
    DbSinkNode({"_input_step_ids": ["src"], "table": "t", "mode": "append"}).execute(ctx)
    DbSinkNode({"_input_step_ids": ["src"], "table": "t", "mode": "append"}).execute(ctx)
    assert ctx.conn.sql('SELECT COUNT(*) FROM "t"').fetchall() == [(2,)]


# ── Values ────────────────────────────────────────────────────────────────

def test_nested_values_are_written_as_json_text():
    conn = sqlite3.connect(":memory:")
    duck = duckdb.connect()
    rel = duck.sql("SELECT 1 AS id, {'lat': 1.5, 'tags': ['a', 'b']} AS place, [1, 2] AS nums")
    rows = rel.fetchall()
    DbSinkNode({})._write_with_conn(conn, "t", rel.columns, rows, "create", "?", "sqlite", types=rel.types)
    place, nums = conn.execute('SELECT "place", "nums" FROM "t"').fetchone()
    assert json.loads(place) == {"lat": 1.5, "tags": ["a", "b"]}
    assert json.loads(nums) == [1, 2]


def test_values_drivers_cannot_take_become_text():
    an_id = uuid.uuid4()
    rows = [(an_id, datetime.timedelta(hours=1), decimal.Decimal("1.25"), "x")]
    fixed = _bindable_rows(rows, "sqlite")
    assert fixed == [(str(an_id), "1:00:00", 1.25, "x")]
    # Other drivers take Decimal as it is.
    assert _bindable_rows(rows, "postgresql")[0][2] == decimal.Decimal("1.25")
    plain = [(1, "a")]
    assert _bindable_rows(plain, "sqlite") is plain


# ── Stored procedures ─────────────────────────────────────────────────────

def test_procedure_calls_per_database():
    assert procedure_call("mssql", "dbo", "GetOrders", [42, "EU"]) == (
        "SET NOCOUNT ON; EXEC [dbo].[GetOrders] ?, ?", (42, "EU"))
    assert procedure_call("mssql", "", "dbo.GetOrders", {"@Start": "2025-01-01"}) == (
        "SET NOCOUNT ON; EXEC [dbo].[GetOrders] @Start = ?", ("2025-01-01",))
    assert procedure_call("mysql", "", "shop.get_orders", [1]) == ("CALL `shop`.`get_orders`(%s)", (1,))
    assert procedure_call("postgresql", "public", "orders_since", ["2025-01-01"]) == (
        'SELECT * FROM "public"."orders_since"(%s)', ("2025-01-01",))
    assert procedure_call("postgresql", "", "orders_since", {"since": "x"}) == (
        'SELECT * FROM "orders_since"("since" => %s)', ("x",))
    assert procedure_call("mssql", "", "Refresh", []) == ("SET NOCOUNT ON; EXEC [Refresh]", ())


def test_procedure_names_cannot_break_out_of_quoting():
    statement, _ = procedure_call("mssql", "", "x]; DROP TABLE t; --", [])
    assert statement == "SET NOCOUNT ON; EXEC [x]]; DROP TABLE t; --]"
    with pytest.raises(ValueError, match="not a valid parameter name"):
        procedure_call("mssql", "", "p", {"a = 1; DROP": 1})


def test_procedure_mode_is_refused_where_it_cannot_run():
    with pytest.raises(ValueError, match="SQL Server, MySQL and PostgreSQL"):
        procedure_call("sqlite", "", "p", [])
    with pytest.raises(ValueError, match="in order"):
        procedure_call("mysql", "", "p", {"a": 1})
    with pytest.raises(ValueError, match="JSON list"):
        procedure_args("not json")
    assert procedure_args('[1, "a"]') == [1, "a"]
    assert procedure_args(None) == []


def test_the_database_source_runs_the_procedure_not_the_query(monkeypatch):
    seen: dict = {}

    def fake_config(connection_id, *args, **kwargs):
        return ({"host": "h"}, "mssql")

    def fake_execute(self, conn_type, config, query, ctx=None, connection_id=None, params=()):
        seen.update(query=query, params=params)
        return [(1, "a"), (2, "b"), (3, "c")], ["id", "name"]

    monkeypatch.setattr("revoluzen_flow.nodes.db_source._get_connection_config", fake_config)
    monkeypatch.setattr(DbSourceNode, "_execute_real", fake_execute)
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=False)
    rel = DbSourceNode({
        "connection_id": "c1", "source_mode": "procedure", "schema": "dbo",
        "procedure": "GetOrders", "proc_args": [42], "query": "SELECT 'the old query'",
        "sync_mode": "incremental", "watermark_column": "id", "sample_rows": 2,
    }).execute(ctx)
    # No incremental or sample wrapper goes around EXEC; a sample keeps its first rows.
    assert seen == {"query": "SET NOCOUNT ON; EXEC [dbo].[GetOrders] ?", "params": (42,)}
    assert rel.fetchall() == [(1, "a"), (2, "b")]


def test_the_warehouse_route_refuses_procedure_mode():
    from revoluzen_flow.nodes.generic import _dialect_source_params

    ctx = ExecutionContext(conn=duckdb.connect(), full_run=True)
    with pytest.raises(ValueError, match="Stored Procedure mode works with"):
        _dialect_source_params({"source_mode": "procedure", "procedure": "p"}, "snowflake", ctx)


class _Cursor:
    """A DB-API cursor whose results come one after another, like a procedure's."""

    def __init__(self, results):
        self._results = list(results)
        self.description = self._results[0]

    def nextset(self):
        self._results.pop(0)
        if not self._results:
            return None
        self.description = self._results[0]
        return True


def test_sql_server_moves_past_empty_results_to_the_rows():
    cur = _Cursor([None, None, [("id",)]])
    assert DbSourceNode._mssql_has_rows(cur, "SET NOCOUNT ON; EXEC [p]") is True
    assert cur.description == [("id",)]
    assert DbSourceNode._mssql_has_rows(_Cursor([None]), "SET NOCOUNT ON; EXEC [p]") is False
    # A plain query is read as before.
    assert DbSourceNode._mssql_has_rows(_Cursor([None]), "SELECT 1") is True


def test_copy_data_runs_a_stored_procedure(monkeypatch):
    from revoluzen_flow.nodes.flow_control import CopyDataNode

    seen: dict = {}

    def fake_config(connection_id, *args, **kwargs):
        return ({"host": "h"}, "postgresql")

    def fake_execute(self, conn_type, config, query, ctx=None, connection_id=None, params=()):
        seen.update(query=query, params=params)
        return [(7,)], ["n"]

    monkeypatch.setattr("revoluzen_flow.nodes.db_source._get_connection_config", fake_config)
    monkeypatch.setattr(DbSourceNode, "_execute_real", fake_execute)
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=True)
    node = CopyDataNode({
        "source_kind": "stored_procedure", "source_schema": "public",
        # The name and named parameters as the form saved them before 2026-11-11.
        "stored_procedure": "count_orders", "proc_params": '{"since": "2025-01-01"}',
    })
    rel = node._read_from_source(ctx, "c1")
    assert seen == {"query": 'SELECT * FROM "public"."count_orders"("since" => %s)', "params": ("2025-01-01",)}
    assert rel.fetchall() == [(7,)]
