"""Incremental Database Source: the saved cursor must be what the next run starts from.

With ``sync_mode: incremental`` and a ``watermark_column`` the source saves the
newest value it read, and the next run asks only for rows after it. These tests run
the real executor against a SQLite file through a saved connection, twice, with
the real cursor store, so what they pin is what a scheduled pipeline does.

They exist because the feature had no test at all. The save looked for
``__db_source`` while the source stages its rows under a name scoped to the step,
so the lookup failed quietly, no cursor was ever stored, and every run read every
row. ``test_shared_dataloss_sweep.py`` builds the unscoped table by hand and calls
the save directly, which is why it never noticed.

A saved cursor is only right when the rows it covers reached the destination:
a run that failed, and a Sample run (a capped read that writes nothing), must
leave it where it was. Otherwise the next real run skips rows that were never
loaded.
"""
from __future__ import annotations

import sqlite3

import pytest

from revoluzen_flow.connections.models import Connection
from revoluzen_flow.engine import sync_state_store as cursor_store
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

STEP = "s1"
WORKFLOW = "wf-incremental"


@pytest.fixture
def env(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch):
    """A SQLite source behind a saved connection, and the real cursor store."""
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(cursor_store.sync_state_store, "_db", _revoluzen_flow_test_db)

    db = tmp_path / "source.db"
    con = sqlite3.connect(db)
    # The newest row comes first, so a read of only the first row (a Sample) sees the
    # newest value and would carry a cursor past the two rows it never read.
    con.executescript(
        """
        CREATE TABLE orders (order_id INTEGER, note TEXT, updated_at TEXT);
        INSERT INTO orders VALUES (3, 'c', '2026-01-03'), (1, 'a', '2026-01-01'), (2, 'b', '2026-01-02');
        """
    )
    con.commit()
    con.close()
    connection_store.create(Connection(
        id="c_src", name="source db", type="sqlite", workspace_id="default", config={"file_path": str(db)},
    ))
    return type("Env", (), {"db": db, "out": tmp_path / "out.csv", "root": tmp_path})()


def add_rows(env, *rows: tuple) -> None:
    con = sqlite3.connect(env.db)
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


def pipeline(env, *, fail_after_read: bool = False, **source) -> Workflow:
    """Source -> (a step that fails) -> CSV file, the shape of the incremental templates."""
    params = {
        "connector_type": "database", "connection_id": "c_src", "query": "SELECT * FROM orders",
        "sync_mode": "incremental", "watermark_column": "updated_at", **source,
    }
    steps = [Step(id=STEP, type=StepType.SOURCE, params=params)]
    connections = []
    upstream = STEP
    if fail_after_read:
        steps.append(Step(id="s2", type=StepType.FILTER, params={"condition": "no_such_column > 1"}))
        connections.append(StepConnection(from_step=STEP, to_step="s2"))
        upstream = "s2"
    steps.append(Step(id="out", type=StepType.DESTINATION, params={"connector_type": "csv", "file_path": str(env.out)}))
    connections.append(StepConnection(from_step=upstream, to_step="out"))
    return Workflow(id=WORKFLOW, name="incremental", steps=steps, connections=connections)


def run(env, wf: Workflow, **kwargs):
    executor = WorkflowExecutor(data_dir=str(env.root), app_state={"data_dir": str(env.root)})
    return executor.execute_workflow(wf, **kwargs)


def stored_cursor() -> str | None:
    state = cursor_store.sync_state_store.get(WORKFLOW, STEP)
    return state.last_cursor if state else None


# ── The cursor is saved, and the next run starts after it ──────────────────

def test_the_first_run_reads_everything_and_saves_the_newest_value(env):
    result = run(env, pipeline(env))
    assert result.status == "success", {k: v.error for k, v in result.step_results.items()}
    assert result.step_results[STEP].row_count == 3
    assert stored_cursor() == "2026-01-03"


def test_the_next_run_reads_only_the_rows_after_the_saved_cursor(env):
    wf = pipeline(env)
    assert run(env, wf).step_results[STEP].row_count == 3

    add_rows(env, (4, "d", "2026-01-04"), (5, "e", "2026-01-05"))
    second = run(env, wf)

    assert second.status == "success"
    assert second.step_results[STEP].row_count == 2, "the second run read rows it had already loaded"
    assert stored_cursor() == "2026-01-05"


def test_a_run_with_nothing_new_keeps_the_cursor_and_the_next_one_still_finds_new_rows(env):
    wf = pipeline(env)
    run(env, wf)
    quiet = run(env, wf)
    assert quiet.status == "success"
    assert quiet.step_results[STEP].row_count == 0
    assert stored_cursor() == "2026-01-03"

    add_rows(env, (4, "d", "2026-01-04"))
    assert run(env, wf).step_results[STEP].row_count == 1


def test_a_cursor_typed_into_the_step_beats_the_saved_one(env):
    wf = pipeline(env)
    run(env, wf)  # saves 2026-01-03
    backfill = run(env, pipeline(env, watermark_value="2025-12-31"))
    assert backfill.step_results[STEP].row_count == 3


# ── A saved cursor must only cover rows that were loaded ───────────────────

def test_a_failed_run_does_not_move_the_cursor(env):
    failed = run(env, pipeline(env, fail_after_read=True))
    assert failed.status == "error"
    assert failed.step_results[STEP].row_count == 3
    assert stored_cursor() is None
    # So the rows that never reached the destination are read again.
    assert run(env, pipeline(env)).step_results[STEP].row_count == 3


def test_a_sample_run_does_not_move_the_cursor(env):
    """Sample reads a capped number of rows and writes nothing: it must not decide what is loaded."""
    sample = run(env, pipeline(env), preview_mode=True, sample_row_limit=1)
    assert sample.status == "success"
    assert sample.step_results[STEP].row_count == 1
    assert stored_cursor() is None

    real = run(env, pipeline(env))
    assert real.step_results[STEP].row_count == 3, "rows the sample never read were skipped"


def test_a_sample_run_after_a_real_one_leaves_the_cursor_where_it_was(env):
    wf = pipeline(env)
    run(env, wf)
    add_rows(env, (4, "d", "2026-01-04"), (5, "e", "2026-01-05"), (6, "f", "2026-01-06"))
    run(env, wf, preview_mode=True, sample_row_limit=1)
    assert stored_cursor() == "2026-01-03"
    assert run(env, wf).step_results[STEP].row_count == 3


def test_the_watermark_column_is_found_whatever_its_case(env):
    """Databases differ on the case of a column name (Oracle and Snowflake return it in capitals)."""
    run(env, pipeline(env, watermark_column="UPDATED_AT"))
    assert stored_cursor() == "2026-01-03"


def test_the_save_finds_the_staged_rows_by_their_per_step_name_when_it_is_not_handed_them():
    """The staging table carries the step's id; a caller that passes no relation must still find it."""
    import duckdb

    from revoluzen_flow.nodes.base import ExecutionContext
    from revoluzen_flow.nodes.db_source import DbSourceNode

    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(conn=conn, data_dir=".")
    ctx.workflow_id = WORKFLOW
    ctx.current_step_id = STEP
    conn.execute(f"CREATE TABLE {ctx.scoped_name('__db_source')} AS SELECT * FROM (VALUES (10), (30), (20)) t(updated_at)")

    DbSourceNode({"_step_id": STEP})._save_sync_cursor(ctx, "updated_at", rows_loaded=3)

    assert [state.last_cursor for state in ctx.pending_sync_cursors] == ["30"]


# ── A watermark column the result does not have ────────────────────────────

def test_a_watermark_column_the_query_does_not_return_saves_nothing_and_says_so(env, caplog):
    wf = pipeline(env, watermark_column="no_such_column")
    with caplog.at_level("WARNING"):
        result = run(env, wf)
    assert result.status == "success"
    assert result.step_results[STEP].row_count == 3
    assert stored_cursor() is None
    assert any("no_such_column" in r.getMessage() for r in caplog.records), "nothing told the user the column is missing"
