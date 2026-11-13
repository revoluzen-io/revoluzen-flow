"""Measure the facts an Execution Contract needs from a finished run.

Two guarantees cannot be judged from the run result alone: "one row per key"
needs the rows, and "the newest row is no older than" needs the newest value
of a time column. Until this module the executor never supplied either, so
both failed every run with "required fact not provided" however good the
data was. That is worse than not offering them.

Measured at the end of the run, on the run's own DuckDB connection, from the
relation of the step the guarantee measures. Post-run only: the duplicate
count is a full scan, which is why the pre-commit gate leaves it out (see
``precommit.py``). Everything here is best-effort; a fact that cannot be
measured is reported with the reason, and the verifier fails that clause
closed with that reason instead of a generic one.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from .schema import ExecutionContract, Guarantee, GuaranteeType

logger = logging.getLogger(__name__)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _measured_step(run_result: Any, g: Guarantee, contract: ExecutionContract) -> str | None:
    """The step whose output a guarantee measures, as the verifier picks it."""
    steps = getattr(run_result, "step_results", None) or {}
    sid = g.output_step or contract.default_output_step
    if sid and sid in steps:
        return sid
    return list(steps.keys())[-1] if steps else None


def _age_seconds(value: Any, now: datetime) -> float | None:
    """How old a timestamp is, in seconds. A time without a zone is read as
    UTC, the same assumption the run history makes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - moment).total_seconds())


def measure_contract_facts(
    contract: ExecutionContract,
    run_result: Any,
    results: dict[str, Any],
    conn: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Duplicate counts and column ages for the guarantees that need them.

    ``results`` maps a step id to the DuckDB relation it produced in this
    run. Returns ``{"duplicate_key_counts": {...}, "column_age_seconds":
    {...}, "unmeasured": {...}}``, with only the keys that have something.
    """
    now = now or datetime.now(timezone.utc)
    dup_counts: dict[str, int] = {}
    ages: dict[str, float] = {}
    unmeasured: dict[str, str] = {}

    for g in contract.guarantees:
        if g.type == GuaranteeType.NO_DUPLICATE_KEY and g.key:
            column, slot = g.key, f"no_duplicate_key:{g.key}"
        elif g.type == GuaranteeType.FRESHNESS_MAX_AGE and g.time_column:
            column, slot = g.time_column, f"freshness_max_age:{g.time_column}"
        else:
            continue
        step = _measured_step(run_result, g, contract)
        rel = results.get(step) if step else None
        if rel is None:
            unmeasured[slot] = f"could not read the output of step '{step or '?'}' to measure '{column}'"
            continue
        columns = list(getattr(rel, "columns", []) or [])
        if column not in columns:
            unmeasured[slot] = f"column '{column}' is not in the output of step '{step}'"
            continue
        view = f"__contract_fact_{len(dup_counts) + len(ages) + len(unmeasured)}"
        try:
            conn.register(view, rel)
            if g.type == GuaranteeType.NO_DUPLICATE_KEY:
                # Rows beyond the first for each key value. A row with no key
                # is not a duplicate of another row with no key.
                # fetchall, not fetchone: DuckDB keeps a half-read result
                # open, and the next register() on the connection refuses.
                rows = conn.execute(
                    f"SELECT COALESCE(SUM(n - 1), 0) FROM ("
                    f"SELECT COUNT(*) AS n FROM {view} WHERE {_quote(column)} IS NOT NULL "
                    f"GROUP BY {_quote(column)} HAVING COUNT(*) > 1)"
                ).fetchall()
                dup_counts[column] = int(rows[0][0] or 0) if rows else 0
            else:
                rows = conn.execute(f"SELECT MAX({_quote(column)}) FROM {view}").fetchall()
                newest = rows[0][0] if rows else None
                age = _age_seconds(newest, now)
                if age is None:
                    unmeasured[slot] = (
                        f"'{column}' has no value that reads as a date or time"
                        if newest is not None else f"'{column}' is empty in the output of step '{step}'"
                    )
                else:
                    ages[column] = age
        except Exception as exc:  # noqa: BLE001 — a fact we cannot measure is reported, never raised
            logger.warning("could not measure %s on step %s", slot, step, exc_info=True)
            unmeasured[slot] = f"could not measure '{column}': {exc}"
        finally:
            try:
                conn.unregister(view)
            except Exception:  # noqa: BLE001
                pass

    out: dict[str, Any] = {}
    if dup_counts:
        out["duplicate_key_counts"] = dup_counts
    if ages:
        out["column_age_seconds"] = ages
    if unmeasured:
        out["unmeasured"] = unmeasured
    return out


def destination_confirmed(blast_radius: list[Any]) -> bool | None:
    """Did every write that happened report what it wrote, unblocked?

    None when the run wrote nothing, so a contract asking for a confirmed
    destination on a run with no sink still fails, with the reason.
    """
    if not blast_radius:
        return None
    return all((not b.blocked) and b.rows_written is not None for b in blast_radius)


__all__ = ["destination_confirmed", "measure_contract_facts"]
