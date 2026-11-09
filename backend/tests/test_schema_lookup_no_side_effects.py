"""Opening a step in the editor must not do anything outside the run.

The column pickers ask for the columns flowing into a step, and that lookup ran the
upstream steps for real. So opening a step downstream of a sink wrote that sink's file
again, with any parameter still unresolved in its name.
"""

from __future__ import annotations

import os

import pytest

from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow


@pytest.fixture
def data(tmp_path):
    (tmp_path / "in.csv").write_text("region,revenue\nAPAC,10\nEMEA,20\n", encoding="utf-8")
    return tmp_path


def pipeline(data, **extra) -> Workflow:
    return Workflow(
        id="wf1",
        name="Lookup",
        steps=[
            Step(id="src", type=StepType.SOURCE, label="Source",
                 params={"connector_type": "csv", "file_path": str(data / "in.csv"), "path": str(data / "in.csv"), "has_header": True}),
            Step(id="keep", type=StepType.FILTER, label="One region", params={"condition": "region = '${param.region}'"}),
            Step(id="out", type=StepType.DESTINATION, label="Write",
                 params={"connector_type": "csv", "file_path": "outputs/revenue_${param.region}.csv"}),
            Step(id="after", type=StepType.SORT, label="After the write", params={"columns": ["region"]}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="keep"),
            StepConnection(from_step="keep", to_step="out"),
            StepConnection(from_step="out", to_step="after"),
        ],
        **extra,
    )


def written(data):
    folder = data / "outputs"
    return os.listdir(folder) if folder.exists() else []


def test_looking_up_columns_downstream_of_a_sink_writes_nothing(data):
    executor = WorkflowExecutor(data_dir=str(data), app_state={})
    schema = executor.get_step_input_schema(pipeline(data), "after")
    assert written(data) == []
    assert "error" not in schema
    names = [c["name"] for c in schema["inputs"][0]["schema"]["columns"]]
    assert names == ["region", "revenue"]


def test_a_declared_default_reaches_the_lookup(data):
    declared = [{"name": "region", "type": "string", "default": "EMEA", "description": "", "required": False}]
    executor = WorkflowExecutor(data_dir=str(data), app_state={})
    schema = executor.get_step_input_schema(pipeline(data, parameters=declared), "after")
    assert "error" not in schema
    assert written(data) == []


def test_a_real_run_still_writes(data):
    declared = [{"name": "region", "type": "string", "default": "EMEA", "description": "", "required": False}]
    executor = WorkflowExecutor(data_dir=str(data), app_state={})
    result = executor.execute_workflow(pipeline(data, parameters=declared), full_run=True, parameter_values={"region": "APAC"})
    assert result.status == "success"
    assert written(data) == ["revenue_APAC.csv"]
