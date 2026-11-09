"""A broken step must not hide the broken step after it.

`two-faults` in the repair evaluation renames two columns, so two steps
need editing. Every recorded model fixed the first and stopped. The
recording shows why: the agent called `validate_pipeline` once, up front,
and was told about ONE fault.

The reason is in how input columns were resolved. `_upstream_columns`
previews the pipeline and reads what each step produced; while the filter
is broken it produces nothing, so the aggregate downstream of it resolved
to no columns and the column check skipped it — correctly refusing to
report every reference as missing, but leaving the second fault invisible.

A filter does not change the column set. Neither does a sort, a sample or
a quality check. So when a step's own input cannot be resolved, the
columns can be carried down from the nearest ancestor that did resolve,
as long as every step in between is one of those column-preserving kinds.
"""

from __future__ import annotations

import asyncio
import csv

import pytest

from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

PIPELINE_ID = "wf-two-faults"


@pytest.fixture
def two_faults(tmp_path, workflow_store, monkeypatch):
    """The evaluation's `two-faults` pipeline: amount and region renamed."""
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sales_region", "order_amount"])
        writer.writerow(["EMEA", 200])
        writer.writerow(["APAC", 50])

    workflow = Workflow(
        id=PIPELINE_ID, name="Two faults", workspace_id="default",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            # Fault 1 — the source renamed amount to order_amount.
            Step(id="big", type=StepType.FILTER, label="Large orders",
                 params={"mode": "expression", "condition": "amount > 100"}),
            # Fault 2 — and region to sales_region.
            Step(id="totals", type=StepType.AGGREGATE, label="Totals by region",
                 params={"group_by": ["region"],
                         "functions": [{"column": "amount", "function": "SUM",
                                        "alias": "total"}]}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="big"),
            StepConnection(from_step="big", to_step="totals"),
        ],
    )
    workflow_store.save(workflow, change_summary="test", created_by="test")
    return workflow.id


def _validate(**inputs):
    from revoluzen_flow.ai.tools.validate_pipeline import DEFINITION

    ctx = ToolContext(
        tenant_id="default", user_id="u-1", workspace_id="default",
        environment="dev", selected_ids=(PIPELINE_ID,),
    )
    return asyncio.run(DEFINITION.handler(inputs, ctx))


def test_both_faults_are_reported_in_one_call(two_faults):
    """The whole point: one validate call has to surface both edits."""
    out = _validate()
    flagged = {e["step_id"] for e in out["errors"]}
    assert "big" in flagged, "the filter's renamed column"
    assert "totals" in flagged, "the aggregate's renamed column, behind the broken filter"


def test_the_aggregate_error_names_the_column_it_cannot_find(two_faults):
    out = _validate()
    totals = [e for e in out["errors"] if e["step_id"] == "totals"]
    assert totals, "the aggregate must be reported"
    assert any("region" in e["message"] for e in totals)


def test_the_aggregate_error_offers_the_real_column(two_faults):
    out = _validate()
    totals = " ".join(e["message"] for e in out["errors"] if e["step_id"] == "totals")
    assert "sales_region" in totals


def test_an_aggregate_function_column_is_checked_too(two_faults):
    """`functions: [{column: amount}]` is the second half of fault two."""
    out = _validate()
    totals = " ".join(e["message"] for e in out["errors"] if e["step_id"] == "totals")
    assert "amount" in totals


def test_fixing_only_the_filter_still_reports_the_aggregate(two_faults, workflow_store):
    """What the model actually did. It must not read as done."""
    saved = workflow_store.get(PIPELINE_ID, workspace_id="default").workflow
    for step in saved.steps:
        if step.id == "big":
            step.params = {**step.params, "condition": "order_amount > 100"}
    workflow_store.save(saved, change_summary="fix one", created_by="test")

    out = _validate()
    assert out["valid"] is False
    assert any(e["step_id"] == "totals" for e in out["errors"])


def test_fixing_both_makes_it_valid(two_faults, workflow_store):
    saved = workflow_store.get(PIPELINE_ID, workspace_id="default").workflow
    for step in saved.steps:
        if step.id == "big":
            step.params = {**step.params, "condition": "order_amount > 100"}
        if step.id == "totals":
            step.params = {
                "group_by": ["sales_region"],
                "functions": [{"column": "order_amount", "function": "SUM",
                               "alias": "total"}],
            }
    workflow_store.save(saved, change_summary="fix both", created_by="test")

    out = _validate()
    assert out["valid"] is True, out["errors"]


def test_columns_are_not_carried_through_a_step_that_reshapes_them(
    tmp_path, workflow_store, monkeypatch,
):
    """An aggregate changes the columns, so nothing downstream may assume them.

    Carrying a source's columns past a step that rewrites the schema would
    report references to perfectly real columns as missing.
    """
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sales_region", "order_amount"])
        writer.writerow(["EMEA", 200])

    workflow = Workflow(
        id="wf-reshape", name="Reshape", workspace_id="default",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            # Broken, so nothing downstream resolves by preview.
            Step(id="agg", type=StepType.AGGREGATE, label="Totals",
                 params={"group_by": ["nope"],
                         "functions": [{"column": "order_amount",
                                        "function": "SUM", "alias": "total"}]}),
            # `total` is produced BY the aggregate. It is not in the source,
            # so a naive carry-down would call it missing.
            Step(id="big", type=StepType.FILTER, label="Big totals",
                 params={"mode": "expression", "condition": "total > 100"}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="agg"),
            StepConnection(from_step="agg", to_step="big"),
        ],
    )
    workflow_store.save(workflow, change_summary="test", created_by="test")

    out = asyncio.run(_handler_for("wf-reshape"))
    flagged = {e["step_id"] for e in out["errors"]}
    assert "agg" in flagged, "the aggregate's own bad group_by is real"
    assert "big" not in flagged, "must not claim `total` is missing"


def _handler_for(pipeline_id: str):
    from revoluzen_flow.ai.tools.validate_pipeline import DEFINITION

    return DEFINITION.handler(
        {"pipeline_id": pipeline_id},
        ToolContext(tenant_id="default", user_id="u", workspace_id="default",
                    environment="dev"),
    )
