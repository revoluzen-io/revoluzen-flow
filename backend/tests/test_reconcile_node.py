"""Reconcile: compare the data with the system of record before it lands."""

from __future__ import annotations

import duckdb
import pytest

from revoluzen_flow.ir.node_metadata import contract_for
from revoluzen_flow.nodes import control_extras
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.reconcile import ReconcileNode

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx, sid, sql):
    ctx.set_result(sid, ctx.conn.sql(sql))


ORDERS = "SELECT * FROM (VALUES (1, 'a', 10.50), (2, 'b', 20.25), (3, 'c', 30.00)) AS t(order_id, region, amount)"


def _node(**params):
    return ReconcileNode(params={"_input_step_ids": ["data", "ref"], **params})


def test_matching_data_passes_through_unchanged():
    ctx = _ctx()
    _seed(ctx, "data", ORDERS)
    _seed(ctx, "ref", ORDERS)
    out = _node(sum_columns=["amount"], key_columns=["order_id"]).execute(ctx)
    assert sorted(out.fetchall()) == [(1, "a", 10.5), (2, "b", 20.25), (3, "c", 30.0)]


def test_a_missing_row_stops_the_run_and_names_counts_not_values():
    ctx = _ctx()
    _seed(ctx, "data", ORDERS + " WHERE order_id < 3")
    _seed(ctx, "ref", ORDERS)
    with pytest.raises(ValueError) as err:
        _node(sum_columns=["amount"], key_columns=["order_id"]).execute(ctx)
    message = str(err.value)
    assert "row count 2 vs 3" in message
    assert "total of amount 30.75 vs 60.75" in message
    assert "1 order_id key in the reference but not in the data" in message
    # Run errors reach the Copilot, which works from names only: the missing
    # row's key and region never appear.
    assert "'c'" not in message and " 3 " not in message.split(";")[-1]


def test_warn_carries_on_and_the_report_says_what_differed():
    ctx = _ctx()
    _seed(ctx, "data", ORDERS + " WHERE order_id < 3")
    _seed(ctx, "ref", ORDERS)
    rel = _node(key_columns=["order_id"], on_mismatch="warn", output="report").execute(ctx)
    assert rel.columns == ["check_name", "column_name", "value", "reference", "difference", "allowed", "passed", "detail"]
    rows = {r[0]: r for r in rel.fetchall()}
    assert rows["row_count"][2:7] == (2.0, 3.0, -1.0, 0.0, False)
    assert rows["keys_missing"][6] is False and rows["keys_missing"][2] == 1.0
    assert rows["keys_extra"][6] is True


def test_tolerance_allows_a_small_difference():
    ctx = _ctx()
    _seed(ctx, "data", "SELECT range AS id FROM range(98)")
    _seed(ctx, "ref", "SELECT range AS id FROM range(100)")
    _node(tolerance_pct=2).execute(ctx)          # 98 vs 100 is within 2%
    with pytest.raises(ValueError, match=r"row count 98 vs 100 \(allowed ±1\)"):
        _node(tolerance_pct=1).execute(ctx)


def test_floating_point_totals_are_not_failed_for_the_last_digits():
    ctx = _ctx()
    _seed(ctx, "data", "SELECT 0.1::DOUBLE AS amount FROM range(10)")
    _seed(ctx, "ref", "SELECT 1.0::DOUBLE AS amount")
    _node(compare_row_count=False, sum_columns=["amount"]).execute(ctx)


def test_the_reference_input_can_be_pinned_by_step():
    ctx = _ctx()
    _seed(ctx, "data", ORDERS)
    _seed(ctx, "ref", ORDERS + " WHERE order_id = 1")
    # Pin the FIRST wired input as the system of record: now the data (one
    # row, step 'ref') is short of the reference (three rows, step 'data').
    with pytest.raises(ValueError, match="row count 1 vs 3"):
        _node(reference_step="data").execute(ctx)


def test_query_reference_reads_one_select_from_a_connection(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "data", ORDERS)
    seen = {}

    def fake(conn_id, sql, timeout=60):
        seen["conn"], seen["sql"] = conn_id, sql
        return [{"order_id": 1, "amount": 10.5}, {"order_id": 2, "amount": 20.25}, {"order_id": 3, "amount": 30}], -1

    monkeypatch.setattr(control_extras, "_run_connection_sql", fake)
    node = ReconcileNode(params={
        "_input_step_ids": ["data"], "reference": "query", "connection_id": "erp",
        "query": "SELECT id AS order_id, amount FROM orders;", "sum_columns": ["amount"], "key_columns": ["order_id"],
    })
    node.execute(ctx)
    assert seen == {"conn": "erp", "sql": "SELECT id AS order_id, amount FROM orders"}


def test_an_empty_reference_is_a_finding_not_a_crash(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "data", ORDERS)
    monkeypatch.setattr(control_extras, "_run_connection_sql", lambda c, s, timeout=60: ([], -1))
    node = ReconcileNode(params={
        "_input_step_ids": ["data"], "reference": "query", "connection_id": "erp",
        "query": "SELECT order_id, amount FROM orders WHERE 1 = 0", "sum_columns": ["amount"],
    })
    with pytest.raises(ValueError, match="row count 3 vs 0"):
        node.execute(ctx)


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "SELECT 1; DROP TABLE orders",
    "update orders set amount = 0",
])
def test_the_reference_query_may_only_read(monkeypatch, sql):
    ctx = _ctx()
    _seed(ctx, "data", ORDERS)
    monkeypatch.setattr(control_extras, "_run_connection_sql", lambda *a, **k: pytest.fail("must not run"))
    node = ReconcileNode(params={
        "_input_step_ids": ["data"], "reference": "query", "connection_id": "erp", "query": sql,
    })
    with pytest.raises(ValueError, match="one SELECT"):
        node.execute(ctx)


def test_settings_that_cannot_work_are_named():
    ctx = _ctx()
    _seed(ctx, "data", ORDERS)
    _seed(ctx, "ref", "SELECT 1 AS order_id")
    with pytest.raises(ValueError, match="at least one thing to compare"):
        _node(compare_row_count=False).execute(ctx)
    with pytest.raises(ValueError, match="'amount' is not in the reference"):
        _node(sum_columns=["amount"]).execute(ctx)
    with pytest.raises(ValueError, match="'nope' is not in the data"):
        _node(key_columns=["nope"]).execute(ctx)
    lone = ExecutionContext(conn=duckdb.connect(":memory:"))
    _seed(lone, "data", ORDERS)
    with pytest.raises(ValueError, match="second input"):
        ReconcileNode(params={"_input_step_ids": ["data"]}).execute(lone)


def test_the_step_takes_one_or_two_inputs():
    contract = contract_for("reconcile")
    assert (contract["required"], contract["optional"], contract["variadic"]) == (1, 1, False)


def test_a_mismatch_stops_the_write_in_a_real_run(authed_client, data_dir):
    def csv(name, n):
        path = f"{data_dir}/{name}.csv"
        with open(path, "w", encoding="utf-8") as f:
            f.write("order_id,amount\n")
            f.writelines(f"{i},{i * 10}\n" for i in range(n))
        return path

    data, ref = csv("rec_data", 4), csv("rec_ref", 5)
    r = authed_client.post("/api/workflows", json={
        "name": "reconcile-orders",
        "steps": [
            {"id": "src", "type": "csv_source", "label": "orders", "params": {"file_path": data}, "position": {"x": 0, "y": 0}},
            {"id": "erp", "type": "csv_source", "label": "erp", "params": {"file_path": ref}, "position": {"x": 0, "y": 1}},
            {"id": "rec", "type": "reconcile", "label": "match the ERP",
             "params": {"reference_step": "erp", "sum_columns": ["amount"], "key_columns": ["order_id"]},
             "position": {"x": 1, "y": 0}},
            {"id": "out", "type": "local_table_sink", "label": "write",
             "params": {"schema_name": "default", "table_name": "reconciled_orders", "mode": "replace"},
             "position": {"x": 2, "y": 0}},
        ],
        "connections": [
            {"from_step": "src", "to_step": "rec"},
            {"from_step": "erp", "to_step": "rec"},
            {"from_step": "rec", "to_step": "out"},
        ],
    })
    assert r.status_code in (200, 201), r.text
    wid = r.json().get("id") or r.json()["workflow"]["id"]
    run = authed_client.post(f"/api/execute/workflow/{wid}?full_run=true").json()
    assert run["status"] == "error", run
    rec = run["step_results"]["rec"]
    assert rec["status"] == "error"
    assert "row count 4 vs 5" in rec["error"]
    assert run["step_results"].get("out", {}).get("status") != "success"
    tables = authed_client.get("/api/storage/tables").json()
    tables = tables if isinstance(tables, list) else tables.get("tables", tables.get("items", []))
    assert not any(t.get("name") == "reconciled_orders" for t in tables)
