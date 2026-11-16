"""The shared pieces every streaming source uses (2026-11-13).

* ``streams/rows.py``: messages become a DuckDB table with real column types.
* ``SyncStateStore.save_if_unchanged`` + ``streams/positions.py``: a read
  position is saved after a successful run only if no other run, and no Reset,
  moved it while the run was reading. This is what keeps several Enterprise
  servers sharing one metastore from overwriting each other's positions.
* ``streams/holds.py`` + the executor: queue messages are confirmed only after
  the whole run succeeds, and given back after a failure or a Sample run.

No broker or database server is needed.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json

import duckdb
import pytest

from revoluzen_flow.engine import sync_state_store as store_module
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.engine.sync_state_store import SyncState
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow
from revoluzen_flow.nodes.base import BaseNode
from revoluzen_flow.streams import positions
from revoluzen_flow.streams.holds import HeldMessages, hold
from revoluzen_flow.streams.rows import RowsWriter, column_types, empty_relation, rows_to_relation


# ── Rows with real types ────────────────────────────────────────────────────

def test_each_column_gets_the_type_its_values_need():
    conn = duckdb.connect()
    rows = [
        {"id": 1, "price": decimal.Decimal("10.50"), "paid": True, "at": dt.datetime(2026, 11, 13, 9, 30, tzinfo=dt.timezone.utc),
         "day": dt.date(2026, 11, 13), "tags": ["a", "b"], "raw": b"\x00\x01", "note": "first"},
        {"id": 2, "price": decimal.Decimal("123456.789"), "paid": False, "at": None, "day": None,
         "tags": {"k": 1}, "raw": None, "note": None},
    ]
    rel = rows_to_relation(conn, rows)
    types = dict(zip(rel.columns, (str(t) for t in rel.types)))
    assert types["id"] == "BIGINT"
    assert types["price"] == "DECIMAL(9,3)"
    assert types["paid"] == "BOOLEAN"
    assert types["at"] == "TIMESTAMP"  # in UTC
    assert types["day"] == "DATE"
    assert types["raw"] == "BLOB"
    assert types["note"] == "VARCHAR"
    got = rel.fetchall()
    assert got[0][1] == decimal.Decimal("10.500") and got[1][1] == decimal.Decimal("123456.789")
    assert got[0][6] == b"\x00\x01"
    assert json.loads(got[0][5]) == ["a", "b"] and json.loads(got[1][5]) == {"k": 1}


def test_a_column_holding_several_kinds_of_value_becomes_text():
    conn = duckdb.connect()
    rel = rows_to_relation(conn, [{"v": 1}, {"v": "x"}, {"v": 2.5}])
    assert [str(t) for t in rel.types] == ["VARCHAR"]
    assert sorted(r[0] for r in rel.fetchall()) == ["1", "2.5", "x"]


def test_numbers_widen_instead_of_failing():
    assert column_types([{"n": 1}, {"n": 2.5}]) == {"n": "DOUBLE"}
    assert column_types([{"n": 1}, {"n": decimal.Decimal("2.25")}]) == {"n": "DECIMAL(3,2)"}
    assert column_types([{"n": 10 ** 30}]) == {"n": "VARCHAR"}


def test_fixed_types_win_and_text_from_a_database_is_read_into_them():
    conn = duckdb.connect()
    rel = rows_to_relation(conn, [{"amount": "10.50", "at": "2026-11-13 11:44:26.44006+00", "ok": "t"}],
                           fixed={"amount": "DECIMAL(10,2)", "at": "TIMESTAMPTZ", "ok": "BOOLEAN"})
    assert [str(t) for t in rel.types] == ["DECIMAL(10,2)", "TIMESTAMP", "BOOLEAN"]
    amount, at, ok = rel.fetchall()[0]
    assert amount == decimal.Decimal("10.50") and ok is True
    assert at == dt.datetime(2026, 11, 13, 11, 44, 26, 440060)  # UTC


def test_an_empty_read_keeps_the_columns_later_steps_use():
    conn = duckdb.connect()
    rel = rows_to_relation(conn, [], when_empty={"value": "VARCHAR", "_offset": "BIGINT"})
    assert rel.columns == ["value", "_offset"] and rel.fetchall() == []
    assert empty_relation(conn, {}).columns == ["value"]


def test_the_writer_leaves_no_file_behind(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    conn = duckdb.connect()
    writer = RowsWriter(conn, {"id": "BIGINT", "name": "VARCHAR"})
    for n in range(2500):
        writer.add({"id": n, "name": f"n{n}"})
    rel = writer.relation()
    assert rel.aggregate("count(*)").fetchall()[0][0] == 2500
    assert list(tmp_path.iterdir()) == []


def test_odd_column_names_survive():
    conn = duckdb.connect()
    rel = rows_to_relation(conn, [{"it's": 1, 'say "hi"': "x", "a.b": 2}])
    assert rel.columns == ["it's", 'say "hi"', "a.b"]


# ── Saving a position only if nobody moved it ──────────────────────────────

@pytest.fixture
def store(_revoluzen_flow_test_db, monkeypatch):
    monkeypatch.setattr(store_module.sync_state_store, "_db", _revoluzen_flow_test_db)
    return store_module.sync_state_store


def _state(cursor: str, column: str = "kafka:orders") -> SyncState:
    return SyncState(workflow_id="wf", step_id="src", cursor_column=column, last_cursor=cursor, rows_last_run=1)


def test_a_first_position_is_saved_only_while_there_is_none(store):
    assert store.save_if_unchanged(_state('{"0": 3}'), None) is True
    assert store.get("wf", "src").last_cursor == '{"0": 3}'
    # A second first run that started before the first one saved must not win.
    assert store.save_if_unchanged(_state('{"0": 2}'), None) is False
    assert store.get("wf", "src").last_cursor == '{"0": 3}'


def test_a_position_moves_only_from_the_one_the_run_started_at(store):
    store.upsert(_state('{"0": 3}'))
    started_at = store.get("wf", "src")
    store.upsert(_state('{"0": 7}'))  # another run (or server) saved meanwhile
    assert store.save_if_unchanged(_state('{"0": 5}'), started_at) is False
    assert store.get("wf", "src").last_cursor == '{"0": 7}'
    now = store.get("wf", "src")
    assert store.save_if_unchanged(_state('{"0": 9}'), now) is True
    assert store.get("wf", "src").last_cursor == '{"0": 9}'


def test_a_reset_during_a_run_is_not_undone_by_that_run(store):
    store.upsert(_state('{"0": 3}'))
    started_at = store.get("wf", "src")
    store.reset("wf", "src")
    assert store.save_if_unchanged(_state('{"0": 5}'), started_at) is False
    assert store.get("wf", "src") is None


def test_positions_keep_and_commit_check_the_starting_row(store):
    class Ctx:
        workflow_id = "wf"
        pending_sync_cursors: list = []

    ctx = Ctx()
    ctx.pending_sync_cursors = []
    saved = positions.load(ctx, "src", "kinesis:clicks")
    assert saved.value == {} and saved.row is None
    positions.keep(ctx, "src", saved, {"shard-1": "49590"}, 4, label="Kinesis Source")
    [pending] = ctx.pending_sync_cursors
    assert pending.commit() is True
    assert positions.load(ctx, "src", "kinesis:clicks").value == {"shard-1": "49590"}
    # A step whose stream changed starts as a first run, and still saves over the old row.
    other = positions.load(ctx, "src", "kinesis:other")
    assert other.value == {} and other.row is not None
    positions.keep(ctx, "src", other, {"shard-9": "1"}, 1)
    assert ctx.pending_sync_cursors[-1].commit() is True
    assert store.get("wf", "src").cursor_column == "kinesis:other"


def test_nothing_is_kept_outside_a_saved_pipeline():
    class Ctx:
        workflow_id = None
        pending_sync_cursors: list = []

    ctx = Ctx()
    positions.keep(ctx, "src", positions.Saved("k", {}, None), {"a": 1}, 1)
    assert ctx.pending_sync_cursors == []


# ── Queue messages: confirmed after success, given back otherwise ─────────

EVENTS: list[str] = []


class _HoldingSource(BaseNode):
    """A queue source that holds two messages until the run ends."""

    def execute(self, ctx):
        hold(ctx, HeldMessages("Test Queue", self.params.get("_step_id", ""), 2,
                               confirm=lambda: EVENTS.append("confirmed"),
                               give_back=lambda: EVENTS.append("given back"),
                               close=lambda: EVENTS.append("closed")))
        return ctx.conn.sql("SELECT 1 AS id UNION ALL SELECT 2")


@pytest.fixture
def holding_source(monkeypatch):
    """The JSON Source step type, played for these tests by a holding queue source."""
    from revoluzen_flow.nodes import registry as registry_module

    EVENTS.clear()
    registry_module.get_registry()
    monkeypatch.setitem(registry_module._REGISTRY, StepType.JSON_SOURCE, _HoldingSource)
    return StepType.JSON_SOURCE


def _queue_pipeline(step_type, tmp_path, fail: bool) -> Workflow:
    steps = [Step(id="q", type=step_type, params={})]
    conns = []
    last = "q"
    if fail:
        steps.append(Step(id="bad", type=StepType.FILTER, params={"condition": "no_such_column > 1"}))
        conns.append(StepConnection(from_step="q", to_step="bad"))
        last = "bad"
    steps.append(Step(id="out", type=StepType.DESTINATION,
                      params={"connector_type": "csv", "file_path": str(tmp_path / "out.csv")}))
    conns.append(StepConnection(from_step=last, to_step="out"))
    return Workflow(id="wf-q", name="q", steps=steps, connections=conns)


def test_queue_messages_are_confirmed_only_after_the_whole_run_succeeds(holding_source, tmp_path):
    result = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(_queue_pipeline(holding_source, tmp_path, False))
    assert result.status == "success"
    assert EVENTS == ["confirmed", "closed"]


def test_a_failed_run_gives_queue_messages_back(holding_source, tmp_path):
    result = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(_queue_pipeline(holding_source, tmp_path, True))
    assert result.status == "error"
    assert EVENTS == ["given back", "closed"]


def test_a_sample_run_gives_queue_messages_back(holding_source, tmp_path):
    result = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(
        _queue_pipeline(holding_source, tmp_path, False), preview_mode=True)
    assert result.status == "success"
    assert EVENTS == ["given back", "closed"]


def test_a_hold_with_no_run_is_given_back_at_once():
    EVENTS.clear()

    class Bare:
        pass

    hold(Bare(), HeldMessages("Q", "s", 1, confirm=lambda: EVENTS.append("confirmed"),
                              give_back=lambda: EVENTS.append("given back")))
    assert EVENTS == ["given back"]


def test_a_confirmation_that_fails_does_not_fail_the_run_and_closes(caplog):
    EVENTS.clear()

    def boom():
        raise RuntimeError("broker went away")

    held = HeldMessages("Q", "s", 3, confirm=boom, close=lambda: EVENTS.append("closed"))
    assert held.commit() is False
    assert EVENTS == ["closed"]
    assert "will deliver them again" in caplog.text
    held.release()  # a second call does nothing
    assert EVENTS == ["closed"]
