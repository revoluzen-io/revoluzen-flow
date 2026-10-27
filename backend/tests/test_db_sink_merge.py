"""Database Sink â€” merge (upsert) write mode.

The sink could previously only create, append or truncate. That left no way
to express "update the rows I already have, insert the ones I don't", which
is what a dbt incremental model with a `unique_key` does, and what any
re-runnable load needs. Importing such a model as an append meant the second
run silently doubled the table.

These exercise the REAL dialect path (`_write_with_conn` against a live
SQLite connection), not a mock, because the thing worth testing is what ends
up in the table after two runs.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from revoluzen_flow.nodes.activities import (  # noqa: E402
    DbSinkNode, _as_column_list, _quote_ident, _validate_merge_columns,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def duck():
    import duckdb
    from revoluzen_flow.nodes.base import ExecutionContext
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _sink() -> DbSinkNode:
    return DbSinkNode({})


def _write(conn, rows, mode="merge", merge_on=("id",), columns=("id", "name", "amount")):
    _sink()._write_with_conn(
        conn, "t", list(columns), rows, mode, "?", "sqlite",
        types=None, merge_on=list(merge_on) if merge_on else None,
    )
    conn.commit()


def _rows(conn):
    return sorted(conn.execute('SELECT "id", "name", "amount" FROM "t"').fetchall())


class TestMergeUpserts:
    def test_first_merge_creates_the_table(self):
        # dbt builds an incremental model's table on its first run; a merge
        # that needed the table to exist already would not reproduce that.
        c = sqlite3.connect(":memory:")
        _write(c, [("1", "a", "10")])
        assert _rows(c) == [("1", "a", "10")]
        c.close()

    def test_second_run_updates_instead_of_duplicating(self, conn):
        # The whole point. Before merge existed this was an append, and the
        # table held two rows for id=1 after the second run.
        _write(conn, [("1", "a", "10"), ("2", "b", "20")])
        _write(conn, [("1", "a-updated", "11")])
        assert _rows(conn) == [("1", "a-updated", "11"), ("2", "b", "20")]

    def test_untouched_rows_survive(self, conn):
        _write(conn, [("1", "a", "10"), ("2", "b", "20"), ("3", "c", "30")])
        _write(conn, [("2", "b2", "22")])
        assert _rows(conn) == [("1", "a", "10"), ("2", "b2", "22"), ("3", "c", "30")]

    def test_new_keys_are_inserted(self, conn):
        _write(conn, [("1", "a", "10")])
        _write(conn, [("2", "b", "20")])
        assert _rows(conn) == [("1", "a", "10"), ("2", "b", "20")]

    def test_merge_is_idempotent(self, conn):
        # Running the same batch twice must leave the table identical â€”
        # this is what makes a merge-mode pipeline safe to schedule.
        batch = [("1", "a", "10"), ("2", "b", "20")]
        _write(conn, batch)
        first = _rows(conn)
        _write(conn, batch)
        assert _rows(conn) == first

    def test_composite_keys(self, conn):
        cols = ("tenant", "day", "total")
        _write(conn, [("t1", "d1", "5"), ("t1", "d2", "6")],
               merge_on=("tenant", "day"), columns=cols)
        _write(conn, [("t1", "d2", "99")], merge_on=("tenant", "day"), columns=cols)
        got = sorted(conn.execute('SELECT "tenant","day","total" FROM "t"').fetchall())
        assert got == [("t1", "d1", "5"), ("t1", "d2", "99")]

    def test_empty_batch_leaves_the_table_alone(self, conn):
        _write(conn, [("1", "a", "10")])
        _write(conn, [])
        assert _rows(conn) == [("1", "a", "10")]


class TestMergeRefusesWhatItCannotDo:
    def test_missing_merge_on_raises(self, conn):
        with pytest.raises(ValueError, match="merge_on"):
            _write(conn, [("1", "a", "10")], merge_on=None)

    def test_key_that_is_not_a_column_raises(self, conn):
        # A key that matches nothing makes every row look new, so the merge
        # quietly degrades into an append.
        with pytest.raises(ValueError, match="not columns"):
            _write(conn, [("1", "a", "10")], merge_on=("nope",))

    def test_duplicate_keys_within_one_batch_raise(self, conn):
        # Both rows survive the single DELETE and both get inserted, so the
        # "upsert" would leave a duplicate behind.
        with pytest.raises(ValueError, match="not unique in this batch"):
            _write(conn, [("1", "a", "10"), ("1", "b", "20")])

    def test_unknown_mode_raises_instead_of_writing_nothing(self, conn):
        # Regression: the mode chain had no else, so a typo wrote nothing at
        # all and the run still reported success.
        with pytest.raises(ValueError, match="unknown write mode"):
            _write(conn, [("1", "a", "10")], mode="upsert")


class TestInMemoryDuckDbTarget:
    """The no-connection path (`connection_id` empty) writes to the run's own
    DuckDB. It had branches for append and "everything else", so merge fell
    through to CREATE OR REPLACE and threw away every existing row — the
    exact opposite of an upsert. These go through `execute()`, so they also
    cover the param plumbing the dbt importer writes into.
    """

    def _run(self, ctx, rows, merge_on=("id",), mode="merge"):
        rel = ctx.conn.sql(
            "SELECT * FROM (VALUES " + ", ".join(
                f"({r[0]}, '{r[1]}')" for r in rows
            ) + ") AS t(id, name)"
        )
        ctx.set_result("up", rel)
        node = DbSinkNode({
            "table": "t", "mode": mode, "connection_id": "",
            "merge_on": list(merge_on) if merge_on else [],
            "_input_step_ids": ["up"],
        })
        node.execute(ctx)

    def _target(self, ctx):
        return sorted(ctx.conn.sql('SELECT id, name FROM "t"').fetchall())

    def test_merge_updates_rather_than_replacing_the_table(self, duck):
        self._run(duck, [(1, "a"), (2, "b")])
        self._run(duck, [(2, "b2")])
        # Before the merge branch existed this returned just [(2, 'b2')] —
        # the whole table had been replaced.
        assert self._target(duck) == [(1, "a"), (2, "b2")]

    def test_merge_inserts_new_keys(self, duck):
        self._run(duck, [(1, "a")])
        self._run(duck, [(2, "b")])
        assert self._target(duck) == [(1, "a"), (2, "b")]

    def test_merge_is_idempotent(self, duck):
        self._run(duck, [(1, "a"), (2, "b")])
        once = self._target(duck)
        self._run(duck, [(1, "a"), (2, "b")])
        assert self._target(duck) == once

    def test_merge_without_keys_raises(self, duck):
        with pytest.raises(ValueError, match="merge_on"):
            self._run(duck, [(1, "a")], merge_on=None)

    def test_merge_key_not_a_column_raises(self, duck):
        with pytest.raises(ValueError, match="not columns"):
            self._run(duck, [(1, "a")], merge_on=("nope",))


class TestMergeKeyParsing:
    @pytest.mark.parametrize("value,expected", [
        (["a", "b"], ["a", "b"]),
        ("a,b", ["a", "b"]),          # hand-edited JSON / API / dbt importer
        (" a , b ", ["a", "b"]),
        ("", []),
        (None, []),
        ([], []),
        (["a", "", "  "], ["a"]),
    ])
    def test_accepts_list_or_comma_string(self, value, expected):
        assert _as_column_list(value) == expected

    def test_validate_rejects_empty(self):
        with pytest.raises(ValueError, match="merge_on"):
            _validate_merge_columns([], ["a"])


class TestIdentifierQuoting:
    """MySQL reads "x" as a STRING, not an identifier, unless the session
    runs ANSI_QUOTES. The writer hard-coded `"` for every dialect, so every
    statement it built for MySQL was a syntax error — create, append and
    truncate included. The sink had never worked against MySQL, and nothing
    caught it because the tests only ever used SQLite.
    """

    def test_mysql_uses_backticks(self):
        assert _quote_ident("tbl", "mysql") == "`tbl`"

    @pytest.mark.parametrize("engine", ["sqlite", "postgresql", "mssql"])
    def test_other_dialects_use_double_quotes(self, engine):
        assert _quote_ident("tbl", engine) == '"tbl"'

    def test_embedded_quote_is_doubled_not_escaped_away(self):
        # A column name cannot be allowed to terminate its own quoting:
        # these names come from a DuckDB relation and from user config.
        assert _quote_ident('we"ird', "postgresql") == '"we""ird"'
        assert _quote_ident("we`ird", "mysql") == "`we``ird`"


class TestOtherModesStillWork:
    def test_create_replaces(self, conn):
        _write(conn, [("1", "a", "10")], mode="create", merge_on=None)
        _write(conn, [("2", "b", "20")], mode="create", merge_on=None)
        assert _rows(conn) == [("2", "b", "20")]

    def test_append_accumulates(self, conn):
        _write(conn, [("1", "a", "10")], mode="create", merge_on=None)
        _write(conn, [("1", "a", "10")], mode="append", merge_on=None)
        assert len(_rows(conn)) == 2

    def test_truncate_keeps_the_table_and_replaces_rows(self, conn):
        _write(conn, [("1", "a", "10")], mode="create", merge_on=None)
        _write(conn, [("2", "b", "20")], mode="truncate", merge_on=None)
        assert _rows(conn) == [("2", "b", "20")]
