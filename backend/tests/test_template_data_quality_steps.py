"""The built-in templates' Data Quality steps must actually check.

Eleven steps in ``frontend/src/templates/catalog.ts`` were written as
``{type: 'unique', columns: [...]}`` or ``{column, check: 'between', min, max}``
with ``on_fail`` / ``on_failure``. ``DataQualityNode`` reads ``rule['op']``,
``rule['column']``, ``rule['value']`` and ``params['mode']`` and nothing else, so
those rules compiled to ``TRUE``: "Drop empty rows" kept the empty rows,
"Validate required" never failed a run, and a ``between`` check was a not-null.

This module reads every ``data_quality`` step out of the catalog and, for each:

  * checks it is written in the shape the node reads, and
  * runs its params through the real node on a small table with one good row
    and one bad row per rule, then asserts what its mode promises: ``drop``
    removes the bad rows, ``fail`` raises, ``tag`` marks them, ``reject``
    routes them to the Reject output.

One bad row per rule means a single rule that silently passes everything is
caught on its own, not hidden behind the others.

``frontend/src/__tests__/templateDataQuality.test.ts`` holds the same shapes
from the catalog side, and also the wiring out of Pass / Reject.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Any

import duckdb
import pytest

from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.quality import DataQualityNode, _DQ_OPS

CATALOG = pathlib.Path(__file__).parents[2] / "frontend" / "src" / "templates" / "catalog.ts"

pytestmark = pytest.mark.skipif(
    not CATALOG.exists(), reason=f"frontend template catalog not found at {CATALOG}"
)

RULE_KEYS = {"column", "op", "value"}

# What the node offers, taken from the node itself so a new mode or op is seen here.
MODES = next(
    p["options"] for p in DataQualityNode.param_schema() if p["name"] == "mode"
)
PARAM_KEYS = set(DataQualityNode.default_params())

# A (matching value, non-matching value) pair for each regex a template uses.
# A new pattern in a template needs a pair here, so its bad row can be built.
REGEX_SAMPLES = {
    r"^[^@]+@[^@]+\.[^@]+$": ("a@b.co", "not-an-email"),
}


# ── Reading the catalog ────────────────────────────────────────────────────
# The catalog is TypeScript, but a step's `params` is a plain object literal:
# strings, numbers, booleans, arrays and objects. This reads that subset and
# stops with a clear message at anything else, so a change the reader cannot
# follow fails loudly instead of being skipped.

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _skip(src: str, i: int) -> int:
    while i < len(src):
        if src[i] in " \t\r\n":
            i += 1
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = len(src) if end < 0 else end
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        else:
            break
    return i


def _string(src: str, i: int) -> tuple[str, int]:
    quote, out, i = src[i], [], i + 1
    while src[i] != quote:
        if src[i] == "\\":
            if src[i + 1] == "u":
                out.append(chr(int(src[i + 2:i + 6], 16)))
                i += 6
            else:
                out.append(_ESCAPES.get(src[i + 1], src[i + 1]))
                i += 2
        else:
            out.append(src[i])
            i += 1
    return "".join(out), i + 1


def _value(src: str, i: int) -> tuple[Any, int]:
    i = _skip(src, i)
    ch = src[i]
    if ch in "'\"":
        return _string(src, i)
    if ch == "[":
        items, i = [], _skip(src, i + 1)
        while src[i] != "]":
            item, i = _value(src, i)
            items.append(item)
            i = _skip(src, i)
            if src[i] == ",":
                i = _skip(src, i + 1)
        return items, i + 1
    if ch == "{":
        obj, i = {}, _skip(src, i + 1)
        while src[i] != "}":
            if src[i] in "'\"":
                key, i = _string(src, i)
            else:
                m = _IDENT.match(src, i)
                assert m, f"catalog.ts: cannot read an object key at {src[i:i + 40]!r}"
                key, i = m.group(0), m.end()
            i = _skip(src, i)
            assert src[i] == ":", f"catalog.ts: expected ':' after key {key!r}"
            obj[key], i = _value(src, i + 1)
            i = _skip(src, i)
            if src[i] == ",":
                i = _skip(src, i + 1)
        return obj, i + 1
    m = _NUMBER.match(src, i)
    if m:
        text = m.group(0)
        return (float(text) if "." in text else int(text)), m.end()
    for word, val in (("true", True), ("false", False), ("null", None)):
        if src.startswith(word, i) and not src[i + len(word):i + len(word) + 1].isalnum():
            return val, i + len(word)
    raise AssertionError(
        f"catalog.ts: a Data Quality step's params hold something this reader does not "
        f"follow: {src[i:i + 60]!r}. Keep them to strings, numbers, booleans, arrays and objects."
    )


@dataclass(frozen=True)
class DqStep:
    template: str
    step: str
    label: str
    params: dict

    @property
    def id(self) -> str:
        return f"{self.template}/{self.step}"


def _read_dq_steps(src: str) -> list[DqStep]:
    steps: list[DqStep] = []
    for m in re.finditer(r"""type:\s*['"]data_quality['"]""", src):
        before = src[max(0, m.start() - 160):m.start()]
        after = src[m.end():m.end() + 400]
        step_id = re.findall(r"id:\s*['\"]([^'\"]+)['\"]", before)
        label = re.search(r"label:\s*'((?:[^'\\]|\\.)*)'", after)
        params_at = re.search(r"params:\s*", after)
        assert params_at, f"catalog.ts: a data_quality step has no params near {src[m.start():m.start() + 80]!r}"
        params, _ = _value(src, m.end() + params_at.end())
        template = re.findall(r"(?m)^    id: '([^']+)',", src[:m.start()])
        steps.append(DqStep(
            template=template[-1] if template else "?",
            step=step_id[-1] if step_id else "?",
            label=label.group(1) if label else "?",
            params=params,
        ))
    return steps


STEPS = _read_dq_steps(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else []


# ── Shape: only keys the node reads ────────────────────────────────────────

def _shape_problems(params: dict) -> list[str]:
    problems: list[str] = []
    rules = params.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["has no rules"]
    for n, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            problems.append(f"rule {n} is not an object")
            continue
        stray = sorted(set(rule) - RULE_KEYS)
        if stray:
            problems.append(f"rule {n} has keys the node does not read: {', '.join(stray)}")
        if not rule.get("column"):
            problems.append(f"rule {n} has no column")
        if rule.get("op") not in _DQ_OPS:
            problems.append(f"rule {n} op {rule.get('op')!r} is not one the node has")
    if params.get("mode") not in MODES:
        problems.append(f"mode {params.get('mode')!r} is not one of {', '.join(MODES)}")
    stray = sorted(set(params) - PARAM_KEYS)
    if stray:
        problems.append(f"sets {', '.join(stray)}, which the node does not read")
    return problems


# ── Behaviour: one bad row per rule ────────────────────────────────────────

def _other(v: Any) -> Any:
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v + 1
    return f"{v}-other"


def _good_and_bad(rule: dict) -> tuple[Any, Any]:
    """A value that passes the rule and one that fails it."""
    op, v = rule["op"], rule.get("value")
    if op == "not_null":
        return "x", None
    if op == "is_null":
        return None, "x"
    if op == "eq":
        return v, _other(v)
    if op == "ne":
        return _other(v), v
    if op == "gt":
        return v + 1, v
    if op == "gte":
        return v, v - 1
    if op == "lt":
        return v - 1, v
    if op == "lte":
        return v, v + 1
    if op == "in":
        outside = max(v) + 1 if all(isinstance(x, (int, float)) for x in v) else "zz-not-in-the-list"
        return v[0], outside
    if op == "not_in":
        inside = max(v) + 1 if all(isinstance(x, (int, float)) for x in v) else "zz-not-in-the-list"
        return inside, v[0]
    if op == "between":
        low, high = v
        assert all(isinstance(x, (int, float)) for x in (low, high)), (
            f"between {v!r}: this test builds rows for numeric ranges; extend _good_and_bad"
        )
        return (low + high) // 2, high + 1
    if op == "regex":
        assert v in REGEX_SAMPLES, (
            f"regex {v!r} has no (matching, non-matching) pair in REGEX_SAMPLES; add one"
        )
        return REGEX_SAMPLES[v]
    if op == "min_length":
        assert v > 0, "min_length 0 cannot be failed"
        return "x" * v, "x" * (v - 1)
    if op == "max_length":
        return "x" * v, "x" * (v + 1)
    raise AssertionError(f"no sample values for op {op!r}; extend _good_and_bad")


def _cases(rules: list[dict]) -> tuple[list[str], tuple, list[tuple]]:
    """Columns, the all-good row, and one row per rule that breaks only that rule."""
    columns: list[str] = []
    good: dict[str, Any] = {}
    for rule in rules:
        col = rule["column"]
        if col not in columns:
            columns.append(col)
        sample, _ = _good_and_bad(rule)
        # A not-null / is-null sample is a placeholder: prefer a value that
        # satisfies a more specific rule on the same column.
        if col not in good or rule["op"] not in ("not_null", "is_null"):
            good[col] = sample
    good_row = tuple(good[c] for c in columns)
    bad_rows = []
    for rule in rules:
        row = list(good_row)
        row[columns.index(rule["column"])] = _good_and_bad(rule)[1]
        bad_rows.append(tuple(row))
    return columns, good_row, bad_rows


def _sql(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _run(params: dict, columns: list[str], rows: list[tuple]):
    conn = duckdb.connect(":memory:")
    names = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
    values = ", ".join("(" + ", ".join(_sql(v) for v in row) + ")" for row in rows)
    ctx = ExecutionContext(conn=conn)
    ctx.set_result("src", conn.sql(f"SELECT * FROM (VALUES {values}) AS t({names})"))
    node = DataQualityNode(params={"_input_step_ids": ["src"], "_step_id": "dq", **params})
    rel = node.execute(ctx)
    return ctx, rel.columns, rel.fetchall()


def _by_row(columns: list[str], out_columns: list[str], out_rows: list[tuple], extra: str):
    """Map each input row to the value of the extra column the node added."""
    keep = [out_columns.index(c) for c in columns]
    flag = out_columns.index(extra)
    return {tuple(r[i] for i in keep): r[flag] for r in out_rows}


def _assert_step_checks(params: dict) -> None:
    rules, mode = params["rules"], params["mode"]
    columns, good_row, bad_rows = _cases(rules)
    rows = [good_row, *bad_rows]
    # Each message names the rules whose bad row got through, so one rule that
    # checks nothing is pointed at, not just "a row stayed".

    if mode in ("drop", "split"):
        ctx, out_columns, out = _run(params, columns, rows)
        assert good_row in out, "the good row was removed"
        let_through = [r for r, bad in zip(rules, bad_rows) if bad in out]
        assert not let_through, f"bad rows stayed for: {let_through}"
        assert out_columns == columns, f"added columns: {out_columns}"
        if mode == "split":
            assert len(ctx.vars["_dq_failures_dq"].fetchall()) == len(bad_rows)
    elif mode == "fail":
        _run(params, columns, [good_row])  # good data does not fail the run
        for rule, bad in zip(rules, bad_rows):
            try:
                _run(params, columns, [good_row, bad])
            except ValueError as exc:
                assert "failed validation" in str(exc), str(exc)
            else:
                pytest.fail(f"the run did not fail for a row that breaks {rule}")
    elif mode in ("tag", "reject"):
        flag, passed, failed = (
            ("__dq_passed", True, False) if mode == "tag" else ("_split_output", "pass", "reject")
        )
        _, out_columns, out = _run(params, columns, rows)
        flags = _by_row(columns, out_columns, out, flag)
        assert flags.get(good_row) == passed, f"the good row came out as {flags.get(good_row)!r}"
        not_marked = [r for r, bad in zip(rules, bad_rows) if flags.get(bad) != failed]
        assert not not_marked, f"bad rows were not marked {failed!r} for: {not_marked}"
    else:  # pragma: no cover - _shape_problems rejects it first
        raise AssertionError(f"unhandled mode {mode!r}")


# ── Tests ──────────────────────────────────────────────────────────────────

def test_the_catalog_steps_were_found():
    assert STEPS, "no data_quality step was read from the template catalog"
    assert all(s.template != "?" and s.step != "?" for s in STEPS), [s.id for s in STEPS]


@pytest.mark.parametrize("entry", STEPS, ids=[s.id for s in STEPS])
def test_step_is_written_in_the_shape_the_node_reads(entry: DqStep):
    assert _shape_problems(entry.params) == [], f"{entry.id} \"{entry.label}\""


@pytest.mark.parametrize("entry", STEPS, ids=[s.id for s in STEPS])
def test_step_checks_what_its_mode_promises(entry: DqStep):
    _assert_step_checks(entry.params)


def test_the_check_would_have_caught_the_old_shapes():
    old = [
        {"rules": [{"type": "not_null", "columns": ["order_id"]}], "on_fail": "drop"},
        {"rules": [{"type": "unique", "columns": ["id"]}], "on_fail": "fail"},
        {"rules": [{"column": "amount", "check": "between", "min": 0, "max": 1000000}], "on_failure": "tag"},
    ]
    for params in old:
        assert _shape_problems(params), params


def test_no_template_asks_for_a_rule_the_node_lacks():
    """There is no unique-value rule. A template that must collapse duplicate keys
    uses a Deduplicate step."""
    assert "unique" not in _DQ_OPS
    asking = [s.id for s in STEPS if "unique" in repr(s.params)]
    assert asking == []
