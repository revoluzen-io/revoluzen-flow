"""The Database destination's "Apply changes" mode (2026-11-13), on SQLite.

Change rows (``_op`` insert / update / delete / truncate / snapshot) become the
matching inserts, updates and deletes; the last change of each key wins; a
truncate empties the table; values PostgreSQL did not send stay as they are.
"""
from __future__ import annotations

import sqlite3

import pytest

from revoluzen_flow.streams.apply_changes import apply_changes, data_columns

COLUMNS = ["id", "name", "big", "_op", "_table", "_seq", "_unchanged"]


def _q(name: str) -> str:
    return '"' + name + '"'


def apply(conn, rows, columns=COLUMNS, keys=("id",)):
    cur = conn.cursor()

    def ensure(data):
        cur.execute(f"CREATE TABLE IF NOT EXISTS target ({', '.join(_q(c) + ' TEXT' for c in data)})")

    result = apply_changes(cur, _q, "target", list(columns), [tuple(r) for r in rows], list(keys), "?", ensure)
    conn.commit()
    return result


def table(conn):
    return conn.execute("SELECT id, name, big FROM target ORDER BY id").fetchall()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_inserts_updates_and_deletes_land_and_the_last_change_wins(conn):
    apply(conn, [
        ("1", "a", "x", "insert", "s.t", 0, ""),
        ("2", "b", "y", "insert", "s.t", 1, ""),
        ("1", "a2", "x", "update", "s.t", 2, ""),
        ("2", "b", "y", "delete", "s.t", 3, ""),
        ("3", "c", "z", "insert", "s.t", 4, ""),
    ])
    assert table(conn) == [("1", "a2", "x"), ("3", "c", "z")]
    assert [c[1] for c in conn.execute("PRAGMA table_info(target)")] == ["id", "name", "big"]


def test_order_comes_from_the_change_order_not_the_row_order(conn):
    apply(conn, [("1", "late", "x", "update", "s.t", 9, ""), ("1", "early", "x", "insert", "s.t", 1, "")])
    assert table(conn) == [("1", "late", "x")]


def test_applying_the_same_changes_twice_leaves_the_same_table(conn):
    changes = [("1", "a", "x", "insert", "s.t", 0, ""), ("2", "b", "y", "insert", "s.t", 1, ""),
               ("2", "b", "y", "delete", "s.t", 2, "")]
    apply(conn, changes)
    apply(conn, changes)
    assert table(conn) == [("1", "a", "x")]


def test_a_truncate_empties_the_table_and_only_later_changes_count(conn):
    apply(conn, [("1", "a", "x", "snapshot", "s.t", 0, ""), ("9", "z", "z", "snapshot", "s.t", 1, "")])
    apply(conn, [("2", "b", "y", "insert", "s.t", 0, ""), (None, None, None, "truncate", "s.t", 1, ""),
                 ("3", "c", "z", "insert", "s.t", 2, "")])
    assert table(conn) == [("3", "c", "z")]


def test_values_postgres_did_not_send_stay_as_they_are(conn):
    apply(conn, [("1", "a", "a very large value", "insert", "s.t", 0, "")])
    apply(conn, [("1", "renamed", None, "update", "s.t", 0, "big")])
    assert table(conn) == [("1", "renamed", "a very large value")]


def test_changes_of_several_tables_are_refused(conn):
    with pytest.raises(ValueError, match="writes one table"):
        apply(conn, [("1", "a", "x", "insert", "s.one", 0, ""), ("1", "a", "x", "insert", "s.two", 1, "")])


def test_rows_without_change_kinds_are_refused(conn):
    with pytest.raises(ValueError, match="needs change rows"):
        apply(conn, [("1", "a")], columns=["id", "name"])


def test_change_columns_are_not_written():
    assert data_columns(["id", "_op", "_lsn", "_kafka_offset", "_my_own"]) == ["id", "_my_own"]
