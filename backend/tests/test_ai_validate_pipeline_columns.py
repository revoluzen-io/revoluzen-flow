"""validate_pipeline: drafts, and columns that do not exist.

It reported "valid" for a pipeline whose filter names a column the source
renamed — the exact fault behind the evaluation's renamed-column cases —
and it could not be pointed at a draft, so validating after an edit
reported on the version without the edit in it.
"""

from __future__ import annotations

import asyncio
import csv

import pytest

from revoluzen_flow.ai.column_refs import columns_in_expression, missing_columns
from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ai.tools.modify_pipeline_step import DEFINITION as MODIFY
from revoluzen_flow.ai.tools.validate_pipeline import DEFINITION
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

PIPELINE_ID = "wf-validate"


# ── The reference extractor, on its own ───────────────────────────────────

def test_an_expression_yields_its_column_references():
    assert columns_in_expression("amount > 100") == {"amount"}


def test_keywords_literals_and_numbers_are_not_columns():
    found = columns_in_expression("account_type <> 'test' AND amount >= 10")
    assert found == {"account_type", "amount"}


def test_a_function_name_is_not_a_column():
    found = columns_in_expression("upper(customer_name) = 'ACME'")
    assert found == {"customer_name"}


def test_structured_params_are_read_exactly():
    assert missing_columns({"join_key": ["customer_id"]}, ["order_id"]) == ["customer_id"]
    assert missing_columns({"join_key": ["customer_id"]}, ["customer_id"]) == []


def test_key_pairs_are_read_from_their_dicts():
    params = {"key_mode": "mapped_keys",
              "key_pairs": [{"left": "customer_id", "right": "cust_id"}]}
    assert missing_columns(params, ["customer_id"]) == ["cust_id"]


def test_a_rule_names_its_column():
    params = {"rules": [{"column": "amount", "op": "gte", "value": 0}]}
    assert missing_columns(params, ["order_amount"]) == ["amount"]


# ── Through the tool ──────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, workflow_store, monkeypatch):
    """The source calls it order_amount; the filter asks for amount."""
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "order_amount"])
        writer.writerow([1, 500])

    workflow = Workflow(
        id=PIPELINE_ID,
        name="Renamed",
        workspace_id="default",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            Step(id="big", type=StepType.FILTER, label="Large",
                 params={"mode": "expression", "condition": "amount > 100"}),
        ],
        connections=[StepConnection(from_step="src", to_step="big")],
    )
    workflow_store.save(workflow, change_summary="test", created_by="test")
    return tmp_path


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id="default", user_id="u-1", workspace_id="default",
        environment="dev", selected_ids=(PIPELINE_ID,),
        user_intent="Is this pipeline ready?",
    )


def _validate(**inputs):
    return asyncio.run(DEFINITION.handler(inputs, _ctx()))


def test_a_renamed_column_is_now_an_error(app):
    """Used to report valid=True for a pipeline that fails on row one."""
    out = _validate()
    assert out["columns_checked"] is True
    assert out["valid"] is False
    assert any("amount" in e["message"] for e in out["errors"])


def test_the_error_names_the_columns_that_do_exist(app):
    out = _validate()
    assert any("order_amount" in e["message"] for e in out["errors"])


def test_a_correct_pipeline_is_still_valid(app, workflow_store):
    saved = workflow_store.get(PIPELINE_ID, workspace_id="default").workflow
    for step in saved.steps:
        if step.id == "big":
            step.params = {**step.params, "condition": "order_amount > 100"}
    workflow_store.save(saved, change_summary="fix", created_by="test")
    out = _validate()
    assert out["valid"] is True, out["errors"]


def test_a_draft_can_be_validated(app):
    """The edit is in the draft, not in the saved pipeline."""
    edit = asyncio.run(MODIFY.handler(
        {
            "op": "reconfigure",
            "args": {"step_id": "big", "params": {"condition": "order_amount > 100"}},
            "idempotency_key": "safe_write.u-1.modify.big.1.0.0",
        },
        _ctx(),
    ))
    out = _validate(draft_id=edit["draft_id"])
    assert out["draft_id"] == edit["draft_id"]
    assert out["valid"] is True, out["errors"]
    # ...while the saved pipeline is still broken.
    assert _validate()["valid"] is False


def test_a_draft_that_does_not_fix_it_still_reports_the_column(app, monkeypatch):
    # The edit itself is a guess the tool refuses. Validation must not lean on that.
    from revoluzen_flow.ai.tools import modify_pipeline_step

    async def no_reason(*_a):
        return ""

    monkeypatch.setattr(modify_pipeline_step, "guess_reason", no_reason)
    edit = asyncio.run(MODIFY.handler(
        {
            "op": "reconfigure",
            "args": {"step_id": "big", "params": {"condition": "amuont > 100"}},
            "idempotency_key": "safe_write.u-1.modify.big.1.0.1",
        },
        _ctx(),
    ))
    out = _validate(draft_id=edit["draft_id"])
    assert out["valid"] is False
    assert any("amuont" in e["message"] for e in out["errors"])


def test_a_vanished_draft_says_so(app):
    out = _validate(draft_id="wf-draft-gone")
    assert out["valid"] is False
    assert "no longer exists" in out["message"]


def test_an_unreadable_schema_is_admitted_not_hidden(workflow_store, monkeypatch):
    """No data dir: say the column check did not run, do not claim valid."""
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", "")
    workflow = Workflow(
        id="wf-noschema", name="No schema", workspace_id="default",
        steps=[Step(id="src", type=StepType.CSV_SOURCE, label="S",
                    params={"file_path": "nope.csv"})],
        connections=[],
    )
    workflow_store.save(workflow, change_summary="t", created_by="t")
    out = asyncio.run(DEFINITION.handler(
        {"pipeline_id": "wf-noschema"},
        ToolContext(tenant_id="default", user_id="u", workspace_id="default",
                    environment="dev"),
    ))
    assert out["columns_checked"] is False
    assert "still fail on the first row" in out["message"]
