"""Run a managed table's declarative tests.

Moved out of ``api/storage.py`` (2026-11-11) so the same runner serves the
Run button and the table monitors (``table_monitor.py``). Each test becomes
one DuckDB probe over the table's Parquet parts; nothing is written.

Two fixes made in the move:

* **Freshness** compared the clock with the table's ``updated_at``, which
  also moves when someone renames the table or saves its tests, so saving a
  test made a stale table look fresh. It now reads the newest load
  (``load_history``), falling back to ``updated_at`` for tables written
  before load history existed.
* **Row-count drift** compared the rows on disk with ``row_count``, which
  the sink sets to those same rows on every load, so drift was always 0.
  It now compares with the load before the newest one.

Also reads every probe with ``fetchall``: a half-read ``fetchone`` result
leaves the DuckDB connection refusing the next query.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

VALID_TEST_TYPES = frozenset({
    "not_null", "unique", "accepted_values", "relationships", "freshness", "row_count_anomaly",
})


def _quote_lit(v: Any) -> str:
    """A Python value as a SQL literal. Refuses anything else, so a test
    can never become an injection."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise TypeError(f"unsupported accepted_values element type: {type(v).__name__}")


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError:
        return None
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_loaded_at(table: Any) -> datetime | None:
    """When data last landed in the table: the newest load, else updated_at."""
    history = list(getattr(table, "load_history", None) or [])
    if history:
        newest = _as_utc(history[0].get("at"))
        if newest is not None:
            return newest
    return _as_utc(getattr(table, "updated_at", None))


def previous_load_rows(table: Any) -> int | None:
    """Rows written by the load before the newest one, if known."""
    history = list(getattr(table, "load_history", None) or [])
    if len(history) >= 2 and history[1].get("rows") is not None:
        return int(history[1]["rows"])
    return None


def _scalar(conn: Any, sql: str) -> int:
    rows = conn.execute(sql).fetchall()
    return int(rows[0][0] or 0) if rows else 0


def run_tests_for_table(store: Any, table: Any, *, workspace_id: str, data_dir: str) -> dict[str, Any]:
    """Run every test configured on ``table``. Returns
    ``{ran_at, table_id, passed, failed, results}``; each result is
    ``{test_type, columns, ok, violations, severity, message?, ...}``."""
    import duckdb

    from revoluzen_flow.datastore.paths import safe_join_under

    tests = list(getattr(table, "tests", None) or [])
    ran_at = datetime.now(timezone.utc).isoformat()
    if not tests:
        return {"ran_at": ran_at, "table_id": table.id, "passed": 0, "failed": 0, "results": []}

    abs_dir = safe_join_under(data_dir, table.path)
    if not os.path.isdir(abs_dir):
        raise FileNotFoundError("table directory not found on disk")
    glob = os.path.join(abs_dir, "*.parquet").replace("'", "''")
    source = f"read_parquet('{glob}')"

    conn = duckdb.connect()
    results: list[dict[str, Any]] = []
    passed = failed = 0
    try:
        for t in tests:
            tt = (t.get("test_type") or "").strip()
            cols = list(t.get("columns") or [])
            entry: dict[str, Any] = {"test_type": tt, "columns": cols, "severity": t.get("severity", "fail")}
            try:
                if tt == "not_null":
                    if not cols:
                        raise ValueError("columns required")
                    where = " OR ".join(f"{_q(c)} IS NULL" for c in cols)
                    violations = _scalar(conn, f"SELECT COUNT(*) FROM {source} WHERE {where}")
                elif tt == "unique":
                    if not cols:
                        raise ValueError("columns required")
                    group = ", ".join(_q(c) for c in cols)
                    violations = _scalar(conn, (
                        f"SELECT COALESCE(SUM(cnt - 1), 0) FROM ("
                        f"SELECT COUNT(*) AS cnt FROM {source} GROUP BY {group} HAVING COUNT(*) > 1)"
                    ))
                elif tt == "accepted_values":
                    if len(cols) != 1:
                        raise ValueError("accepted_values expects exactly one column")
                    values = t.get("accepted_values") or []
                    if not values:
                        raise ValueError("accepted_values list cannot be empty")
                    rendered = ", ".join(_quote_lit(v) for v in values)
                    violations = _scalar(conn, (
                        f"SELECT COUNT(*) FROM {source} "
                        f"WHERE {_q(cols[0])} NOT IN ({rendered}) AND {_q(cols[0])} IS NOT NULL"
                    ))
                elif tt == "relationships":
                    if len(cols) != 1:
                        raise ValueError("relationships expects exactly one column")
                    to_tbl, to_col = t.get("to_table"), t.get("to_column")
                    if not (to_tbl and to_col):
                        raise ValueError("relationships requires to_table + to_column")
                    schema_target, _, name_target = str(to_tbl).partition(".")
                    if not schema_target or not name_target:
                        raise ValueError("to_table must be 'schema.name'")
                    target = store.find_table_by_name(workspace_id, schema_target, name_target)
                    if not target:
                        raise ValueError(f"target table {to_tbl} not found")
                    target_glob = os.path.join(safe_join_under(data_dir, target.path), "*.parquet").replace("'", "''")
                    violations = _scalar(conn, (
                        f"SELECT COUNT(*) FROM {source} a WHERE a.{_q(cols[0])} IS NOT NULL "
                        f"AND a.{_q(cols[0])} NOT IN (SELECT {_q(to_col)} FROM read_parquet('{target_glob}'))"
                    ))
                elif tt == "freshness":
                    max_age = int(t.get("max_age_minutes") or 0)
                    if max_age <= 0:
                        raise ValueError("freshness requires max_age_minutes > 0")
                    loaded = last_loaded_at(table)
                    if loaded is None:
                        violations = 1
                        entry["message"] = "no load time is recorded for this table"
                    else:
                        age = int((datetime.now(timezone.utc) - loaded).total_seconds() / 60)
                        entry["age_minutes"] = age
                        entry["threshold_minutes"] = max_age
                        entry["last_loaded_at"] = loaded.isoformat()
                        violations = 1 if age > max_age else 0
                        if violations:
                            entry["message"] = f"last loaded {age} minutes ago; allowed {max_age}"
                elif tt == "row_count_anomaly":
                    actual = _scalar(conn, f"SELECT COUNT(*) FROM {source}")
                    prior = previous_load_rows(table)
                    min_r, max_r, drift = t.get("expected_min_rows"), t.get("expected_max_rows"), t.get("drift_pct")
                    problems: list[str] = []
                    if min_r is not None and actual < int(min_r):
                        problems.append(f"{actual} rows < expected_min_rows={min_r}")
                    if max_r is not None and actual > int(max_r):
                        problems.append(f"{actual} rows > expected_max_rows={max_r}")
                    if drift is not None and prior:
                        pct = abs(actual - prior) / prior * 100.0
                        if pct > float(drift):
                            problems.append(f"row count shifted {pct:.1f}% from the previous load's {prior} (threshold {drift}%)")
                    entry["actual_rows"] = actual
                    entry["prior_rows"] = prior
                    violations = len(problems)
                    if problems:
                        entry["message"] = "; ".join(problems)
                else:
                    raise ValueError(f"unknown test_type '{tt}'")
                entry["violations"] = int(violations)
                entry["ok"] = violations == 0
            except Exception as exc:  # noqa: BLE001 — one broken test is reported, the rest still run
                entry["ok"] = False
                entry["violations"] = -1
                entry["message"] = f"{type(exc).__name__}: {exc}"[:200]
            if entry["ok"]:
                passed += 1
            else:
                failed += 1
            results.append(entry)
    finally:
        conn.close()

    return {"ran_at": ran_at, "table_id": table.id, "passed": passed, "failed": failed, "results": results}


__all__ = ["VALID_TEST_TYPES", "last_loaded_at", "previous_load_rows", "run_tests_for_table"]
