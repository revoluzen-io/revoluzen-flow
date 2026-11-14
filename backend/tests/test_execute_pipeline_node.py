"""One pipeline can start another, for real.

Execute Pipeline crashed on every run: the store returns a version of the
pipeline, not a dict, and a second path read fields that don't exist. For Each
(Run Pipeline) shared the first fault. These run a real child through the real
executor, with a store that hands back what the real one does.
"""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest

from revoluzen_flow.ir.schema import Workflow, WorkflowVersion
from revoluzen_flow.nodes import flow_control
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.flow_control import ExecutePipelineNode, ForEachPipelineNode


class _Store:
    def __init__(self, *flows, workspace="default"):
        self.flows = {f.id: f for f in flows}
        self.workspace = workspace

    def get(self, workflow_id, workspace_id=None):
        flow = self.flows.get(workflow_id)
        if flow is None or (workspace_id and (flow.workspace_id or "default") != workspace_id):
            return None
        return WorkflowVersion(version=3, workflow=flow)

    # A child is started at its live (published) version. These fakes have
    # one version each, so the live one is that version.
    def live_version(self, workflow_id, workspace_id=None):
        return self.get(workflow_id, workspace_id=workspace_id)


class _Runs:
    def __init__(self):
        self.records = []

    def record(self, execution):
        self.records.append(execution)
        return execution


def _child(tmp_path, wid="child", name="Child pipeline", workspace="default", **extra):
    return Workflow.model_validate({
        "id": wid, "name": name, "workspace_id": workspace, **extra,
        "steps": [
            {"id": "make", "type": "transform", "label": "Make", "params": {"sql": "SELECT 1 AS n"}, "position": {"x": 0, "y": 0}, "risk": "low"},
        ],
        "connections": [],
    })


def _ctx(tmp_path, *flows, parent_id="parent"):
    runs = _Runs()
    ctx = ExecutionContext(conn=duckdb.connect(":memory:"))
    ctx.data_dir = str(tmp_path)
    ctx.app_state = {"store": _Store(*flows), "execution_store": runs}
    ctx._workflow_ref = SimpleNamespace(id=parent_id, name="Parent pipeline", workspace_id="default")
    ctx.full_run = True
    ctx.set_result("in", ctx.conn.sql("SELECT * FROM (VALUES (1),(2)) AS t(id)"))
    return ctx, runs


class _Result:
    def __init__(self, status="success", error=None):
        self.status = status
        self.step_results = {"make": SimpleNamespace(status=status, row_count=1, duration_ms=1.0, error=error)}


@pytest.fixture
def executed(monkeypatch):
    """Stands in for the engine, and notes what it was asked to run."""
    from revoluzen_flow.engine import executor as engine

    calls = []
    outcome = {"result": _Result()}

    class _Executor:
        def __init__(self, *args, **kwargs):
            pass

        def execute_workflow(self, wf, **kwargs):
            calls.append((wf, kwargs))
            return outcome["result"]

    monkeypatch.setattr(engine, "WorkflowExecutor", _Executor)
    monkeypatch.setattr("revoluzen_flow.security.execution_codes.mint_for_run", lambda wf: "code")
    return SimpleNamespace(calls=calls, outcome=outcome)


def _node(**params):
    return ExecutePipelineNode(params={"_input_step_ids": ["in"], "pipeline_id": "child", **params})


def test_runs_the_child_and_passes_its_input_on(tmp_path, executed):
    ctx, runs = _ctx(tmp_path, _child(tmp_path))
    out = _node().execute(ctx)
    assert [wf.id for wf, _ in executed.calls] == ["child"]
    assert sorted(out.fetchall()) == [(1,), (2,)]


def test_the_child_gets_its_own_run_record(tmp_path, executed):
    ctx, runs = _ctx(tmp_path, _child(tmp_path))
    _node().execute(ctx)
    record = runs.records[0]
    assert (record.workflow_id, record.workflow_name, record.status) == ("child", "Child pipeline", "success")
    assert record.triggered_by == "pipeline"
    assert record.workflow_version == 3
    assert record.metadata["started_by_pipeline"] == "Parent pipeline"
    assert record.steps_completed == 1


def test_a_failed_child_stops_the_parent_and_says_why(tmp_path, executed):
    executed.outcome["result"] = _Result("error", "table not found")
    ctx, runs = _ctx(tmp_path, _child(tmp_path))
    with pytest.raises(ValueError, match="table not found"):
        _node().execute(ctx)
    assert runs.records[0].status == "error"
    assert runs.records[0].error_message == "table not found"


def test_a_failed_child_can_be_allowed_to_continue(tmp_path, executed):
    executed.outcome["result"] = _Result("error", "table not found")
    ctx, _ = _ctx(tmp_path, _child(tmp_path))
    assert _node(on_failure="continue").execute(ctx).fetchall()


def test_only_declared_parameters_are_passed(tmp_path, executed):
    flow = _child(tmp_path, parameters=[{"name": "region", "type": "string", "default": "EMEA", "description": "", "required": False}])
    ctx, _ = _ctx(tmp_path, flow)
    _node(parameters={"region": "APAC", "other": "x"}).execute(ctx)
    assert executed.calls[0][1]["parameter_values"] == {"region": "APAC"}


def test_it_can_start_with_nothing_coming_in(tmp_path, executed):
    ctx, _ = _ctx(tmp_path, _child(tmp_path))
    out = ExecutePipelineNode(params={"pipeline_id": "child"}).execute(ctx)
    assert out.fetchall() == [("child", "success")]


def test_no_pipeline_chosen_is_refused(tmp_path, executed):
    ctx, _ = _ctx(tmp_path)
    with pytest.raises(ValueError, match="choose the pipeline"):
        ExecutePipelineNode(params={"_input_step_ids": ["in"]}).execute(ctx)


def test_an_unknown_pipeline_is_refused(tmp_path, executed):
    ctx, _ = _ctx(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        _node().execute(ctx)


def test_a_pipeline_in_another_workspace_is_refused(tmp_path, executed):
    ctx, _ = _ctx(tmp_path, _child(tmp_path, workspace="other"))
    with pytest.raises(ValueError, match="not found"):
        _node().execute(ctx)
    assert executed.calls == []


def test_a_pipeline_cannot_start_itself(tmp_path, executed):
    ctx, _ = _ctx(tmp_path, _child(tmp_path), parent_id="child")
    with pytest.raises(ValueError, match="starting itself"):
        _node().execute(ctx)
    assert executed.calls == []


def test_a_loop_through_another_pipeline_is_caught(tmp_path, monkeypatch, executed):
    ctx, _ = _ctx(tmp_path, _child(tmp_path), parent_id="top")
    monkeypatch.setattr(flow_control._inside_pipelines, "ids", ("child",), raising=False)
    with pytest.raises(ValueError, match="starting itself"):
        _node().execute(ctx)


def test_the_loop_guard_clears_afterwards(tmp_path, executed):
    ctx, _ = _ctx(tmp_path, _child(tmp_path))
    _node().execute(ctx)
    _node().execute(ctx)
    assert len(executed.calls) == 2


def test_for_each_runs_a_stored_pipeline_too(tmp_path, executed):
    ctx, _ = _ctx(tmp_path, _child(tmp_path))
    ForEachPipelineNode(params={"_input_step_ids": ["in"], "pipeline_id": "child"}).execute(ctx)
    assert len(executed.calls) == 2
    assert all(wf.id == "child" for wf, _ in executed.calls)
