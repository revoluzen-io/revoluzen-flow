"""test_pipeline_draft: run a draft, report the result, write nothing.

The tool exists because of the second finding in `docs/repair-evaluation.md`
— no recorded model completed the `two-faults` case, because the engine
error names only the first broken step and the agent could not run its own
draft to discover the second.
"""

from __future__ import annotations

import asyncio
import csv

import pytest

from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ai.tools.draft_pipeline_from_intent import store_draft
from revoluzen_flow.ai.tools.modify_pipeline_step import DEFINITION as MODIFY
from revoluzen_flow.ai.tools.test_pipeline_draft import (
    DEFINITION,
    FAILED,
    NOT_RUN,
    PASSED,
    get_test_evidence,
)
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

SECRET_VALUE = "row-value-that-must-not-leave"
PIPELINE_ID = "wf-draft-test"


@pytest.fixture
def app(tmp_path, workflow_store, monkeypatch):
    """A pipeline with TWO faults: both filters name the old column."""
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "order_amount", "account_type", "note"])
        writer.writerow([1, 500, "customer", SECRET_VALUE])
        writer.writerow([2, 5, "test", SECRET_VALUE])

    workflow = Workflow(
        id=PIPELINE_ID,
        name="Two faults",
        workspace_id="default",
        steps=[
            Step(id="src", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            Step(id="big", type=StepType.FILTER, label="Large",
                 params={"mode": "expression", "condition": "amount > 100"}),
            Step(id="real", type=StepType.FILTER, label="Real accounts",
                 params={"mode": "expression", "condition": "account <> 'test'"}),
            Step(id="out", type=StepType.CSV_SINK, label="Out",
                 params={"file_path": "out.csv"}),
        ],
        connections=[
            StepConnection(from_step="src", to_step="big"),
            StepConnection(from_step="big", to_step="real"),
            StepConnection(from_step="real", to_step="out"),
        ],
    )
    workflow_store.save(workflow, change_summary="test", created_by="test")
    return tmp_path


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id="default", user_id="u-1", workspace_id="default",
        environment="dev", selected_ids=(PIPELINE_ID,),
        user_intent="This pipeline is failing. Find the cause and fix it.",
    )


def _test_draft(draft_id):
    return asyncio.run(DEFINITION.handler({"draft_id": draft_id}, _ctx()))


def _edit(step_id, params, draft_id=None):
    inputs = {
        "op": "reconfigure",
        "args": {"step_id": step_id, "params": params},
        "idempotency_key": f"safe_write.u-1.modify.{step_id}.1.0.0",
    }
    if draft_id:
        inputs["draft_id"] = draft_id
    return asyncio.run(MODIFY.handler(inputs, _ctx()))


def test_a_draft_that_still_fails_reports_the_next_fault(app):
    """One fix of two: the tool names the step that is still broken."""
    first = _edit("big", {"condition": "order_amount > 100"})
    out = _test_draft(first["draft_id"])
    assert out["verdict"] == FAILED
    assert out["failures"], "the second fault must be reported"
    assert out["failures"][0]["step_id"] == "real"


def test_chaining_both_fixes_passes(app):
    """The loop the tool exists to make possible."""
    first = _edit("big", {"condition": "order_amount > 100"})
    assert _test_draft(first["draft_id"])["verdict"] == FAILED
    second = _edit("real", {"condition": "account_type <> 'test'"},
                   draft_id=first["draft_id"])
    out = _test_draft(second["draft_id"])
    assert out["verdict"] == PASSED, out["message"]


def test_no_row_values_are_returned(app):
    first = _edit("big", {"condition": "order_amount > 100"})
    second = _edit("real", {"condition": "account_type <> 'test'"},
                   draft_id=first["draft_id"])
    out = _test_draft(second["draft_id"])
    assert SECRET_VALUE not in repr(out)


def test_columns_and_counts_are_returned(app):
    first = _edit("big", {"condition": "order_amount > 100"})
    second = _edit("real", {"condition": "account_type <> 'test'"},
                   draft_id=first["draft_id"])
    out = _test_draft(second["draft_id"])
    by_id = {s["step_id"]: s for s in out["steps"]}
    assert "order_amount" in by_id["big"]["columns"]
    assert isinstance(by_id["big"]["row_count"], int)


def test_the_saved_pipeline_is_untouched(app, workflow_store):
    first = _edit("big", {"condition": "order_amount > 100"})
    _test_draft(first["draft_id"])
    saved = workflow_store.get(PIPELINE_ID, workspace_id="default")
    conditions = {s.id: (s.params or {}).get("condition") for s in saved.workflow.steps}
    assert conditions["big"] == "amount > 100", "testing a draft must not save it"


def test_no_destination_file_is_written(app):
    first = _edit("big", {"condition": "order_amount > 100"})
    second = _edit("real", {"condition": "account_type <> 'test'"},
                   draft_id=first["draft_id"])
    assert _test_draft(second["draft_id"])["verdict"] == PASSED
    assert not (app / "out.csv").exists(), "a sink must never run"


def test_a_draft_that_moves_a_source_is_refused(app):
    """Model-written text may not repoint where the pipeline reads."""
    draft = store_draft({
        "name": "Two faults",
        "_modification_of": PIPELINE_ID,
        "steps": [
            {"id": "src", "type": "csv_source",
             "params": {"file_path": "../../etc/passwd"}},
            {"id": "big", "type": "filter",
             "params": {"mode": "expression", "condition": "order_amount > 100"}},
        ],
        "connections": [{"from_step": "src", "to_step": "big"}],
    })
    out = _test_draft(draft)
    assert out["verdict"] == NOT_RUN
    assert "source step" in out["message"]


def test_a_draft_with_a_file_reading_expression_is_refused(app, workflow_store):
    """Only the filter changes, so the SQL rule is the one that must fire.

    Built from the SAVED ir rather than hand-written: the store migrates
    `csv_source` to `source` + `connector_type`, and a hand-written source
    step then trips the "source differs" rule first — a correct refusal,
    but not the one under test.
    """
    saved = workflow_store.get(PIPELINE_ID, workspace_id="default").workflow.model_dump()
    steps = [dict(s) for s in saved["steps"] if s["id"] in ("src", "big")]
    for step in steps:
        if step["id"] == "big":
            step["params"] = {
                **step["params"],
                "condition": "order_amount > (SELECT max(x) FROM read_csv('/etc/hosts'))",
            }
    draft = store_draft({
        "name": saved["name"],
        "_modification_of": PIPELINE_ID,
        "steps": steps,
        "connections": [{"from_step": "src", "to_step": "big"}],
    })
    out = _test_draft(draft)
    assert out["verdict"] == NOT_RUN
    assert "outside the pipeline" in out["message"]


def test_a_missing_draft_says_so_rather_than_raising(app):
    out = _test_draft("wf-draft-nope")
    assert out["verdict"] == NOT_RUN
    assert "no longer exists" in out["message"]


# ── Evidence ──────────────────────────────────────────────────────────────

def test_evidence_is_recorded_for_the_draft_that_ran(app):
    first = _edit("big", {"condition": "order_amount > 100"})
    _test_draft(first["draft_id"])
    evidence = get_test_evidence(first["draft_id"])
    assert evidence is not None
    assert evidence["verdict"] == FAILED


def test_evidence_does_not_survive_a_later_edit_of_the_draft(app):
    """A pass must not carry forward onto a draft that then changed."""
    first = _edit("big", {"condition": "order_amount > 100"})
    second = _edit("real", {"condition": "account_type <> 'test'"},
                   draft_id=first["draft_id"])
    assert _test_draft(second["draft_id"])["verdict"] == PASSED
    # Edit the tested draft again; its recorded pass no longer describes it.
    third = _edit("real", {"condition": "account_type <> 'test' OR account_type IS NULL"},
                  draft_id=second["draft_id"])
    assert get_test_evidence(third["draft_id"]) is None


def test_evidence_is_absent_for_a_draft_never_tested(app):
    first = _edit("big", {"condition": "order_amount > 100"})
    assert get_test_evidence(first["draft_id"]) is None
