"""The step params the drafting prompt documents are the ones the nodes read.

The prompt told the model to write a sort as ``{columns, ascending}``. The node reads
``sort_by``, so a drafted sort was refused by the editor's checks, and did nothing if it
ran anyway. Unpivot had the same problem and failed outright.
"""

from __future__ import annotations

import re

import pytest

from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow
from revoluzen_flow.planner.ai_client import SYSTEM_PROMPT
from revoluzen_flow.planner.ir_from_json import planner_json_to_ir


@pytest.fixture
def csv(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("id,amount,tax\n1,10,1\n2,40,4\n3,20,2\n", encoding="utf-8")
    return path


def run_step(tmp_path, csv, step_type, params):
    workflow = Workflow(
        id="w",
        name="w",
        steps=[
            Step(id="src", type=StepType.SOURCE, label="s", params={"connector_type": "csv", "file_path": str(csv), "path": str(csv), "has_header": True}),
            Step(id="t", type=step_type, label="t", params=params),
        ],
        connections=[StepConnection(from_step="src", to_step="t")],
    )
    return WorkflowExecutor(data_dir=str(tmp_path), app_state={}).execute_workflow(workflow, full_run=True).step_results["t"]


def test_the_prompt_documents_the_sort_the_node_reads(tmp_path, csv):
    assert '"sort_by"' in re.search(r"- sort: (.*)", SYSTEM_PROMPT).group(1)
    result = run_step(tmp_path, csv, StepType.SORT, {"sort_by": ["amount DESC"]})
    assert [row["amount"] for row in result.sample_data] == [40, 20, 10]


def test_the_prompt_documents_the_unpivot_the_node_reads(tmp_path, csv):
    assert '"columns"' in re.search(r"- unpivot: (.*)", SYSTEM_PROMPT).group(1)
    result = run_step(tmp_path, csv, StepType.UNPIVOT, {"id_columns": ["id"], "columns": ["amount", "tax"], "name_column": "variable", "value_column": "value"})
    assert result.status == "success"
    assert result.row_count == 6


def test_a_model_using_the_old_sort_shape_gets_the_current_one():
    ir = planner_json_to_ir({
        "name": "n",
        "steps": [{"type": "sort", "label": "s", "params": {"columns": ["a", "b"], "ascending": [False]}}],
        "connections": [],
    })
    assert ir["steps"][0]["params"] == {"sort_by": ["a DESC", "b ASC"]}


def test_a_current_sort_is_left_alone():
    ir = planner_json_to_ir({"name": "n", "steps": [{"type": "sort", "params": {"sort_by": ["a"]}}], "connections": []})
    assert ir["steps"][0]["params"] == {"sort_by": ["a"]}


def test_a_derived_column_keeps_its_columns():
    params = {"columns": [{"name": "x", "expression": "a + 1"}]}
    ir = planner_json_to_ir({"name": "n", "steps": [{"type": "derived_column", "params": params}], "connections": []})
    assert ir["steps"][0]["params"] == params


def test_a_sort_already_saved_in_the_old_shape_now_sorts(tmp_path, csv):
    result = run_step(tmp_path, csv, StepType.SORT, {"columns": ["amount"], "ascending": [False]})
    assert [row["amount"] for row in result.sample_data] == [40, 20, 10]
