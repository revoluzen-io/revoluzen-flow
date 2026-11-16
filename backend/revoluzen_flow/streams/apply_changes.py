"""Apply change rows to a database table by key (Database destination, 2026-11-13).

The Change Data Capture step (and a Kafka Source reading Debezium events)
gives rows with ``_op``: insert, update, delete, truncate or snapshot. The
Database destination's "Apply changes" mode makes the target table match:

* only the last change of each key counts, in the order the changes were made
  (``_seq``, else ``_lsn``, else ``_position``, else the order of the rows);
* an insert, update or snapshot row replaces the key's row (delete, then
  insert, as Merge does, so it needs no unique index and means the same on
  every database);
* a delete removes the key's row;
* a truncate empties the table; only changes after it count;
* columns PostgreSQL did not send because their large value did not change
  (``_unchanged``) are left as they are in the table;
* the change columns themselves (``_op``, ``_table``, ``_seq`` ...) are not
  written to the table.

One destination writes one table, so the input may hold one source table's
changes only; with several, put a Filter on ``_table`` before each destination.
Running the same changes twice leaves the same table, which is what lets the
sources deliver a change twice after a failure.
"""

from __future__ import annotations

from typing import Any, Callable

CHANGE_COLUMNS = frozenset({
    "_op", "_table", "_lsn", "_seq", "_position", "_committed_at", "_txid", "_unchanged",
    "_kafka_topic", "_kafka_partition", "_kafka_offset", "_kafka_key", "_kafka_headers", "_kafka_timestamp",
})
_UPSERT = {"insert", "update", "snapshot", "read", "c", "u", "r"}
_DELETE = {"delete", "d"}
_TRUNCATE = {"truncate", "t"}


def data_columns(columns: list[str]) -> list[str]:
    return [c for c in columns if c not in CHANGE_COLUMNS]


def apply_changes(
    cur: Any,
    quote: Callable[[str], str],
    qtable: str,
    columns: list[str],
    rows: list[tuple],
    keys: list[str],
    placeholder: str,
    ensure_table: Callable[[list[str]], None],
) -> dict:
    """Apply ``rows`` (with the change columns) to ``qtable``; returns counts."""
    if "_op" not in columns:
        raise ValueError(
            "DB Sink: Apply changes needs change rows, with an _op column (insert, update, "
            "delete). Put a Change Data Capture step, or a Kafka Source reading Debezium "
            "events, before this destination, or choose Merge."
        )
    if not keys:
        raise ValueError("DB Sink: Apply changes needs the key column(s) that identify a row (Merge Keys).")
    missing = [k for k in keys if k not in columns]
    if missing:
        raise ValueError(f"DB Sink: the key column(s) {missing} are not in this step's input.")
    index = {c: i for i, c in enumerate(columns)}
    if "_table" in index:
        tables = {r[index["_table"]] for r in rows if r[index["_table"]] is not None}
        if len(tables) > 1:
            raise ValueError(
                f"DB Sink: Apply changes writes one table, but the input holds changes for "
                f"{len(tables)} tables ({', '.join(sorted(map(str, tables))[:4])}). Add a Filter on "
                f"_table before this destination, one destination per table."
            )
    order = next((c for c in ("_seq", "_lsn", "_position") if c in index), None)
    numbered = list(enumerate(rows))
    if order is not None:
        numbered.sort(key=lambda item: (item[1][index[order]] is None, item[1][index[order]], item[0]))
    ops = [(str(r[index["_op"]] or "").lower(), r) for _n, r in numbered]

    # Only what follows the last truncate counts.
    last_truncate = max((i for i, (op, _r) in enumerate(ops) if op in _TRUNCATE), default=-1)
    emptied = last_truncate >= 0
    ops = ops[last_truncate + 1:]

    unknown = sorted({op for op, _r in ops if op not in _UPSERT | _DELETE | _TRUNCATE})
    if unknown:
        raise ValueError(f"DB Sink: Apply changes does not know the change kind(s) {unknown}.")

    final: dict[tuple, tuple[str, tuple]] = {}
    for op, row in ops:
        final[tuple(row[index[k]] for k in keys)] = (op, row)

    data = data_columns(columns)
    ensure_table(data)
    if emptied:
        cur.execute(f"DELETE FROM {qtable}")
    where = " AND ".join(f"{quote(k)} = {placeholder}" for k in keys)

    unchanged_at = index.get("_unchanged")
    replace: list[tuple] = []          # keys whose row is deleted (then maybe re-inserted)
    plain: list[tuple] = []            # whole rows to insert
    partial: list[tuple[tuple, list[str], tuple]] = []  # (key, columns sent, values)
    for key, (op, row) in final.items():
        skip = {
            name for name in str(row[unchanged_at] or "").split(",") if name
        } if unchanged_at is not None and op not in _DELETE else set()
        if skip:
            kept = [c for c in data if c not in skip]
            partial.append((key, kept, tuple(row[index[c]] for c in kept)))
            continue
        replace.append(key)
        if op not in _DELETE:
            plain.append(tuple(row[index[c]] for c in data))
    if replace:
        cur.executemany(f"DELETE FROM {qtable} WHERE {where}", replace)
    if plain:
        names = ", ".join(quote(c) for c in data)
        cur.executemany(
            f"INSERT INTO {qtable} ({names}) VALUES ({', '.join([placeholder] * len(data))})", plain,
        )
    # Rows PostgreSQL sent without some large values: update the row in place
    # so those values stay as the table holds them. Such a change is always an
    # update of a row that existed; if the table lacks it, it is inserted with
    # what was sent.
    for key, kept, values in partial:
        sets = ", ".join(f"{quote(c)} = {placeholder}" for c in kept)
        cur.execute(f"UPDATE {qtable} SET {sets} WHERE {where}", tuple(values) + tuple(key))
        if getattr(cur, "rowcount", 1) == 0:
            names = ", ".join(quote(c) for c in kept)
            cur.execute(f"INSERT INTO {qtable} ({names}) VALUES ({', '.join([placeholder] * len(kept))})", values)
    return {"deleted": sum(1 for op, _r in final.values() if op in _DELETE),
            "written": len(plain) + len(partial), "emptied": emptied}
