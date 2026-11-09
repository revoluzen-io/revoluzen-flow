"""A mapped_keys join with unusable pairs must fail, not cross-join.

`nodes/join.py` built its ON clause from each pair's `left` and `right`
and fell through to `ON TRUE` when no pair produced one. A pipeline whose
pairs used any other field name — `left_key`/`right_key`, say — therefore
matched every row against every row and returned a wrong answer with no
error at all. Found by a recorded model making exactly that mistake; it
is a correctness bug for a hand-written pipeline too.
"""

from __future__ import annotations

import csv

import pytest

from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow


@pytest.fixture
def orders_and_customers(tmp_path):
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "customer_id"])
        w.writerow([1, "c1"])
        w.writerow([2, "c2"])
    with (tmp_path / "customers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cust_id", "customer_name"])
        w.writerow(["c1", "Acme"])
        w.writerow(["c2", "Globex"])
        w.writerow(["c3", "Initech"])
    return tmp_path


def _workflow(key_pairs):
    return Workflow(
        id="wf-mapped", name="Mapped", workspace_id="default",
        steps=[
            Step(id="orders", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            Step(id="cust", type=StepType.CSV_SOURCE, label="Customers",
                 params={"file_path": "customers.csv"}),
            Step(id="named", type=StepType.JOIN, label="Add name",
                 params={"key_mode": "mapped_keys", "join_type": "INNER",
                         "left_input_id": "orders", "key_pairs": key_pairs}),
        ],
        connections=[
            StepConnection(from_step="orders", to_step="named"),
            StepConnection(from_step="cust", to_step="named"),
        ],
    )


def _run(workflow, data_dir):
    executor = WorkflowExecutor(data_dir=str(data_dir), app_state={})
    return executor.execute_step_trace(workflow, "named", preview_limit=50)


def test_correct_pairs_join_on_the_key(orders_and_customers):
    workflow = _workflow([{"left": "customer_id", "right": "cust_id"}])
    result = _run(workflow, orders_and_customers).step_results["named"]
    assert result.status == "success", result.error
    # Two orders, each matching one customer. Not 2 x 3.
    assert result.row_count == 2


def test_pairs_with_the_wrong_field_names_fail_rather_than_cross_join(
    orders_and_customers,
):
    workflow = _workflow([{"left_key": "customer_id", "right_key": "cust_id"}])
    result = _run(workflow, orders_and_customers).step_results["named"]
    assert result.status == "error", (
        f"expected a failure, got {result.row_count} rows — a silent cross join"
    )
    assert "left" in (result.error or "") and "right" in (result.error or "")


def test_the_error_names_the_field_names_that_were_used(orders_and_customers):
    workflow = _workflow([{"left_key": "customer_id", "right_key": "cust_id"}])
    result = _run(workflow, orders_and_customers).step_results["named"]
    assert "left_key" in (result.error or "")


def test_pairs_with_an_empty_side_fail(orders_and_customers):
    workflow = _workflow([{"left": "customer_id", "right": ""}])
    result = _run(workflow, orders_and_customers).step_results["named"]
    assert result.status == "error"
