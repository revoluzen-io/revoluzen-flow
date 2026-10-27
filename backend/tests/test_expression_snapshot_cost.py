"""Expression resolution must not make a run quadratic (2026-10-23).

`_execute_step` called `ctx.results_as_rows()` before every step. That
pulls EVERY upstream result fully into Python dicts, so step N re-fetched
the complete output of steps 1..N-1. Measured on a 120,000-row source: a
3-step chain pulled 360,000 row-dicts and a 9-step chain pulled 4,320,000
(120k x sum(0..8)). Wall clock went 0.74 s -> 6.41 s for identical input.

Almost all of it was waste: most steps contain no `{{ }}` template, and the
snapshot was built and discarded. `_has_template` now gates the call.

Two things need pinning, and the first matters more than the second:

  1. Templates STILL RESOLVE. A performance guard that skips resolution
     when it was needed would be far worse than the slowness it fixes.
  2. The snapshot is NOT taken when no template is present, and the cost
     of a run stays linear in the number of steps.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.engine.executor import _has_template


# ── the guard itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("params", [
    {"condition": "{{ $json.amount }} > 10"},
    {"nested": {"deep": ["x", "{{ $vars.FOO }}"]}},
    {"list": [{"expr": "{{ item.id }}"}]},
    {"a": "plain", "b": "{{ x }}"},
    {"tuple_like": ("a", "{{ y }}")},
])
def test_detects_templates_anywhere_in_the_tree(params):
    assert _has_template(params) is True


@pytest.mark.parametrize("params", [
    {},
    {"condition": "amount > 10"},
    {"sql": "SELECT * FROM t WHERE status = 'paid'"},
    {"columns": [{"name": "band", "expression": "CASE WHEN amount > 5 THEN 'h' ELSE 'l' END"}]},
    {"n": 5, "flag": True, "none": None},
    # A single brace is not a template — must not trigger the slow path.
    {"fmt": "{not_a_template}"},
])
def test_no_false_positives_on_plain_params(params):
    assert _has_template(params) is False


def test_single_brace_json_body_is_not_a_template():
    """An HTTP node carrying a JSON body full of single braces must stay on
    the fast path — this is the common case that would silently lose the
    optimisation."""
    assert _has_template({"body": '{"a": 1, "b": {"c": 2}}'}) is False


# ── correctness: templates must still resolve end to end ────────────────

PASSWORD = "ExprCost!2026X"


@pytest.fixture
def client(tmp_path):
    import os

    os.environ["REVOLUZEN_FLOW_DATA_DIR"] = str(tmp_path)
    from fastapi.testclient import TestClient
    import revoluzen_flow.main as m
    from revoluzen_flow.auth.models import User

    (tmp_path / "orders.csv").write_text(
        "order_id,amount,status\n1,250,paid\n2,89,paid\n3,1200,cancelled\n",
        encoding="utf-8",
    )
    with TestClient(m.app, base_url="http://localhost") as c:
        admin = m.app_state["user_store"].get_user("admin")
        admin.password_hash = User.hash_password(PASSWORD)
        m.app_state["user_store"]._save_user(admin)
        tok = c.post("/api/auth/login", json={
            "email": "admin@revoluzen.local", "password": PASSWORD}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c


def _run(c, steps, conns):
    wf = c.post("/api/workflows", json={
        "name": "expr-cost", "steps": steps, "connections": conns})
    assert wf.status_code < 400, wf.text
    wid = wf.json()["id"]
    r = c.post(f"/api/execute/workflow/{wid}?full_run=true&safety_mode=live", json={})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_pipeline_with_no_templates_still_runs_correctly(client):
    body = _run(client, [
        {"id": "s1", "type": "csv_source", "label": "src",
         "params": {"file_path": "orders.csv"}, "position": {"x": 0, "y": 0}},
        {"id": "s2", "type": "filter", "label": "paid",
         "params": {"mode": "expression", "condition": "status = 'paid'"},
         "position": {"x": 1, "y": 0}},
    ], [{"from_step": "s1", "to_step": "s2"}])
    assert body["status"] == "success"
    assert body["step_results"]["s1"]["row_count"] == 3
    assert body["step_results"]["s2"]["row_count"] == 2


def test_a_step_WITH_a_template_still_gets_the_snapshot(client, monkeypatch):
    """The guard must not skip resolution when it IS needed.

    Asserted as "the snapshot is taken", not "this expression evaluates to
    that value", because the second would be testing the expression
    language rather than this change — and the snapshot is precisely what
    the guard decides. If `results_as_rows` is reached, `resolve_expressions`
    receives real upstream rows and behaves exactly as it did before.
    """
    from revoluzen_flow.nodes.base import ExecutionContext

    calls = {"n": 0}
    original = ExecutionContext.results_as_rows

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(ExecutionContext, "results_as_rows", counting)

    _run(client, [
        {"id": "s1", "type": "csv_source", "label": "src",
         "params": {"file_path": "orders.csv"}, "position": {"x": 0, "y": 0}},
        {"id": "s2", "type": "filter", "label": "templated",
         # A template in the condition — the step cannot be resolved
         # without the upstream snapshot.
         "params": {"mode": "expression", "condition": "amount > {{ $vars.FLOOR }}"},
         "position": {"x": 1, "y": 0}},
    ], [{"from_step": "s1", "to_step": "s2"}])

    assert calls["n"] >= 1, (
        "results_as_rows was never called for a step whose params contain a "
        "'{{ }}' template — the fast-path guard is skipping resolution that "
        "is actually needed"
    )


# ── the cost itself ─────────────────────────────────────────────────────

def test_snapshot_is_skipped_when_no_step_has_a_template(client, monkeypatch):
    """The actual regression guard: zero snapshots for a template-free run.

    Counting calls rather than timing — a wall-clock assertion would be
    flaky on a loaded machine, and the call count is what the defect was.
    """
    from revoluzen_flow.nodes.base import ExecutionContext

    calls = {"n": 0}
    original = ExecutionContext.results_as_rows

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(ExecutionContext, "results_as_rows", counting)

    steps = [{"id": "s0", "type": "csv_source", "label": "src",
              "params": {"file_path": "orders.csv"}, "position": {"x": 0, "y": 0}}]
    for i in range(1, 6):
        steps.append({"id": f"s{i}", "type": "derived_column", "label": f"d{i}",
                      "params": {"columns": [{"name": f"c{i}", "expression": f"amount + {i}"}]},
                      "position": {"x": i, "y": 0}})
    conns = [{"from_step": f"s{i}", "to_step": f"s{i+1}"} for i in range(5)]

    body = _run(client, steps, conns)
    assert body["status"] == "success"
    assert calls["n"] == 0, (
        f"results_as_rows was called {calls['n']} times for a pipeline with no "
        "templates — each call pulls every upstream result into Python and is "
        "what made runs quadratic in step count"
    )
