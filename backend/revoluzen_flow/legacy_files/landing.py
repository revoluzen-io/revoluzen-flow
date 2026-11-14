"""Landing decoded rows in DuckDB, a batch at a time.

The same pattern as the Avro reader: rows are gathered in batches, each batch
becomes an Arrow table with a fixed schema, and is appended to a DuckDB temp
table. Peak Python memory is one batch, and DuckDB's memory limit and spill to
disk govern the rest.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

BATCH_ROWS = 50_000


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def unique_names(names: Sequence[str], fallback: str = "column") -> list[str]:
    """Blank names become ``column_1``...; repeated names get ``_2``, ``_3``."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(names, start=1):
        name = (raw or "").strip() or f"{fallback}_{index}"
        key = name.lower()
        if key in seen:
            seen[key] += 1
            candidate = f"{name}_{seen[key]}"
            while candidate.lower() in seen:
                seen[key] += 1
                candidate = f"{name}_{seen[key]}"
            name = candidate
            key = name.lower()
        seen[key] = 1
        out.append(name)
    return out


class Lander:
    """Collects rows and appends them to one DuckDB temp table."""

    def __init__(self, conn, columns: Sequence[tuple[str, Any]], batch_rows: int = BATCH_ROWS):
        import pyarrow as pa

        self._pa = pa
        self._conn = conn
        self._names = [name for name, _ in columns]
        self._types = [arrow_type for _, arrow_type in columns]
        self._schema = pa.schema(list(zip(self._names, self._types)))
        self._batch_rows = batch_rows
        self._rows: list[Sequence[Any]] = []
        self._table = f"__legacy_{uuid.uuid4().hex[:16]}"
        self._created = False
        self.row_count = 0

    @property
    def total(self) -> int:
        """Rows added so far, landed or still in the current batch."""
        return self.row_count + len(self._rows)

    def add(self, row: Sequence[Any]) -> None:
        self._rows.append(row)
        if len(self._rows) >= self._batch_rows:
            self.flush()

    def flush(self) -> None:
        if not self._rows and self._created:
            return
        pa = self._pa
        columns = list(zip(*self._rows)) if self._rows else [() for _ in self._names]
        arrays = []
        for values, arrow_type, name in zip(columns, self._types, self._names):
            try:
                arrays.append(pa.array(list(values), type=arrow_type))
            except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
                raise ValueError(f"Column '{name}' has a value that does not fit {arrow_type}: {exc}") from None
        table = pa.Table.from_arrays(arrays, schema=self._schema)
        view = f"{self._table}_batch"
        self._conn.register(view, table)
        try:
            if not self._created:
                self._conn.execute(f"CREATE TEMP TABLE {quote_identifier(self._table)} AS SELECT * FROM {view}")
                self._created = True
            else:
                self._conn.execute(f"INSERT INTO {quote_identifier(self._table)} SELECT * FROM {view}")
        finally:
            self._conn.unregister(view)
        self.row_count += len(self._rows)
        self._rows = []

    def finish(self):
        self.flush()
        return self._conn.sql(f"SELECT * FROM {quote_identifier(self._table)}")
