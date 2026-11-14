"""Storage results a pipeline leaves behind.

* Pipeline Output keeps ONE file per pipeline and name in Storage ->
  Pipeline Outputs, replaced on every run, listed once and stamped with the
  run that last wrote it.
* Managed Table Sink records which saved version of its pipeline loaded the
  table, and keeps a short history of loads, so the Storage page can say
  "Prepared by X, version N" and show what each edit of the preparation did.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest


def _ctx(conn, data_dir, datastore, schema_history_store, *, workflow_id=None,
         run_id="run-1", metadata=None, executions=None):
    results: dict[str, object] = {}
    state = {
        "data_dir": str(data_dir),
        "datastore": datastore,
        "schema_history_store": schema_history_store,
    }
    if executions is not None:
        state["execution_store"] = executions
    return SimpleNamespace(
        conn=conn,
        app_state=state,
        workspace_id="default",
        _results=results,
        run_id=run_id,
        workflow_id=workflow_id,
        workflow_metadata=metadata or {},
        step_params={},
        get_inputs=lambda ids: [results[i] for i in ids if i in results],
    )


def _outputs(datastore):
    from revoluzen_flow.datastore.models import OBJECT_KIND_OUTPUT
    return datastore.list_objects("default", kind=OBJECT_KIND_OUTPUT, include_deleted=False)


# ── Pipeline Output ─────────────────────────────────────────────────────


def test_each_run_replaces_the_one_file_and_its_one_listing(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.pipeline_output import PipelineOutputNode

    conn = duckdb.connect()
    try:
        for run_id, rows in (("run-1", 5), ("run-2", 3)):
            ctx = _ctx(conn, tmp_path, datastore, schema_history_store, workflow_id="wf1", run_id=run_id)
            ctx._results["up"] = conn.sql(f"SELECT range AS id FROM range({rows})")
            PipelineOutputNode({
                "output_name": "Daily Sales", "format": "csv", "_input_step_ids": ["up"],
            }).execute(ctx)
    finally:
        conn.close()

    folder = os.path.join(str(tmp_path), "outputs", "default", "wf1", "latest")
    path = os.path.join(folder, "daily_sales.csv")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().strip().splitlines()
    assert lines[0] == "id"
    assert len(lines) == 4, "the second run replaced the first run's rows"
    assert os.listdir(folder) == ["daily_sales.csv"], "no staging file left behind"

    listed = _outputs(datastore)
    assert len(listed) == 1, "one listing per file, not one per run"
    only = listed[0]
    assert (only.name, only.pipeline_id, only.run_id, only.row_count) == ("daily_sales.csv", "wf1", "run-2", 3)


@pytest.mark.parametrize("fmt,reader", [("parquet", "read_parquet"), ("json", "read_json_auto")])
def test_parquet_and_json_outputs_read_back(tmp_path, datastore, schema_history_store, fmt, reader):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.common.duckdb_compat import fetch_scalar
    from revoluzen_flow.nodes.pipeline_output import PipelineOutputNode

    conn = duckdb.connect()
    try:
        ctx = _ctx(conn, tmp_path, datastore, schema_history_store, workflow_id="wf2")
        ctx._results["up"] = conn.sql("SELECT range AS id, 'x' AS tag FROM range(4)")
        PipelineOutputNode({"output_name": "result", "format": fmt, "_input_step_ids": ["up"]}).execute(ctx)
        path = os.path.join(str(tmp_path), "outputs", "default", "wf2", "latest", f"result.{fmt}")
        count = fetch_scalar(conn.sql(f"SELECT COUNT(*) FROM {reader}('{path.replace(chr(92), '/')}')"))
    finally:
        conn.close()
    assert count == 4
    if fmt == "json":
        with open(path, encoding="utf-8") as fh:
            assert isinstance(json.load(fh), list), "JSON output is one array, not one object per line"


def test_an_unsaved_canvas_writes_under_adhoc(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.pipeline_output import PipelineOutputNode

    conn = duckdb.connect()
    try:
        ctx = _ctx(conn, tmp_path, datastore, schema_history_store, workflow_id=None)
        ctx._results["up"] = conn.sql("SELECT 1 AS id")
        PipelineOutputNode({"output_name": "scratch", "_input_step_ids": ["up"]}).execute(ctx)
    finally:
        conn.close()
    assert os.path.isfile(os.path.join(str(tmp_path), "outputs", "default", "adhoc", "latest", "scratch.csv"))


def test_a_name_is_required(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.pipeline_output import PipelineOutputNode

    conn = duckdb.connect()
    try:
        ctx = _ctx(conn, tmp_path, datastore, schema_history_store, workflow_id="wf3")
        ctx._results["up"] = conn.sql("SELECT 1 AS id")
        with pytest.raises(ValueError, match="output name is required"):
            PipelineOutputNode({"output_name": "  ", "_input_step_ids": ["up"]}).execute(ctx)
        with pytest.raises(ValueError, match="unknown format"):
            PipelineOutputNode({"output_name": "x", "format": "xml", "_input_step_ids": ["up"]}).execute(ctx)
    finally:
        conn.close()
    assert _outputs(datastore) == []


def test_it_is_a_registered_replace_style_sink():
    from revoluzen_flow.backfills.idempotency import classify
    from revoluzen_flow.ir.node_metadata import side_effect_class_for
    from revoluzen_flow.nodes.pipeline_output import PipelineOutputNode
    from revoluzen_flow.nodes.registry import NodeRegistry, get_registry

    get_registry()
    assert side_effect_class_for("pipeline_output") == "passthrough", "Preview and Sample skip the write"
    assert classify("pipeline_output", {}) == "replace", "safe to re-run and backfill"
    entry = next(t for t in NodeRegistry.all_types() if t["type"] == "pipeline_output")
    assert entry["label"] == "Pipeline Output"
    assert [p["name"] for p in entry["param_schema"]] == ["output_name", "format"]
    assert "would replace the pipeline output daily.csv with 2 rows" == PipelineOutputNode.preview_message(
        {"output_name": "daily"}, 2,
    )


# ── Managed Table Sink: the version that loaded the table ───────────────


def test_a_table_records_the_loading_version_and_its_loads(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.local_table import LocalTableSinkNode

    conn = duckdb.connect()
    try:
        for version, rows in ((3, 4), (4, 6)):
            ctx = _ctx(
                conn, tmp_path, datastore, schema_history_store,
                workflow_id="prep1", run_id=f"run-{version}", metadata={"_run_version": version},
            )
            ctx._results["up"] = conn.sql(f"SELECT range AS id FROM range({rows})")
            LocalTableSinkNode({"schema_name": "sales", "table_name": "people", "mode": "replace"}).execute(ctx)
    finally:
        conn.close()

    table = datastore.find_table_by_name("default", "sales", "people")
    assert table.schema_name == "sales", "a new schema comes into being with its first table"
    assert (table.prep_workflow_id, table.prep_workflow_version) == ("prep1", 4)
    assert table.prep_loaded_at is not None
    assert [(h["version"], h["rows"], h["run_id"]) for h in table.load_history] == [
        (4, 6, "run-4"), (3, 4, "run-3"),
    ]


def test_a_scheduled_run_finds_its_version_on_its_record(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.nodes.local_table import LocalTableSinkNode

    executions = SimpleNamespace(
        get=lambda run_id: SimpleNamespace(workflow_version=7) if run_id == "sched-1" else None,
    )
    conn = duckdb.connect()
    try:
        ctx = _ctx(
            conn, tmp_path, datastore, schema_history_store,
            workflow_id="prep2", run_id="sched-1", executions=executions,
        )
        ctx._results["up"] = conn.sql("SELECT 1 AS id")
        LocalTableSinkNode({"table_name": "nightly", "mode": "replace"}).execute(ctx)
    finally:
        conn.close()
    assert datastore.find_table_by_name("default", "default", "nightly").prep_workflow_version == 7


def test_the_load_history_is_capped(tmp_path, datastore, schema_history_store):
    duckdb = pytest.importorskip("duckdb")
    from revoluzen_flow.datastore.models import LOAD_HISTORY_LIMIT
    from revoluzen_flow.nodes.local_table import LocalTableSinkNode

    conn = duckdb.connect()
    try:
        for i in range(LOAD_HISTORY_LIMIT + 3):
            ctx = _ctx(
                conn, tmp_path, datastore, schema_history_store,
                workflow_id="prep3", run_id=f"run-{i}", metadata={"_run_version": i + 1},
            )
            ctx._results["up"] = conn.sql("SELECT 1 AS id")
            LocalTableSinkNode({"table_name": "capped", "mode": "append"}).execute(ctx)
    finally:
        conn.close()
    history = datastore.find_table_by_name("default", "default", "capped").load_history
    assert len(history) == LOAD_HISTORY_LIMIT
    assert history[0]["version"] == LOAD_HISTORY_LIMIT + 3, "newest first"
