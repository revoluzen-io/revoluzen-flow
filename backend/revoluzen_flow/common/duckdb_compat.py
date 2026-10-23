"""DuckDB relation helpers that work on both 1.x and 2.x.

DuckDB 2.0 removed ``Relation.fetchone()``:

    AttributeError: Relations no longer have fetchone(): a relation keeps no
    result between calls, so every call runs it again.

That is a deliberate API change, and the reasoning is sound — a relation is a
lazy query, not a cursor holding a result set, so ``fetchone()`` silently
re-executed the whole query on each call.

It caught this project because ``duckdb>=1.2.0`` has no upper bound, so a
fresh install resolves 2.0 and 29 call sites stop working. The breakage is
not limited to the obvious ``rel.count(...)`` chains: ``conn.sql()`` returns
a **relation** too, so every ``conn.sql(...).fetchone()`` was affected, while
``conn.execute(...).fetchone()`` was always fine because that returns a
cursor.

``fetchall()`` exists on relations in both versions, so these helpers work
either side of the bump and the package keeps a wide version range rather
than pinning away from a major release.
"""

from __future__ import annotations

from typing import Any


def fetch_one(relation: Any) -> tuple | None:
    """First row of a relation, or None when there are no rows.

    The None-on-empty behaviour matters: it is what the old
    ``Relation.fetchone()`` did, and several callers test the result for
    truthiness. Returning an empty tuple or raising would change their
    control flow.

    ``limit(1)`` keeps this bounded. A bare ``fetchall()`` would materialise
    the whole result just to read its first row, which for a relation over a
    large table is the difference between one row and all of them.
    """
    rows = relation.limit(1).fetchall()
    return rows[0] if rows else None


def fetch_scalar(relation: Any, default: Any = None) -> Any:
    """First column of the first row — the `COUNT(*)` case.

    Most of the migrated call sites were ``...fetchone()[0]`` against an
    aggregate, which cannot return zero rows. They use this for the shorter
    read, and `default` covers the callers where the query is not an
    aggregate and could legitimately be empty.
    """
    row = fetch_one(relation)
    if row is None or len(row) == 0:
        return default
    return row[0]
