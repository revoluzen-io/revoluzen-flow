"""A Data Quality rule the node cannot use stops the run. It no longer passes every row.

``_build_rule_sql`` turned a rule with no column, an op it does not have, or a value
it could not read into ``TRUE``. The rule then passed every row and nothing said so.
That is how the built-in templates' checks written as ``{type: 'unique', columns:
[...]}`` went unnoticed for so long, and how a ``between`` saved as the text
"10,100" would have been a check that never failed. Now the step raises before it
checks a single row, naming the rule and what it needs, and the run stops with a
user-input failure (a retry cannot fix it).

Two parts:

  * the node itself, in every mode, with the messages;
  * ``frontend/src/__tests__/fixtures/dqRuleCases.json``, one table of rules that
    can and cannot run. The editor's check (dataQualityRuleCases.test.ts) reads the
    same table, so the editor and the node cannot disagree about what is unusable.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import duckdb
import pytest

from revoluzen_flow.engine.failure_class import FailureClass, classify_error
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.quality import DataQualityNode, _DQ_OPS

FIXTURE = (
    pathlib.Path(__file__).parents[2]
    / "frontend" / "src" / "__tests__" / "fixtures" / "dqRuleCases.json"
)
CASES: list[dict[str, Any]] = (
    json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"] if FIXTURE.exists() else []
)
UNUSABLE = [c for c in CASES if not c["usable"]]

MODES = ["drop", "fail", "tag", "reject", "split"]
OLD_TEMPLATE_SHAPES = [
    {"type": "not_null", "columns": ["order_id"]},
    {"type": "unique", "columns": ["id"]},
]


def _run(rules: Any, mode: str = "drop", **extra: Any):
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(conn=conn)
    ctx.set_result("src", conn.sql(
        "SELECT * FROM (VALUES (1, 50, 'north'), (2, 5, 'south')) AS t(id, amount, region)"
    ))
    node = DataQualityNode(params={
        "_input_step_ids": ["src"], "_step_id": "dq", "mode": mode, "rules": rules, **extra,
    })
    rel = node.execute(ctx)
    return ctx, rel.columns, rel.fetchall()


# ── The rule that used to pass every row ───────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("rule", OLD_TEMPLATE_SHAPES, ids=["type-not_null", "type-unique"])
def test_a_rule_written_in_the_old_template_shape_stops_the_run(rule, mode):
    """No `column`, so it compiled to TRUE and let every row through, in every mode."""
    with pytest.raises(ValueError, match=r"rule 1.*no column"):
        _run([rule], mode=mode)


@pytest.mark.parametrize("mode", MODES)
def test_an_unusable_rule_stops_the_run_in_every_mode(mode):
    bad = {"column": "amount", "op": "between", "value": "10,100"}
    with pytest.raises(ValueError, match=r"rule 1 \(amount between\)"):
        _run([bad], mode=mode)
    # The score and the threshold do not get past it either.
    with pytest.raises(ValueError, match=r"rule 1 \(amount between\)"):
        _run([bad], mode=mode, include_score=True, quality_threshold=50)


def test_the_message_says_what_the_rule_needs():
    with pytest.raises(ValueError) as info:
        _run([{"column": "amount", "op": "between", "value": [10]}])
    text = str(info.value)
    assert text.startswith("Data Quality:")
    assert "needs a low and a high limit" in text
    assert "no rows were checked" in text


def test_every_rule_that_cannot_run_is_named_and_the_others_are_not():
    rules = [
        {"column": "id", "op": "not_null"},                       # 1 fine
        {"column": "amount", "op": "between", "value": ""},       # 2 no limits
        {"column": "region", "op": "in", "value": ["north"]},     # 3 fine
        {"op": "not_null"},                                       # 4 no column
        {"column": "amount", "op": "unique"},                     # 5 no such check
        "amount > 0",                                             # 6 not a rule
    ]
    with pytest.raises(ValueError) as info:
        _run(rules)
    text = str(info.value)
    for named in ("rule 2 (amount between)", "rule 4", "rule 5 (amount unique)", "rule 6"):
        assert named in text, named
    assert "rule 1" not in text and "rule 3" not in text
    assert "no column chosen" in text
    assert "is not one of the checks" in text
    assert "is not a rule" in text


def test_a_rule_with_two_problems_reports_both():
    with pytest.raises(ValueError) as info:
        _run([{"column": "", "op": "between"}])
    text = str(info.value)
    assert "no column chosen" in text and "needs a low and a high limit" in text


def test_the_rules_must_be_a_list():
    with pytest.raises(ValueError, match="rules must be a list"):
        _run({"column": "id", "op": "not_null"})
    with pytest.raises(ValueError, match="rules must be a list"):
        _run("id is not null")


def test_nothing_is_checked_and_nothing_is_set_aside_when_a_rule_cannot_run():
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(conn=conn)
    ctx.set_result("src", conn.sql("SELECT * FROM (VALUES (1), (2)) AS t(id)"))
    node = DataQualityNode(params={
        "_input_step_ids": ["src"], "_step_id": "dq", "mode": "split",
        "rules": [{"column": "id", "op": "eq"}],
    })
    with pytest.raises(ValueError):
        node.execute(ctx)
    assert "_dq_failures_dq" not in ctx.vars


def test_the_failure_is_user_input_so_it_is_not_retried():
    with pytest.raises(ValueError) as info:
        _run([{"op": "not_null"}])
    assert classify_error(info.value) is FailureClass.USER_INPUT


# ── What still runs ────────────────────────────────────────────────────────

def test_empty_rules_still_pass_the_rows_through():
    for rules in ([], None):
        _, _, out = _run(rules)
        assert sorted(r[0] for r in out) == [1, 2]
    conn = duckdb.connect(":memory:")
    ctx = ExecutionContext(conn=conn)
    ctx.set_result("src", conn.sql("SELECT * FROM (VALUES (1), (2)) AS t(id)"))
    out = DataQualityNode(params={"_input_step_ids": ["src"], "_step_id": "dq"}).execute(ctx).fetchall()
    assert sorted(r[0] for r in out) == [1, 2]


@pytest.mark.parametrize("rule,kept", [
    ({"column": "id", "op": "not_null"}, [1, 2]),
    ({"column": "id"}, [1, 2]),
    ({"column": "id", "op": "not_null", "value": ""}, [1, 2]),
    ({"column": "amount", "op": "eq", "value": 50}, [1]),
    ({"column": "amount", "op": "gt", "value": "10"}, [1]),
    ({"column": "region", "op": "in", "value": "north"}, [1]),
    ({"column": "region", "op": "not_in", "value": ["north"]}, [2]),
    ({"column": "amount", "op": "between", "value": [10, 100]}, [1]),
    ({"column": "amount", "op": "between", "value": ["10", "100"]}, [1]),
    ({"column": "amount", "op": "between", "value": [100, 10]}, []),
    ({"column": "region", "op": "regex", "value": "^n"}, [1]),
    ({"column": "region", "op": "min_length", "value": "5"}, [1, 2]),
    ({"column": "region", "op": "max_length", "value": 5.0}, [1, 2]),
], ids=lambda v: json.dumps(v) if isinstance(v, dict) else None)
def test_a_rule_that_can_run_still_checks_the_rows(rule, kept):
    _, _, out = _run([rule])
    assert sorted(r[0] for r in out) == kept


# ── The shared table: the node and the editor agree ────────────────────────

needs_table = pytest.mark.skipif(
    not CASES, reason=f"the shared rule cases were not found at {FIXTURE}"
)


@needs_table
def test_the_case_table_covers_every_op_the_node_has():
    """A new op needs a rule that runs and, if it takes a value, one that cannot."""
    ops_that_run = {(c["rule"].get("op") or "not_null") for c in CASES if c["usable"] and isinstance(c["rule"], dict)}
    ops_that_cannot = {c["rule"].get("op") for c in UNUSABLE if isinstance(c["rule"], dict)}
    assert set(_DQ_OPS) <= ops_that_run, sorted(set(_DQ_OPS) - ops_that_run)
    needs_a_value = set(_DQ_OPS) - {"not_null", "is_null"}
    assert needs_a_value <= ops_that_cannot, sorted(needs_a_value - ops_that_cannot)


@needs_table
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_node_agrees_with_the_case_table(case):
    from revoluzen_flow.nodes.quality import dq_rule_problems

    problems = dq_rule_problems([case["rule"]])
    assert (not problems) == case["usable"], f"{case['rule']!r} -> {problems}"


@needs_table
@pytest.mark.parametrize("case", UNUSABLE, ids=[c["name"] for c in UNUSABLE])
def test_every_rule_in_the_table_that_cannot_run_stops_the_step(case):
    with pytest.raises(ValueError, match=r"Data Quality:.*rule 1"):
        _run([case["rule"]])
