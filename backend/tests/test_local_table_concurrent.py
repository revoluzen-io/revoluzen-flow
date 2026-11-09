"""Several pipelines writing one managed table at the same time.

Two runs writing one table left the Storage page showing 0 rows: one writer
counted rows while the other was replacing the file, the count failed, and
the failure was stored as 0.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


def _ctx(conn, data_dir, datastore, schema_history_store):
    results: dict[str, object] = {}
    return SimpleNamespace(
        conn=conn,
        app_state={
            "data_dir": str(data_dir),
            "datastore": datastore,
            "schema_history_store": schema_history_store,
        },
        workspace_id="default",
        _results=results,
        run_id="run-test",
        get_inputs=lambda ids: [results[i] for i in ids if i in results],
    )


def _table_dir(data_dir, name):
    return os.path.join(str(data_dir), "tables", "default", "default", name)


def test_concurrent_replaces_leave_a_true_row_count(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.local_table import LocalTableSinkNode

    rows = 50_000

    def write(_):
        conn = duckdb.connect()
        try:
            ctx = _ctx(conn, tmp_path, datastore, schema_history_store)
            ctx._results["up"] = conn.sql(f"SELECT range AS id FROM range({rows})")
            LocalTableSinkNode({
                "schema_name": "default", "table_name": "shared", "mode": "replace",
            }).execute(ctx)
        finally:
            conn.close()

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(write, range(12)))

    table = datastore.find_table_by_name("default", "default", "shared")
    assert table.row_count == rows
    assert table.part_count == 1

    assert os.listdir(_table_dir(tmp_path, "shared")) == ["part-000.parquet"]
    conn = duckdb.connect()
    glob = os.path.join(_table_dir(tmp_path, "shared"), "part-*.parquet").replace("\\", "/")
    assert conn.sql(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0] == rows
    conn.close()


def test_failed_replace_keeps_the_old_data(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.local_table import LocalTableSinkNode

    conn = duckdb.connect()
    ctx = _ctx(conn, tmp_path, datastore, schema_history_store)
    params = {"schema_name": "default", "table_name": "kept", "mode": "replace"}
    ctx._results["up"] = conn.sql("SELECT 1 AS id UNION ALL SELECT 2")
    LocalTableSinkNode(params).execute(ctx)

    # Parses, then fails while the COPY is running.
    ctx._results["up"] = conn.sql("SELECT CAST('not a number' AS INTEGER) AS id FROM range(1)")
    with pytest.raises(Exception):
        LocalTableSinkNode(params).execute(ctx)

    assert os.listdir(_table_dir(tmp_path, "kept")) == ["part-000.parquet"]
    glob = os.path.join(_table_dir(tmp_path, "kept"), "part-*.parquet").replace("\\", "/")
    assert conn.sql(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0] == 2
    conn.close()


def test_a_failed_count_does_not_store_zero(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes import local_table
    from revoluzen_flow.nodes.local_table import LocalTableSinkNode

    conn = duckdb.connect()
    ctx = _ctx(conn, tmp_path, datastore, schema_history_store)
    params = {"schema_name": "default", "table_name": "counted", "mode": "replace"}
    ctx._results["up"] = conn.sql("SELECT 1 AS id UNION ALL SELECT 2")
    LocalTableSinkNode(params).execute(ctx)

    original = local_table.fetch_scalar
    local_table.fetch_scalar = lambda *_: (_ for _ in ()).throw(RuntimeError("file busy"))
    try:
        LocalTableSinkNode(params).execute(ctx)
    finally:
        local_table.fetch_scalar = original
        conn.close()

    assert datastore.find_table_by_name("default", "default", "counted").row_count == 2
