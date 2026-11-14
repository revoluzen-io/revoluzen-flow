"""For Each (Run Pipeline) — true per-item loop (2026-06-11).

Per-row loop: run a sub-pipeline once per input row,
injecting the row as the sub-pipeline's parameters. The sub-execution itself
is ExecutePipeline's well-tested machinery; these tests pin the LOOP logic —
iteration count, per-row param injection, the item param, error policy, the
safety cap, and passthrough — by monkeypatching the sub-runner.
"""
from __future__ import annotations

import duckdb
import pytest

from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes import flow_control
from revoluzen_flow.nodes.flow_control import ForEachPipelineNode


def _ctx() -> ExecutionContext:
    return ExecutionContext(conn=duckdb.connect(":memory:"))


def _seed(ctx, sid, sql):
    rel = ctx.conn.sql(sql)
    ctx.set_result(sid, rel)
    return rel


class _OK:
    status = "success"
    error = ""


def _items(ctx):
    return _seed(ctx, "items",
                 "SELECT * FROM (VALUES (1,'a'),(2,'b'),(3,'c')) AS t(id, name)")


def test_runs_subpipeline_once_per_row_and_passes_through(monkeypatch):
    ctx = _ctx()
    _items(ctx)
    calls = []
    monkeypatch.setattr(flow_control, "_run_subpipeline",
                        lambda c, pid, ov: (calls.append((pid, dict(ov))), _OK())[1])
    node = ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"})
    out = node.execute(ctx)

    assert len(calls) == 3
    # per-row columns injected by name + whole row under the item param
    assert calls[0][0] == "child"
    assert calls[0][1]["id"] == 1 and calls[0][1]["name"] == "a"
    assert calls[0][1]["item"] == {"id": 1, "name": "a"}
    # input passes through unchanged (ForEach is control flow)
    assert sorted(out.fetchall()) == [(1, "a"), (2, "b"), (3, "c")]


def test_static_params_merged_and_overridden_by_row(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM (VALUES (1)) AS t(id)")
    seen = {}
    monkeypatch.setattr(flow_control, "_run_subpipeline",
                        lambda c, pid, ov: (seen.update(ov), _OK())[1])
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child",
        "parameters": {"env": "prod", "id": 999},
    })
    node.execute(ctx)
    assert seen["env"] == "prod"      # static carried
    assert seen["id"] == 1            # row overrides static


def test_custom_item_param_name(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM (VALUES (5)) AS t(id)")
    seen = {}
    monkeypatch.setattr(flow_control, "_run_subpipeline",
                        lambda c, pid, ov: (seen.update(ov), _OK())[1])
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "item_param": "row",
    })
    node.execute(ctx)
    assert seen["row"] == {"id": 5}


def test_on_item_error_fail_stops(monkeypatch):
    ctx = _ctx()
    _items(ctx)

    def boom(c, pid, ov):
        if ov["id"] == 2:
            raise RuntimeError("child blew up")
        return _OK()

    monkeypatch.setattr(flow_control, "_run_subpipeline", boom)
    node = ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"})
    with pytest.raises(ValueError, match="item 2/3 failed"):
        node.execute(ctx)


def test_on_item_error_continue_skips(monkeypatch):
    ctx = _ctx()
    _items(ctx)
    ran = []

    def boom(c, pid, ov):
        ran.append(ov["id"])
        if ov["id"] == 2:
            raise RuntimeError("child blew up")
        return _OK()

    monkeypatch.setattr(flow_control, "_run_subpipeline", boom)
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "on_item_error": "continue",
    })
    out = node.execute(ctx)
    assert ran == [1, 2, 3]                 # all attempted despite item 2 failing
    assert sorted(out.fetchall()) == [(1, "a"), (2, "b"), (3, "c")]


def test_child_error_status_respected(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM (VALUES (1)) AS t(id)")

    class _Err:
        status = "error"
        error = "bad config"

    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: _Err())
    node = ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"})
    with pytest.raises(ValueError, match="sub-pipeline failed"):
        node.execute(ctx)


def test_max_iterations_cap_refuses_oversized_input(monkeypatch):
    ctx = _ctx()
    _seed(ctx, "items", "SELECT * FROM range(5) AS t(id)")
    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: _OK())
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_iterations": 2,
    })
    with pytest.raises(ValueError, match="max_iterations is 2"):
        node.execute(ctx)


def test_missing_pipeline_id_and_input():
    ctx = _ctx()
    _seed(ctx, "items", "SELECT 1 AS id")
    with pytest.raises(ValueError, match="pipeline_id"):
        ForEachPipelineNode(params={"_input_step_ids": ["items"]}).execute(ctx)
    with pytest.raises(ValueError, match="input"):
        ForEachPipelineNode(params={"_input_step_ids": [], "pipeline_id": "child"}).execute(ctx)


# ── Rows at once (max_parallel) ─────────────────────────────────────────

import threading
import time

from revoluzen_flow.engine.cancellation import RunCancelled, clear_token, create_token


class _Concurrency:
    """Counts how many fake child runs are inside the sub-runner at once."""

    def __init__(self, hold: float = 0.05):
        self.hold = hold
        self.now = 0
        self.peak = 0
        self.seen: list[int] = []
        self._lock = threading.Lock()

    def __call__(self, c, pid, ov):
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            self.seen.append(ov["id"])
        time.sleep(self.hold)
        with self._lock:
            self.now -= 1
        return _OK()


def _range_items(ctx, n):
    return _seed(ctx, "items", f"SELECT range + 1 AS id FROM range({n})")


def test_parallel_runs_every_row_within_the_cap(monkeypatch):
    ctx = _ctx()
    _range_items(ctx, 8)
    fake = _Concurrency()
    monkeypatch.setattr(flow_control, "_run_subpipeline", fake)
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": 3,
    })
    out = node.execute(ctx)
    assert sorted(fake.seen) == list(range(1, 9))
    assert 1 < fake.peak <= 3
    assert sorted(r[0] for r in out.fetchall()) == list(range(1, 9))


def test_one_row_at_a_time_by_default(monkeypatch):
    ctx = _ctx()
    _range_items(ctx, 4)
    fake = _Concurrency(hold=0.01)
    monkeypatch.setattr(flow_control, "_run_subpipeline", fake)
    ForEachPipelineNode(params={"_input_step_ids": ["items"], "pipeline_id": "child"}).execute(ctx)
    assert fake.peak == 1
    assert fake.seen == [1, 2, 3, 4]


def test_parallel_fail_stops_starting_rows_and_names_the_first_failure(monkeypatch):
    ctx = _ctx()
    _range_items(ctx, 5)
    started = []

    def run(c, pid, ov):
        started.append(ov["id"])
        if ov["id"] == 1:
            raise RuntimeError("child blew up")
        time.sleep(0.2)
        return _OK()

    monkeypatch.setattr(flow_control, "_run_subpipeline", run)
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": 2,
    })
    with pytest.raises(ValueError, match=r"item 1/5 failed: child blew up.*3 more row\(s\) were not started"):
        node.execute(ctx)
    assert sorted(started) == [1, 2]


def test_parallel_continue_reports_every_row_in_results(monkeypatch):
    ctx = _ctx()
    _items(ctx)

    class _Err:
        status = "error"
        error = "bad config"

    def run(c, pid, ov):
        time.sleep(0.01 * (4 - ov["id"]))   # finish out of order on purpose
        return _Err() if ov["id"] == 2 else _OK()

    monkeypatch.setattr(flow_control, "_run_subpipeline", run)
    node = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": 3,
        "on_item_error": "continue", "output": "results",
    })
    rel = node.execute(ctx)
    assert rel.columns == ["id", "name", "_item", "_status", "_error", "_duration_ms"]
    rows = rel.fetchall()
    assert [(r[0], r[2], r[3], r[4]) for r in rows] == [
        (1, 1, "success", None),
        (2, 2, "error", "bad config"),
        (3, 3, "success", None),
    ]


def test_results_output_in_order_when_sequential(monkeypatch):
    ctx = _ctx()
    _items(ctx)
    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: _OK())
    rel = ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "output": "results",
    }).execute(ctx)
    assert [(r[0], r[2], r[3]) for r in rel.fetchall()] == [
        (1, 1, "success"), (2, 2, "success"), (3, 3, "success"),
    ]


def test_parallel_workers_carry_the_pipeline_chain(monkeypatch):
    """A worker thread must still know which pipelines it is inside, or a
    child that starts its own parent would loop instead of stopping."""
    ctx = _ctx()
    _items(ctx)
    seen = []
    monkeypatch.setattr(
        flow_control, "_run_subpipeline",
        lambda c, pid, ov: (seen.append(getattr(flow_control._inside_pipelines, "ids", ())), _OK())[1],
    )
    flow_control._inside_pipelines.ids = ("grandparent",)
    try:
        ForEachPipelineNode(params={
            "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": 3,
        }).execute(ctx)
    finally:
        flow_control._inside_pipelines.ids = ()
    assert seen == [("grandparent",)] * 3


@pytest.mark.parametrize("parallel", [1, 3])
def test_stop_ends_the_loop_before_any_new_row(monkeypatch, parallel):
    ctx = ExecutionContext(conn=duckdb.connect(":memory:"), run_id="run-stop-test")
    _items(ctx)
    calls = []
    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: (calls.append(ov["id"]), _OK())[1])
    token = create_token("run-stop-test")
    token.cancel("Stopped by a test")
    try:
        with pytest.raises(RunCancelled):
            ForEachPipelineNode(params={
                "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": parallel,
            }).execute(ctx)
    finally:
        clear_token("run-stop-test")
    assert calls == []


def test_memory_pressure_starts_one_row_at_a_time(monkeypatch):
    class _Tier:
        value = "red"

    class _Sample:
        tier = _Tier()

    class _Governor:
        def sample(self):
            return _Sample()

    class _Manager:
        _governor = _Governor()

    ctx = ExecutionContext(conn=duckdb.connect(":memory:"), app_state={"execution_manager": _Manager()})
    _range_items(ctx, 5)
    fake = _Concurrency(hold=0.02)
    monkeypatch.setattr(flow_control, "_run_subpipeline", fake)
    ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": 4,
    }).execute(ctx)
    assert fake.peak == 1
    assert sorted(fake.seen) == [1, 2, 3, 4, 5]


def test_rows_at_once_is_validated_and_capped(monkeypatch):
    ctx = _ctx()
    _items(ctx)
    monkeypatch.setattr(flow_control, "_run_subpipeline", lambda c, pid, ov: _OK())
    with pytest.raises(ValueError, match="max_parallel"):
        ForEachPipelineNode(params={
            "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": -2,
        }).execute(ctx)
    with pytest.raises(ValueError, match="invalid output"):
        ForEachPipelineNode(params={
            "_input_step_ids": ["items"], "pipeline_id": "child", "output": "rows",
        }).execute(ctx)
    # Asking for more than the ceiling runs, at the ceiling.
    ForEachPipelineNode(params={
        "_input_step_ids": ["items"], "pipeline_id": "child", "max_parallel": 500,
    }).execute(ctx)
