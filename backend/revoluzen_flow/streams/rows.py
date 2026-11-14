"""Rows read from a stream, as a DuckDB table with real column types.

Messages and change events arrive as Python values: whole numbers, decimals,
times, nested JSON, text. ``rows_to_relation`` picks one DuckDB type per column
from the values it sees (a column holding several kinds of value becomes text),
unless the caller already knows the types. ``RowsWriter`` writes the rows to a
temporary JSON-lines file as they come, then has DuckDB read that file with
those exact types into a temporary table. Reading JSON is DuckDB's fast path,
nothing holds every row in memory, and the file is deleted afterwards.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import os
import tempfile
import uuid
from typing import Any, Iterable

_ORDER = ("BOOLEAN", "BIGINT", "DOUBLE")
_INTEGERS = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT")
_FLOATS = ("FLOAT", "REAL", "DOUBLE")


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT" if -(2 ** 63) <= value < 2 ** 63 else "VARCHAR"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, decimal.Decimal):
        return "DECIMAL"
    if isinstance(value, _dt.datetime):
        return "TIMESTAMPTZ" if value.tzinfo is not None else "TIMESTAMP"
    if isinstance(value, _dt.date):
        return "DATE"
    if isinstance(value, _dt.time):
        return "TIME"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "BLOB"
    if isinstance(value, (dict, list, tuple)):
        return "JSON"
    return "VARCHAR"


def _decimal_type(values: list[decimal.Decimal]) -> str:
    """DECIMAL(p, s) wide enough for every value, or DOUBLE past 38 digits."""
    scale = 0
    whole = 1
    for v in values:
        if not v.is_finite():
            return "DOUBLE"
        _sign, digits, exponent = v.as_tuple()
        frac = max(0, -int(exponent))
        scale = max(scale, frac)
        whole = max(whole, len(digits) - frac if len(digits) > frac else 1, int(exponent) + len(digits))
    precision = whole + scale
    if precision > 38:
        return "DOUBLE"
    return f"DECIMAL({max(precision, 1)},{scale})"


def column_types(rows: list[dict], fixed: dict[str, str] | None = None) -> dict[str, str]:
    """``{column: DuckDB type}`` in first-seen column order; ``fixed`` wins."""
    fixed = dict(fixed or {})
    seen: dict[str, set[str]] = {}
    decimals: dict[str, list[decimal.Decimal]] = {}
    for row in rows:
        for key, value in row.items():
            kinds = seen.setdefault(key, set())
            if value is None or key in fixed:
                continue
            kind = _value_type(value)
            kinds.add(kind)
            if kind == "DECIMAL":
                decimals.setdefault(key, []).append(value)
    out: dict[str, str] = {}
    for key, kinds in seen.items():
        if key in fixed:
            out[key] = fixed[key]
        elif not kinds:
            out[key] = "VARCHAR"
        elif len(kinds) == 1:
            kind = next(iter(kinds))
            out[key] = _decimal_type(decimals[key]) if kind == "DECIMAL" else kind
        elif kinds <= set(_ORDER):
            out[key] = max(kinds, key=_ORDER.index)
        elif kinds <= {"BIGINT", "DECIMAL"}:
            out[key] = _decimal_type(decimals[key] + [decimal.Decimal(0)])
        elif kinds <= {"TIMESTAMP", "TIMESTAMPTZ"}:
            out[key] = "TIMESTAMPTZ"
        else:
            out[key] = "VARCHAR"
    for key, kind in fixed.items():
        out.setdefault(key, kind)
    return out


def _json_value(value: Any, kind: str) -> Any:
    """The value as JSON text DuckDB reads back as ``kind``."""
    if value is None:
        return None
    base = kind.split("(")[0]
    if base in ("VARCHAR", "JSON"):
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            return value.isoformat()
        return str(value)
    if base == "BLOB":
        if isinstance(value, str):
            value = value.encode("utf-8")
        return base64.b64encode(bytes(value)).decode("ascii")
    if base == "BOOLEAN":
        if isinstance(value, str):
            return value.strip().lower() in ("t", "true", "1", "yes", "y")
        return bool(value)
    if base in _INTEGERS:
        return int(value)
    if base in _FLOATS:
        if isinstance(value, str):
            return value  # 'NaN', 'Infinity': DuckDB reads them from text
        f = float(value)
        return f if f == f and f not in (float("inf"), float("-inf")) else str(f)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    return str(value)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _key(name: str) -> str:
    """A column name as a key of DuckDB's ``columns={...}`` struct."""
    return "'" + str(name).replace("'", "''") + "'"


def _read_type(kind: str) -> str:
    return "VARCHAR" if kind in ("JSON", "BLOB") else kind


def empty_relation(conn: Any, types: dict[str, str]) -> Any:
    """A relation with these columns and no rows."""
    if not types:
        types = {"value": "VARCHAR"}
    cols = ", ".join(f"NULL::{_read_type(kind) if kind != 'BLOB' else 'BLOB'} AS {_quote(name)}"
                     for name, kind in types.items())
    return conn.sql(f"SELECT {cols} WHERE false")


class RowsWriter:
    """Collect rows of known column types on disk, then make one DuckDB table."""

    def __init__(self, conn: Any, types: dict[str, str], name: str = "__stream_rows") -> None:
        self.conn = conn
        self.types = dict(types)
        self.name = name
        self.count = 0
        fd, self.path = tempfile.mkstemp(suffix=".jsonl", prefix="flow_stream_")
        self._fh = os.fdopen(fd, "w", encoding="utf-8")

    def add(self, row: dict) -> None:
        self._fh.write(json.dumps({k: _json_value(row.get(k), t) for k, t in self.types.items()}))
        self._fh.write("\n")
        self.count += 1

    def relation(self) -> Any:
        """The table; the file is gone afterwards."""
        try:
            self._fh.close()
            if not self.count:
                return empty_relation(self.conn, self.types)
            table = f"{self.name}_{uuid.uuid4().hex[:8]}"
            spec = ", ".join(f"{_key(k)}: '{_read_type(v)}'" for k, v in self.types.items())
            select = ", ".join(
                f"from_base64({_quote(k)}) AS {_quote(k)}" if v == "BLOB" else _quote(k)
                for k, v in self.types.items()
            )
            literal = self.path.replace("'", "''")
            self.conn.execute(
                f"CREATE OR REPLACE TEMP TABLE {table} AS SELECT {select} FROM read_json('{literal}', "
                f"format='newline_delimited', columns={{{spec}}})"
            )
            return self.conn.sql(f"SELECT * FROM {table}")
        finally:
            self.discard()

    def discard(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass


def rows_to_relation(
    conn: Any,
    rows: Iterable[dict],
    name: str = "__stream_rows",
    fixed: dict[str, str] | None = None,
    when_empty: dict[str, str] | None = None,
) -> Any:
    """The rows as a DuckDB relation; ``fixed`` pins some columns' types.

    ``when_empty`` gives the columns of a run that read nothing, so the steps
    after it still find the columns they use.
    """
    rows = list(rows)
    if not rows:
        return empty_relation(conn, {**(when_empty or {}), **(fixed or {})})
    writer = RowsWriter(conn, column_types(rows, fixed), name)
    try:
        for row in rows:
            writer.add(row)
    except BaseException:
        writer.discard()
        raise
    return writer.relation()


def relation_types(relation: Any) -> dict[str, str]:
    """``{column: type}`` of a relation, for remembering a run's columns."""
    return {str(c): str(t) for c, t in zip(relation.columns, relation.types)}
