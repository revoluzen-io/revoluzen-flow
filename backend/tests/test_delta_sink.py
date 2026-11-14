"""Delta Lake Sink writes a real Delta table, or refuses (2026-11-11).

Without the `deltalake` package the sink used to write plain Parquet files
with no `_delta_log`. No Delta reader can open such a folder, yet the run
reported success. Now it writes through `deltalake` or stops with a plain
message and writes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.sinks import DeltaSinkNode, _delta_partition_columns

ROWS = (
    "SELECT * FROM (VALUES (1, 'north', 10.5), (2, 'south', 20.0), (3, 'north', 7.25)) "
    "AS t(id, region, amount)"
)


def _ctx(tmp_path: Path, sql: str = ROWS) -> ExecutionContext:
    conn = duckdb.connect()
    ctx = ExecutionContext(conn=conn, data_dir=str(tmp_path), full_run=True)
    ctx.set_result("src", conn.sql(sql))
    return ctx


def _run(ctx: ExecutionContext, **params):
    return DeltaSinkNode(params={"_input_step_ids": ["src"], **params}).execute(ctx)


def test_writes_a_delta_table_that_delta_readers_open(tmp_path):
    deltalake = pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="lake/customers", mode="overwrite")

    folder = tmp_path / "lake" / "customers"
    assert (folder / "_delta_log").is_dir()
    table = deltalake.DeltaTable(str(folder))
    data = table.to_pyarrow_table()
    assert data.num_rows == 3
    assert set(data.column_names) == {"id", "region", "amount"}


def test_append_adds_rows_and_overwrite_replaces_them(tmp_path):
    deltalake = pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="t", mode="overwrite")
    _run(_ctx(tmp_path), table_path="t", mode="append")
    table = deltalake.DeltaTable(str(tmp_path / "t"))
    assert table.version() == 1
    assert table.to_pyarrow_table().num_rows == 6

    _run(_ctx(tmp_path, ROWS + " WHERE id = 1"), table_path="t", mode="overwrite")
    table = deltalake.DeltaTable(str(tmp_path / "t"))
    assert table.to_pyarrow_table().num_rows == 1


def test_partition_columns_typed_as_text_are_used(tmp_path):
    deltalake = pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="p", partition_by="region, ")
    table = deltalake.DeltaTable(str(tmp_path / "p"))
    assert table.metadata().partition_columns == ["region"]
    assert table.to_pyarrow_table().num_rows == 3


def test_partition_column_list_helper():
    assert _delta_partition_columns(None) == []
    assert _delta_partition_columns("") == []
    assert _delta_partition_columns("a, b ,,c") == ["a", "b", "c"]
    assert _delta_partition_columns(["a", " b ", ""]) == ["a", "b"]


def test_without_the_add_on_nothing_is_written(tmp_path, monkeypatch):
    # `None` in sys.modules makes `import deltalake` raise ImportError.
    monkeypatch.setitem(sys.modules, "deltalake", None)
    with pytest.raises(ValueError, match="needs an add-on") as err:
        _run(_ctx(tmp_path), table_path="lake/orders")
    assert "nothing was written" in str(err.value)
    assert not (tmp_path / "lake").exists()


def test_a_cloud_address_is_not_put_under_the_data_folder(tmp_path, monkeypatch):
    pytest.importorskip("deltalake")
    seen: dict = {}

    def fake_write(ctx, source, table_path, mode, partition_by, **kwargs):
        seen.update(path=table_path, mode=mode, partition_by=partition_by, **kwargs)

    monkeypatch.setattr(DeltaSinkNode, "_write_with_deltalake", staticmethod(fake_write))
    _run(
        _ctx(tmp_path), table_path="s3://bucket/customers", write_mode="append",
        partition_by=["region"], storage_options='{"AWS_REGION": "eu-west-1", "EMPTY": ""}',
    )
    assert seen["path"] == "s3://bucket/customers"
    assert seen["mode"] == "append"
    assert seen["partition_by"] == ["region"]
    # Older steps saved the options as JSON text; empty values are left out.
    assert seen["storage_options"] == {"AWS_REGION": "eu-west-1"}


def test_the_write_mode_picked_in_the_form_is_used(tmp_path):
    # The form saves `write_mode`. It used to be ignored, so Append overwrote.
    deltalake = pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="t", write_mode="overwrite")
    _run(_ctx(tmp_path), table_path="t", write_mode="append")
    assert deltalake.DeltaTable(str(tmp_path / "t")).to_pyarrow_table().num_rows == 6


def test_merge_updates_matching_rows_and_adds_the_rest(tmp_path):
    deltalake = pytest.importorskip("deltalake")
    # First run: nothing to merge into, so the table is created.
    _run(_ctx(tmp_path), table_path="m", write_mode="merge", merge_keys="id")
    changes = "SELECT * FROM (VALUES (2, 'west', 99.0), (4, 'east', 1.0)) AS t(id, region, amount)"
    _run(_ctx(tmp_path, changes), table_path="m", write_mode="merge", merge_keys="id")

    rows = sorted(deltalake.DeltaTable(str(tmp_path / "m")).to_pyarrow_table().to_pylist(), key=lambda r: r["id"])
    assert [r["id"] for r in rows] == [1, 2, 3, 4]
    assert rows[1] == {"id": 2, "region": "west", "amount": 99.0}


def test_merge_without_keys_is_refused(tmp_path):
    with pytest.raises(ValueError, match="Merge Key Columns"):
        _run(_ctx(tmp_path), table_path="m", write_mode="merge")


def test_schema_merge_lets_an_append_add_a_column(tmp_path):
    deltalake = pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="s")
    wider = "SELECT 9 AS id, 'north' AS region, 1.0 AS amount, 'new' AS note"
    _run(_ctx(tmp_path, wider), table_path="s", write_mode="append", schema_mode="merge")
    data = deltalake.DeltaTable(str(tmp_path / "s")).to_pyarrow_table()
    assert "note" in data.column_names
    assert data.num_rows == 4


def test_a_failed_write_leaves_the_run_able_to_query(tmp_path):
    # DuckDB streams the rows; a write that fails part way used to leave the
    # run's connection busy, so every later step failed too.
    pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="f")
    ctx = _ctx(tmp_path, "SELECT 1 AS only_column")
    with pytest.raises(Exception):
        _run(ctx, table_path="f", write_mode="append")
    assert ctx.conn.sql("SELECT 42").fetchall() == [(42,)]


def test_the_older_mode_name_is_still_read(tmp_path):
    deltalake = pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="o", mode="overwrite")
    _run(_ctx(tmp_path), table_path="o", mode="append")
    assert deltalake.DeltaTable(str(tmp_path / "o")).to_pyarrow_table().num_rows == 6


def _read(ctx: ExecutionContext, **params):
    from revoluzen_flow.nodes.sources import DeltaSourceNode

    return DeltaSourceNode(params=params).execute(ctx)


def test_the_source_reads_only_the_live_rows(tmp_path):
    # Two overwrites leave the first run's files on disk. Reading every Parquet
    # file in the folder used to return both runs' rows.
    pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="live")
    _run(_ctx(tmp_path, ROWS + " WHERE id = 1"), table_path="live")
    ctx = _ctx(tmp_path)
    assert _read(ctx, table_path="live").fetchall() == [(1, "north", 10.5)]


def test_the_source_reads_an_earlier_version_and_filters(tmp_path):
    pytest.importorskip("deltalake")
    _run(_ctx(tmp_path), table_path="tt")
    _run(_ctx(tmp_path, ROWS + " WHERE id = 1"), table_path="tt")
    ctx = _ctx(tmp_path)
    assert len(_read(ctx, table_path="tt", version=0).fetchall()) == 3
    kept = _read(ctx, table_path="tt", version=0, predicate="region = 'north'").fetchall()
    assert sorted(row[0] for row in kept) == [1, 3]


def test_the_source_without_the_add_on_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "deltalake", None)
    with pytest.raises(ValueError, match="needs an add-on"):
        _read(_ctx(tmp_path), table_path="anything")


def test_a_delta_step_needs_a_table_path_not_a_connection():
    # Delta steps read `table_path` and take no saved connection; asking for
    # one made them impossible to use from Source and Destination.
    from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow
    from revoluzen_flow.ir.validator import validate_workflow

    def errors_for(params):
        wf = Workflow(
            id="wf-delta", name="delta",
            steps=[
                Step(id="src", type=StepType.SOURCE, params={"connector_type": "delta", **params}),
                Step(id="out", type=StepType.DESTINATION, params={"connector_type": "delta", **params}),
            ],
            connections=[StepConnection(from_step="src", to_step="out")],
        )
        return [e.message for e in validate_workflow(wf) if getattr(e, "step_id", None) in ("src", "out")]

    assert not [m for m in errors_for({"table_path": "lake/orders"}) if "delta" in m.lower()]
    missing = errors_for({})
    assert any("requires a Table Path" in m for m in missing)
    assert not any("connection_id" in m for m in missing if "delta" in m.lower())


def test_an_unknown_write_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="overwrite, append or merge"):
        _run(_ctx(tmp_path), table_path="t", write_mode="upsert")
    assert not (tmp_path / "t").exists()
