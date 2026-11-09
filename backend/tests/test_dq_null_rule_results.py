"""A rule that evaluates to NULL is a failed row, in every Data Quality mode.

SQL has a third truth value. ``"amount" BETWEEN 10 AND 100`` on a NULL amount is
neither true nor false, it is NULL, and the modes read that differently:

  drop / split   ``WHERE <rules>``            dropped it (NULL is not true)
  reject         ``CASE WHEN <rules> ...``    sent it to Reject
  tag            ``<rules> AS __dq_passed``   left __dq_passed NULL
  fail           ``WHERE NOT (<rules>)``      did not count it, so the run passed
  split          the failures list is ``WHERE NOT (<rules>)`` too, so the row was
                 in neither the passing rows nor the failures

A row the rules cannot confirm is not a row they passed. These tests pin one
answer for all five modes: it failed, whichever rule gave NULL (``between``,
``gt``, ``eq``, ``regex``, ...), and whichever way the rules are combined.

``not_null`` and ``is_null`` never give NULL, so they are untouched: a NULL value
still passes ``is_null``.
"""
from __future__ import annotations

from typing import Any

import duckdb
import pytest

from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.quality import DataQualityNode

MODES = ["drop", "fail", "tag", "reject", "split"]

# (op, rule value, a value that passes it, a value that fails it). Every op that
# compares the column: each of them gives NULL for a NULL column.
OPS = [
    ("eq", 50, 50, 5),
    ("ne", 5, 50, 5),
    ("gt", 10, 50, 5),
    ("gte", 10, 50, 5),
    ("lt", 10, 5, 50),
    ("lte", 10, 5, 50),
    ("between", [10, 100], 50, 5),
    ("in", [50, 60], 50, 5),
    ("not_in", [5, 6], 50, 5),
    ("regex", "^a", "abc", "xyz"),
    ("min_length", 2, "abc", "a"),
    ("max_length", 2, "ab", "abc"),
]


def _sql(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _run(rows: list[tuple], **params):
    """Run the node over (id, v) rows. Always include one row with a value, so
    DuckDB can type the column when the other row is NULL."""
    conn = duckdb.connect(":memory:")
    values = ", ".join(f"({i}, {_sql(v)})" for i, v in rows)
    ctx = ExecutionContext(conn=conn)
    ctx.set_result("src", conn.sql(f"SELECT * FROM (VALUES {values}) AS t(id, v)"))
    node = DataQualityNode(params={"_input_step_ids": ["src"], "_step_id": "dq", **params})
    rel = node.execute(ctx)
    return ctx, rel.columns, rel.fetchall()


def _verdict(mode: str, rule: dict, good: Any, other: Any) -> str:
    """How `mode` reports row 2 (holding `other`) next to a good row 1: 'passed',
    'failed', or 'undecided ...' when the mode gives no answer for it."""
    rows = [(1, good), (2, other)]
    if mode == "fail":
        try:
            _run(rows, mode=mode, rules=[rule])
        except ValueError:
            return "failed"
        return "passed"
    _, cols, out = _run(rows, mode=mode, rules=[rule])
    by_id = {r[0]: r for r in out}
    if mode in ("drop", "split"):
        return "passed" if 2 in by_id else "failed"
    if mode == "tag":
        flag = by_id[2][cols.index("__dq_passed")]
        return {True: "passed", False: "failed"}.get(flag, f"undecided (__dq_passed is {flag!r})")
    if mode == "reject":
        return "failed" if by_id[2][cols.index("_split_output")] == "reject" else "passed"
    raise AssertionError(f"unhandled mode {mode!r}")


# ── Every mode, every op that can give NULL ────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("op,value,good,bad", OPS, ids=[o[0] for o in OPS])
def test_a_null_rule_result_fails_the_row_in_every_mode(op, value, good, bad, mode):
    rule = {"column": "v", "op": op, "value": value}
    # The rule does tell good from bad, so the checks below mean something.
    assert _verdict(mode, rule, good, good) == "passed", f"{mode}/{op}: a good value was failed"
    assert _verdict(mode, rule, good, bad) == "failed", f"{mode}/{op}: a bad value was passed"
    # A NULL gives the rule no answer. It reads as the bad value does.
    assert _verdict(mode, rule, good, None) == "failed", f"{mode}/{op}: a NULL value"


# ── The pieces of the five modes ───────────────────────────────────────────

BETWEEN = {"column": "v", "op": "between", "value": [10, 100]}


def test_fail_mode_counts_the_null_row():
    """The run stops, and the message counts the NULL row with the bad one."""
    with pytest.raises(ValueError, match=r"2 row\(s\) failed validation"):
        _run([(1, 50), (2, 5), (3, None)], mode="fail", rules=[BETWEEN])
    with pytest.raises(ValueError, match=r"1 row\(s\) failed validation"):
        _run([(1, 50), (3, None)], mode="fail", rules=[BETWEEN])


def test_fail_mode_still_passes_when_every_row_passes():
    _, _, out = _run([(1, 50), (2, 99)], mode="fail", rules=[BETWEEN])
    assert sorted(r[0] for r in out) == [1, 2]


def test_tag_mode_never_leaves_dq_passed_empty():
    _, cols, out = _run([(1, 50), (2, 5), (3, None)], mode="tag", rules=[BETWEEN])
    flags = {r[0]: r[cols.index("__dq_passed")] for r in out}
    assert flags == {1: True, 2: False, 3: False}


def test_reject_mode_sends_the_null_row_to_reject():
    _, cols, out = _run([(1, 50), (2, 5), (3, None)], mode="reject", rules=[BETWEEN])
    where = {r[0]: r[cols.index("_split_output")] for r in out}
    assert where == {1: "pass", 2: "reject", 3: "reject"}


def test_drop_mode_drops_the_null_row():
    _, _, out = _run([(1, 50), (2, 5), (3, None)], mode="drop", rules=[BETWEEN])
    assert [r[0] for r in out] == [1]


def test_split_mode_puts_the_null_row_with_the_failures():
    """Passing rows and failures partition the input: no row in neither."""
    ctx, _, out = _run([(1, 50), (2, 5), (3, None)], mode="split", rules=[BETWEEN])
    failures = ctx.vars["_dq_failures_dq"].fetchall()
    assert sorted(r[0] for r in out) == [1]
    assert sorted(r[0] for r in failures) == [2, 3]


# ── Combining rules ────────────────────────────────────────────────────────

NOT_NULL_ID = {"column": "id", "op": "not_null"}


def test_a_null_rule_sinks_the_row_even_when_the_other_rules_pass():
    """TRUE AND NULL is NULL: the id is there, the amount cannot be judged."""
    rules = [NOT_NULL_ID, BETWEEN]
    with pytest.raises(ValueError, match=r"1 row\(s\) failed validation"):
        _run([(1, 50), (3, None)], mode="fail", rules=rules)
    _, cols, out = _run([(1, 50), (3, None)], mode="tag", rules=rules)
    assert {r[0]: r[cols.index("__dq_passed")] for r in out} == {1: True, 3: False}


def test_a_row_that_fails_one_rule_and_is_null_for_another_counts_once():
    """FALSE AND NULL is FALSE: the null id fails not_null, and there is no amount."""
    rules = [NOT_NULL_ID, BETWEEN]
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(conn=conn)
    ctx.set_result("src", conn.sql(
        "SELECT * FROM (VALUES (1, 50), (NULL, NULL)) AS t(id, v)"
    ))
    node = DataQualityNode(params={
        "_input_step_ids": ["src"], "_step_id": "dq", "mode": "fail", "rules": rules,
    })
    with pytest.raises(ValueError, match=r"1 row\(s\) failed validation"):
        node.execute(ctx)


def test_is_null_still_passes_a_null_value():
    """Only a NULL *result* is a failure. is_null on a NULL value is TRUE."""
    rule = {"column": "v", "op": "is_null"}
    for mode in MODES:
        assert _verdict(mode, rule, None, None) == "passed", mode
        assert _verdict(mode, rule, None, 5) == "failed", mode


def test_not_null_still_fails_a_null_value():
    rule = {"column": "v", "op": "not_null"}
    for mode in MODES:
        assert _verdict(mode, rule, 5, None) == "failed", mode
        assert _verdict(mode, rule, 5, 6) == "passed", mode


# ── The score and the threshold already counted it as failed; keep them in step ──

def test_the_quality_score_counts_a_null_rule_as_failed():
    _, cols, out = _run(
        [(1, 50), (3, None)], mode="tag", rules=[NOT_NULL_ID, BETWEEN], include_score=True,
    )
    score = {r[0]: r[cols.index("__dq_score")] for r in out}
    assert score == {1: 100.0, 3: 50.0}


def test_the_threshold_and_fail_mode_agree_about_the_null_row():
    """Two rows, one NULL: the pass rate is 50%. At a 40% threshold the rate is
    fine, but fail mode still stops the run for the row that failed."""
    with pytest.raises(ValueError, match="below threshold"):
        _run([(1, 50), (3, None)], mode="fail", rules=[BETWEEN], quality_threshold=60)
    with pytest.raises(ValueError, match=r"1 row\(s\) failed validation"):
        _run([(1, 50), (3, None)], mode="fail", rules=[BETWEEN], quality_threshold=40)
