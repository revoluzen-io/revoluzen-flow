"""A write refused by the contract names the step as people see it on the canvas."""

from __future__ import annotations

from revoluzen_flow.contracts.precommit import ContractBlockedWrite, PrecommitViolation
from revoluzen_flow.contracts.schema import ExecutionContract, Guarantee, GuaranteeType
from revoluzen_flow.contracts.verify import verify_contract
from revoluzen_flow.ir.schema import StepRunResult, WorkflowRunResult

EMPTY = PrecommitViolation(
    guarantee=GuaranteeType.NO_EMPTY_OUTPUT,
    description="Never write an empty table",
    detail="would write 0 rows",
)


def test_the_refusal_names_the_step_by_its_name_not_its_id():
    e = ContractBlockedWrite("b6bn99md", [EMPTY], step_label="sales.revenue_by_tier")
    assert str(e) == (
        "Execution Contract blocked the write at 'sales.revenue_by_tier': "
        "Never write an empty table (would write 0 rows). The destination was left unchanged."
    )
    assert e.step_id == "b6bn99md" and e.violations == [EMPTY]


def test_a_step_without_a_name_is_named_by_its_id():
    assert "at 'b6bn99md':" in str(ContractBlockedWrite("b6bn99md", [EMPTY]))


def test_the_refusal_carries_what_would_have_been_written():
    e = ContractBlockedWrite("sink", [EMPTY], row_count=0, columns=["tier", "revenue", "orders"])
    assert e.row_count == 0 and e.columns == ["tier", "revenue", "orders"]


def test_a_refused_empty_write_still_has_its_columns_on_the_receipt():
    # The sink refused 0 rows: emptiness is the broken guarantee, not the
    # columns, which the refused result had. They used to read as missing.
    contract = ExecutionContract(id="c1", default_output_step="sink", guarantees=[
        Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT, description="Never write an empty table"),
        Guarantee(type=GuaranteeType.SCHEMA_CONTAINS, description="The dashboard finds its columns",
                  columns=["tier", "revenue", "orders"]),
    ])
    run = WorkflowRunResult(workflow_id="wf1", status="error", step_results={
        "paid": StepRunResult(step_id="paid", status="success", row_count=0, columns=["tier", "revenue", "orders"]),
        "sink": StepRunResult(step_id="sink", status="error", row_count=0, columns=["tier", "revenue", "orders"],
                              error="Execution Contract blocked the write"),
    })
    clauses = {c.type: c for c in verify_contract(contract, run).clauses}
    assert clauses[GuaranteeType.NO_EMPTY_OUTPUT].passed is False
    assert clauses[GuaranteeType.SCHEMA_CONTAINS].passed is True
    assert clauses[GuaranteeType.SCHEMA_CONTAINS].detail == "schema compatible"
