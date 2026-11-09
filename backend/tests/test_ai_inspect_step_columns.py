"""inspect_step_columns: names and types only, and never a side effect."""

from __future__ import annotations

import asyncio
import csv
import json

import pytest

from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ai.tools.inspect_step_columns import DEFINITION
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

SECRET_VALUE = "row-value-that-must-not-leave"


@pytest.fixture
def app(tmp_path, workflow_store, monkeypatch):
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "order_amount", "note"])
        writer.writerow([1, 150, SECRET_VALUE])
    return workflow_store


def _save(store, steps, connections, workflow_id="wf-columns"):
    workflow = Workflow(
        id=workflow_id,
        name="Columns",
        workspace_id="default",
        steps=steps,
        connections=[StepConnection(from_step=a, to_step=b) for a, b in connections],
    )
    store.save(workflow, change_summary="test", created_by="test")
    return workflow.id


def _ctx(workflow_id):
    return ToolContext(
        tenant_id="default",
        user_id="u-1",
        workspace_id="default",
        environment="dev",
        selected_ids=(workflow_id,),
    )


def _call(workflow_id, **inputs):
    return asyncio.run(DEFINITION.handler(inputs, _ctx(workflow_id)))


def _source():
    return Step(id="src", type=StepType.CSV_SOURCE, label="Orders", params={"file_path": "orders.csv"})


def _broken_filter():
    return Step(id="big", type=StepType.FILTER, label="Large", params={"mode": "expression", "condition": "amount > 1"})


def test_lists_what_a_step_receives_even_when_that_step_is_broken(app):
    workflow_id = _save(app, [_source(), _broken_filter()], [("src", "big")])
    out = _call(workflow_id, step_id="big")
    assert out["available"] is True
    names = [c["name"] for c in out["columns"][0]["columns"]]
    assert names == ["order_id", "order_amount", "note"]
    assert all(c["type"] for c in out["columns"][0]["columns"])


def test_no_row_value_ever_appears_in_the_result(app):
    workflow_id = _save(app, [_source(), _broken_filter()], [("src", "big")])
    assert SECRET_VALUE not in json.dumps(_call(workflow_id, step_id="big"))
    assert SECRET_VALUE not in json.dumps(_call(workflow_id))


def test_without_a_step_it_returns_the_source_columns_and_step_ids(app):
    workflow_id = _save(app, [_source(), _broken_filter()], [("src", "big")])
    out = _call(workflow_id)
    assert out["columns"][0]["step_id"] == "src"
    assert {s["step_id"] for s in out["steps"]} == {"src", "big"}


def test_a_step_with_a_side_effect_upstream_is_never_run(app, monkeypatch):
    from revoluzen_flow.engine import executor as executor_module

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("the executor ran a step with a side effect")

    monkeypatch.setattr(executor_module.WorkflowExecutor, "execute_step_trace", must_not_run)
    send = Step(id="send", type=StepType.HTTP_REQUEST, label="Send", params={"url": "https://example.invalid"})
    workflow_id = _save(app, [_source(), send, _broken_filter()], [("src", "send"), ("send", "big")])
    out = _call(workflow_id, step_id="big")
    assert out["available"] is False
    assert "send" in out["message"]
    assert out["columns"] == []


@pytest.mark.parametrize("step_type", [StepType.KAFKA_SOURCE, StepType.API_SOURCE, StepType.CDC_SOURCE])
def test_a_source_that_does_more_than_read_is_not_run(app, step_type, monkeypatch):
    from revoluzen_flow.engine import executor as executor_module

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("the executor ran an active source")

    monkeypatch.setattr(executor_module.WorkflowExecutor, "execute_step_trace", must_not_run)
    source = Step(id="src", type=step_type, label="Feed", params={})
    workflow_id = _save(app, [source, _broken_filter()], [("src", "big")])
    assert _call(workflow_id, step_id="big")["available"] is False


def test_a_missing_file_reports_without_leaking_the_install_path(app, tmp_path):
    gone = Step(id="src", type=StepType.CSV_SOURCE, label="Gone", params={"file_path": "gone.csv"})
    workflow_id = _save(app, [gone, _broken_filter()], [("src", "big")])
    out = _call(workflow_id, step_id="big")
    text = json.dumps(out)
    assert out["columns"][0]["columns"] == []
    assert "gone.csv" in text
    assert str(tmp_path) not in out["columns"][0].get("error", "")


def test_an_unknown_step_or_pipeline_is_reported_plainly(app):
    workflow_id = _save(app, [_source()], [])
    assert _call(workflow_id, step_id="nope")["available"] is False
    assert _call("wf-does-not-exist")["available"] is False


def test_another_workspace_cannot_read_the_columns(app):
    workflow_id = _save(app, [_source()], [])
    ctx = ToolContext(
        tenant_id="other", user_id="u-2", workspace_id="other", environment="dev", selected_ids=(workflow_id,),
    )
    out = asyncio.run(DEFINITION.handler({}, ctx))
    assert out["available"] is False
    assert out["columns"] == []


def test_every_answer_satisfies_the_declared_output_schema(app):
    from revoluzen_flow.ai.normalize import normalize_tool_output, register_output_schema

    register_output_schema(DEFINITION.name, DEFINITION.output_schema)
    workflow_id = _save(app, [_source()], [])
    for inputs in ({}, {"step_id": "nope"}):
        normalize_tool_output(DEFINITION.name, _call(workflow_id, **inputs))
