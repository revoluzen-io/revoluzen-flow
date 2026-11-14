"""Reconcile — check the data against the system of record before it lands.

A pipeline can keep every step's promise and still be wrong the way the
business means it: 2,480 orders reach the warehouse when the ERP has 2,500,
or revenue totals 1,234,000 against the ledger's 1,240,000. Row rules cannot
see that. It shows only when the result is compared with the place the data
came from, and usually someone does that by hand at month end. Reconcile
does the comparison inside the run, so a mismatch stops the write instead
of being found weeks later.

The reference is either another input wired into this step (any source that
reads the system of record), or a read-only query against a saved
connection. What it compares, each within ``tolerance_pct`` (0 = exact):

* the number of rows,
* the total of each column in ``sum_columns`` (same name on both sides;
  alias the column in the reference query when it differs),
* the keys in ``key_columns``: keys the reference has and the data lacks,
  and the other way round. Key checks are always exact.

``on_mismatch`` is ``fail`` (the run stops here, before anything after this
step such as a destination) or ``warn`` (the run carries on and the report
says what differed). ``output`` is ``input`` (the rows pass through
unchanged, so the step sits in front of a destination) or ``report`` (one
row per check).

An error message carries counts and totals only, never a row's values: run
errors are shown to the Copilot, which works from column names only.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb

from revoluzen_flow.ir.schema import StepType
from revoluzen_flow.nodes.base import BaseNode, ExecutionContext
from revoluzen_flow.nodes.registry import register

logger = logging.getLogger(__name__)

# One statement that only reads. The reference query runs against the
# customer's own system of record, so a typo must never become a write.
_READ_ONLY = re.compile(r"(?is)^\s*(select|with)\b")

_REPORT_COLUMNS = (
    "check_name VARCHAR, column_name VARCHAR, value DOUBLE, reference DOUBLE, "
    "difference DOUBLE, allowed DOUBLE, passed BOOLEAN, detail VARCHAR"
)


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _number(value: Any) -> Decimal:
    """A total as an exact number. No rows sum to NULL, which is 0 here."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Reconcile: a total came back as '{type(value).__name__}', not a number")


def _fmt(value: Decimal) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".") if value != value.to_integral() else f"{int(value):,}"


def _scalar(conn: Any, sql: str) -> Any:
    rows = conn.execute(sql).fetchall()
    return rows[0][0] if rows else None


_INTEGER_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
}


def _kind(duck_type: Any) -> str:
    """'int', 'number' or 'text', from a DuckDB column type."""
    name = str(duck_type).upper()
    if name in _INTEGER_TYPES:
        return "int"
    if name in ("FLOAT", "DOUBLE", "REAL") or name.startswith("DECIMAL"):
        return "number"
    return "text"


def _key_expr(column: str, kind_a: str, kind_b: str) -> str:
    """A key column as both sides can compare it: as written when both are
    whole numbers, as a number when both are numbers, else as text. Text
    alone would read 1 and 1.0 as different keys."""
    if kind_a == kind_b == "int":
        return _q(column)
    if kind_a in ("int", "number") and kind_b in ("int", "number"):
        return f"CAST({_q(column)} AS DOUBLE)"
    return f"CAST({_q(column)} AS VARCHAR)"


def _reference_from_rows(
    ctx: ExecutionContext, rows: list[dict], sum_columns: list[str], key_columns: list[str],
) -> str:
    """Load the reference query's rows into a temp table and return its name.

    A column of whole numbers is stored as BIGINT, of other numbers as
    DOUBLE (so both can be totalled), anything else as text. With no rows
    at all the reference's columns are unknown, so the ones the checks need
    are created empty, numeric for totals: "the reference has 0 rows" is
    then a finding, not an error about a missing column.
    """
    name = ctx.scoped_name("__reconcile_reference")
    columns: list[str] = []
    for row in rows:
        for k in row:
            if k not in columns:
                columns.append(k)
    if not rows:
        columns = list(dict.fromkeys([*sum_columns, *key_columns]))

    def is_number(v: Any) -> bool:
        return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)

    def is_whole(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    types: dict[str, str] = {}
    for c in columns:
        values = [r.get(c) for r in rows if r.get(c) is not None]
        if values and all(is_whole(v) for v in values):
            types[c] = "BIGINT"
        elif values and all(is_number(v) for v in values):
            types[c] = "DOUBLE"
        elif not values and c in sum_columns:
            types[c] = "DOUBLE"
        else:
            types[c] = "VARCHAR"
    defs = ", ".join(f"{_q(c)} {types[c]}" for c in columns)
    ctx.conn.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({defs})")

    def cell(c: str, v: Any) -> Any:
        if v is None:
            return None
        if types[c] == "BIGINT":
            return int(v)
        if types[c] == "DOUBLE":
            return float(v)
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        return str(v)

    if rows:
        placeholders = ", ".join("?" for _ in columns)
        ctx.conn.executemany(
            f"INSERT INTO {name} VALUES ({placeholders})",
            [tuple(cell(c, r.get(c)) for c in columns) for r in rows],
        )
    return name


@register(StepType.RECONCILE)
class ReconcileNode(BaseNode):
    """Compare the data with a system of record; stop or warn on a mismatch."""

    display_name = "Reconcile"
    category = "transform"
    description = (
        "Compare the data with the system of record — row count, totals and keys — "
        "and stop before the write when they disagree"
    )

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        data, reference = self._sides(ctx)
        compare_rows = bool(self.params.get("compare_row_count", True))
        sum_columns = [c for c in (self.params.get("sum_columns") or []) if str(c).strip()]
        key_columns = [c for c in (self.params.get("key_columns") or []) if str(c).strip()]
        if not (compare_rows or sum_columns or key_columns):
            raise ValueError(
                "Reconcile: choose at least one thing to compare — the row count, "
                "a column to total, or the key columns."
            )
        try:
            tolerance = float(self.params.get("tolerance_pct", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError("Reconcile: 'tolerance_pct' must be a number.")
        if tolerance < 0:
            raise ValueError("Reconcile: 'tolerance_pct' cannot be negative.")
        on_mismatch = (self.params.get("on_mismatch") or "fail").lower()
        if on_mismatch not in ("fail", "warn"):
            raise ValueError(f"Reconcile: invalid on_mismatch '{on_mismatch}'. Allowed: fail, warn.")
        output = (self.params.get("output") or "input").lower()
        if output not in ("input", "report"):
            raise ValueError(f"Reconcile: invalid output '{output}'. Allowed: input, report.")

        d = ctx.register_scoped("__reconcile_data", data)
        if isinstance(reference, str):
            r = reference
        else:
            r = ctx.register_scoped("__reconcile_ref", reference)

        have_data = set(data.columns)
        ref_shape = ctx.conn.sql(f"SELECT * FROM {r} LIMIT 0")
        have_ref = set(ref_shape.columns)
        data_kinds = dict(zip(data.columns, (_kind(t) for t in data.types)))
        ref_kinds = dict(zip(ref_shape.columns, (_kind(t) for t in ref_shape.types)))
        for col in [*sum_columns, *key_columns]:
            if col not in have_data:
                raise ValueError(f"Reconcile: column '{col}' is not in the data being checked.")
            if col not in have_ref:
                raise ValueError(
                    f"Reconcile: column '{col}' is not in the reference. Name it the same "
                    f"there, for example with AS {col} in the reference query."
                )

        checks: list[tuple] = []  # rows of the report, in _REPORT_COLUMNS order
        problems: list[str] = []
        allowed_share = Decimal(str(tolerance)) / Decimal(100)

        def compare(name: str, column: str | None, raw_value: Any, raw_ref: Any, label: str) -> None:
            value, ref = _number(raw_value), _number(raw_ref)
            allowed = abs(ref) * allowed_share
            difference = value - ref
            # A total of floating-point numbers depends on the order they were
            # added in, and DuckDB adds in parallel, so two sums of the same
            # numbers can differ in the last digits. Only exact types (whole
            # numbers, decimals) are held to exact equality.
            slack = Decimal(0)
            if isinstance(raw_value, float) or isinstance(raw_ref, float):
                slack = max(abs(value), abs(ref), Decimal(1)) * Decimal("1e-9")
            passed = abs(difference) <= allowed + slack
            detail = f"{label}: {_fmt(value)} against {_fmt(ref)} in the reference"
            if not passed:
                problems.append(
                    f"{label} {_fmt(value)} vs {_fmt(ref)}"
                    + (f" (allowed ±{_fmt(allowed)})" if allowed else "")
                )
            checks.append((name, column, float(value), float(ref), float(difference), float(allowed), passed, detail))

        if compare_rows:
            compare(
                "row_count", None,
                _scalar(ctx.conn, f"SELECT COUNT(*) FROM {d}"),
                _scalar(ctx.conn, f"SELECT COUNT(*) FROM {r}"),
                "row count",
            )
        for col in sum_columns:
            try:
                value = _scalar(ctx.conn, f"SELECT SUM({_q(col)}) FROM {d}")
                ref = _scalar(ctx.conn, f"SELECT SUM({_q(col)}) FROM {r}")
            except Exception as exc:  # noqa: BLE001 — a text column cannot be totalled
                raise ValueError(f"Reconcile: column '{col}' cannot be totalled: {exc}") from exc
            compare("sum", col, value, ref, f"total of {col}")

        if key_columns:
            keys_d = ", ".join(_key_expr(c, data_kinds.get(c, "text"), ref_kinds.get(c, "text")) for c in key_columns)
            missing = int(_scalar(ctx.conn, (
                f"SELECT COUNT(*) FROM (SELECT DISTINCT {keys_d} FROM {r} "
                f"EXCEPT SELECT DISTINCT {keys_d} FROM {d})"
            )) or 0)
            extra = int(_scalar(ctx.conn, (
                f"SELECT COUNT(*) FROM (SELECT DISTINCT {keys_d} FROM {d} "
                f"EXCEPT SELECT DISTINCT {keys_d} FROM {r})"
            )) or 0)
            key_label = ", ".join(key_columns)
            for name, count, says in (
                ("keys_missing", missing, "in the reference but not in the data"),
                ("keys_extra", extra, "in the data but not in the reference"),
            ):
                passed = count == 0
                if not passed:
                    problems.append(f"{count:,} {key_label} key{'s' if count != 1 else ''} {says}")
                checks.append((
                    name, key_label, float(count), 0.0, float(count), 0.0, passed,
                    f"{count:,} key{'s' if count != 1 else ''} {says}",
                ))

        if problems:
            message = "Reconcile: the data does not match the reference: " + "; ".join(problems) + "."
            if on_mismatch == "fail":
                raise ValueError(message)
            logger.warning("%s (on_mismatch=warn, carrying on)", message)

        if output == "input":
            return data
        report = ctx.scoped_name("__reconcile_report")
        ctx.conn.execute(f"CREATE OR REPLACE TEMP TABLE {report} ({_REPORT_COLUMNS})")
        ctx.conn.executemany(f"INSERT INTO {report} VALUES (?, ?, ?, ?, ?, ?, ?, ?)", checks)
        return ctx.conn.sql(f"SELECT * FROM {report}")

    # ── The two sides ────────────────────────────────────────────────────

    def _sides(self, ctx: ExecutionContext):
        """(data, reference). The reference is a relation, or the name of a
        temp table holding the reference query's rows."""
        ids = list(self.params.get("_input_step_ids", []) or [])
        ports = self.params.get("_input_step_ports")
        mode = (self.params.get("reference") or "input").lower()

        def one(sid: str):
            got = ctx.get_routed_inputs([sid], ports)
            return got[0] if got else None

        if mode == "query":
            if not ids:
                raise ValueError("Reconcile: needs an input — the data to check.")
            data = one(ids[0])
            if data is None:
                raise ValueError("Reconcile: needs an input — the data to check.")
            return data, self._query_reference(ctx)
        if mode != "input":
            raise ValueError(f"Reconcile: invalid reference '{mode}'. Allowed: input, query.")

        pinned = (self.params.get("reference_step") or "").strip()
        if pinned and pinned in ids:
            ref_id = pinned
        elif len(ids) >= 2:
            ref_id = ids[1]
        else:
            raise ValueError(
                "Reconcile: wire the system of record into this step as a second input, "
                "or set Reference to a query on a connection."
            )
        data_id = next((s for s in ids if s != ref_id), None)
        data = one(data_id) if data_id else None
        reference = one(ref_id)
        if data is None or reference is None:
            raise ValueError("Reconcile: one of its inputs produced nothing to compare.")
        return data, reference

    def _query_reference(self, ctx: ExecutionContext) -> str:
        connection_id = (self.params.get("connection_id") or "").strip()
        sql = (self.params.get("query") or "").strip().rstrip(";").strip()
        if not connection_id or not sql:
            raise ValueError("Reconcile: a reference query needs a connection and a SELECT.")
        if ";" in sql or not _READ_ONLY.match(sql):
            raise ValueError("Reconcile: the reference query must be one SELECT (or WITH) statement.")
        from revoluzen_flow.nodes.control_extras import _run_connection_sql

        rows, _ = _run_connection_sql(connection_id, sql)
        return _reference_from_rows(
            ctx, rows,
            [c for c in (self.params.get("sum_columns") or []) if str(c).strip()],
            [c for c in (self.params.get("key_columns") or []) if str(c).strip()],
        )

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "reference": "input",
            "reference_step": "",
            "connection_id": "",
            "query": "",
            "compare_row_count": True,
            "sum_columns": [],
            "key_columns": [],
            "tolerance_pct": 0,
            "on_mismatch": "fail",
            "output": "input",
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "reference", "type": "select", "label": "Reference", "default": "input",
             "options": [
                 {"value": "input", "label": "Another input",
                  "description": "Wire a source that reads the system of record into this step."},
                 {"value": "query", "label": "A query on a connection",
                  "description": "Run one read-only SELECT against a saved connection."},
             ],
             "description": "Where the numbers to match come from: the system of record."},
            # `tier: required` puts these in view whenever they apply; they are
            # not marked `required`, because each is needed in one mode only.
            {"name": "reference_step", "type": "upstream_step", "label": "Reference input",
             "show_when": {"reference": "input"}, "tier": "required",
             "description": "Which wired input is the system of record. Empty = the second input."},
            {"name": "connection_id", "type": "connection", "label": "Connection",
             "show_when": {"reference": "query"}, "tier": "required",
             "description": "The system of record to query."},
            {"name": "query", "type": "sql", "label": "Reference query", "rows": 5,
             "show_when": {"reference": "query"}, "tier": "required",
             "placeholder": "SELECT order_id, amount FROM erp.orders WHERE order_date = CURRENT_DATE - 1",
             "description": "One read-only SELECT. Name columns as in the data (AS ...) so they can be compared."},
            {"name": "compare_row_count", "type": "boolean", "label": "Compare the row count", "default": True,
             "description": "The data and the reference must have the same number of rows."},
            {"name": "sum_columns", "type": "column_list", "label": "Columns to total", "default": [],
             "description": "Each column's total must match the reference's total of the same column."},
            {"name": "key_columns", "type": "column_list", "label": "Key columns", "default": [],
             "description": "Every key in the reference must be in the data, and no other keys. Always exact."},
            {"name": "tolerance_pct", "type": "number", "label": "Tolerance (%)", "default": 0, "min": 0,
             "description": "How far the row count and totals may differ from the reference, as a percentage. 0 = exactly equal."},
            {"name": "on_mismatch", "type": "select", "label": "On mismatch", "default": "fail",
             "options": ["fail", "warn"],
             "description": "fail = stop the run here, before anything after this step such as a destination. warn = carry on."},
            {"name": "output", "type": "select", "label": "Output", "default": "input",
             "options": ["input", "report"],
             "description": "input = the data, unchanged. report = one row per check: what was compared, both values, and whether it matched."},
        ]
