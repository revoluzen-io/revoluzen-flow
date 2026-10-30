"""A verdict is not an answer: what did it cost, and which step caused it?

Two gaps either side of a failed Execution Contract.

*Before* the verdict: "contract failed: output is empty" does not say
that the table your dashboards read was replaced with nothing. The
numbers that make that sentence — 0 written, 3 there before — exist
together only at the pre-commit gate; after the sink runs, the old count
is gone.

*After* the verdict: the clause names the symptom at the sink, and the
cause is upstream. Finding it meant reading every step's row count by
hand.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.contracts.chain import verify_chain
from revoluzen_flow.contracts.schema import (
    BlastRadius,
    ExecutionContract,
    Guarantee,
    GuaranteeType,
)
from revoluzen_flow.contracts.verify import verify_contract
from revoluzen_flow.ir.schema import StepRunResult, WorkflowRunResult

pytestmark = pytest.mark.unit


def _run(*steps, status="success", workflow_id="wf1"):
    """steps: (step_id, row_count) or (step_id, row_count, status)."""
    results = {}
    for s in steps:
        sid, rows = s[0], s[1]
        st = s[2] if len(s) > 2 else "success"
        results[sid] = StepRunResult(
            step_id=sid, status=st, row_count=rows, duration_ms=1.0)
    return WorkflowRunResult(
        workflow_id=workflow_id, status=status, step_results=results)


def _contract(*guarantees, cid="c1"):
    return ExecutionContract(id=cid, guarantees=list(guarantees))


NO_EMPTY = Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT,
                     description="the rollup is never empty")


# ── blast radius ───────────────────────────────────────────────────────

def test_a_destructive_replace_is_stated_in_rows_not_adjectives():
    b = BlastRadius(step_id="ro_sink", destination="default.orders_by_region",
                    mode="replace", rows_before=3, rows_written=0)
    assert b.destructive is True
    assert b.summary() == (
        "default.orders_by_region replaced with 0 rows, was 3")


def test_a_blocked_write_reports_what_survived():
    # The whole value of a gate is the reassurance. "We refused" without
    # "and your 3 rows are still there" leaves the operator checking.
    b = BlastRadius(step_id="ro_sink", destination="default.orders_by_region",
                    mode="replace", rows_before=3, rows_written=0, blocked=True)
    assert b.destructive is False
    assert "still has 3 rows" in b.summary()


def test_an_append_that_adds_nothing_is_not_a_loss():
    # Flagging a harmless no-op the same as a truncation trains people to
    # ignore the flag.
    b = BlastRadius(step_id="s", destination="t", mode="append",
                    rows_before=100, rows_written=0)
    assert b.destructive is False


def test_growing_a_table_is_not_a_loss():
    b = BlastRadius(step_id="s", destination="t", mode="replace",
                    rows_before=3, rows_written=900)
    assert b.destructive is False


def test_an_unreadable_prior_count_is_never_reported_as_zero():
    # "The table was already empty" would excuse exactly the loss this
    # exists to surface, so an unknown must stay unknown.
    b = BlastRadius(step_id="s", destination="t", mode="replace",
                    rows_before=None, rows_written=0)
    assert b.destructive is False
    assert "was" not in b.summary()


def test_blast_radius_rides_on_the_receipt():
    receipt = verify_contract(
        _contract(NO_EMPTY),
        _run(("src", 3), ("sink", 0)),
        blast_radius=[BlastRadius(step_id="sink", destination="default.t",
                                  mode="replace", rows_before=3,
                                  rows_written=0)],
    )
    assert receipt.verdict == "failed"
    assert len(receipt.blast_radius) == 1
    assert receipt.blast_radius[0].destructive is True
    assert "was 3" in receipt.render_markdown()


# ── attribution ────────────────────────────────────────────────────────

def test_the_step_that_dropped_the_rows_is_named():
    receipt = verify_contract(
        _contract(NO_EMPTY),
        _run(("ro_src", 8), ("ro_filter", 0), ("ro_sink", 0)),
    )
    assert receipt.verdict == "failed"
    cause = receipt.attribution[0]
    assert cause.step_id == "ro_filter"
    assert cause.upstream_step == "ro_src"
    assert cause.detail == "8 -> 0 at 'ro_filter'"


def test_the_biggest_collapse_wins_not_the_last_one():
    # Both steps lose rows; the filter is where the data went.
    receipt = verify_contract(
        _contract(Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=100)),
        _run(("src", 1000), ("filter", 5), ("dedupe", 3), ("sink", 3)),
    )
    assert receipt.attribution[0].step_id == "filter"
    assert receipt.attribution[0].rows_in == 1000


def test_an_empty_source_is_reported_as_such_not_as_a_lost_row():
    # Nothing collapsed — nothing ever arrived. That is a different fix
    # (look at the source) and saying so beats staying silent.
    receipt = verify_contract(
        _contract(NO_EMPTY), _run(("src", 0), ("sink", 0)))
    assert "no rows entered the pipeline" in receipt.attribution[0].detail
    assert receipt.attribution[0].step_id == "src"


def test_a_verified_run_has_nothing_to_attribute():
    receipt = verify_contract(
        _contract(NO_EMPTY), _run(("src", 8), ("sink", 8)))
    assert receipt.verdict == "verified"
    assert receipt.attribution == []


def test_a_schema_failure_is_not_blamed_on_a_row_count():
    # Where rows were lost has nothing to do with a missing column, and
    # pointing at a step would send the reader somewhere irrelevant.
    receipt = verify_contract(
        _contract(Guarantee(type=GuaranteeType.SCHEMA_CONTAINS,
                            columns=["region", "revenue"])),
        _run(("src", 8), ("sink", 8)),
    )
    assert receipt.verdict == "failed"
    assert receipt.attribution == []


def test_attribution_reaches_the_rendered_receipt():
    receipt = verify_contract(
        _contract(NO_EMPTY), _run(("ro_src", 8), ("ro_filter", 0)))
    assert "likely cause: 8 -> 0 at 'ro_filter'" in receipt.render_markdown()


# ── the chain keeps working ────────────────────────────────────────────

def test_a_receipt_carrying_the_new_fields_still_recomputes():
    contract = _contract(NO_EMPTY)
    r = verify_contract(
        contract, _run(("src", 8), ("sink", 0)),
        blast_radius=[BlastRadius(step_id="sink", destination="default.t",
                                  mode="replace", rows_before=3, rows_written=0)],
    )
    assert r.blast_radius and r.attribution  # the case under test
    out = verify_chain([r], contracts={contract.id: contract})
    assert out.ok, out.reason
    assert out.checked_hashes


def test_a_receipt_predating_the_new_fields_still_recomputes():
    """The compatibility requirement.

    These fields were added after receipts were already stored. Hashing
    them unconditionally would change the canonical form of every
    historical receipt, so a chain check would report the entire record
    as tampered on the day the feature shipped — destroying the meaning
    of the one alarm the chain exists to raise.
    """
    contract = _contract(NO_EMPTY)
    r = verify_contract(contract, _run(("src", 8), ("sink", 8)))
    assert r.blast_radius == [] and r.attribution == []

    # Byte-for-byte the pre-feature payload: the keys must be absent,
    # not present-and-empty.
    from revoluzen_flow.contracts.verify import _receipt_hash, _receipt_payload
    legacy = _receipt_hash({
        "contract": contract.model_dump(mode="json"),
        "run_id": r.run_id,
        "verdict": r.verdict,
        "clauses": [c.model_dump(mode="json") for c in r.clauses],
        "verified_at": r.verified_at,
        "prev_hash": None,
        "resumed_from": None,
        "provenance": r.provenance.model_dump(mode="json"),
    })
    assert r.receipt_hash == legacy

    payload = _receipt_payload(contract, r.run_id, r.verdict, r.clauses,
                               r.verified_at, None, None, r.provenance, [], [])
    assert "blast_radius" not in payload
    assert "attribution" not in payload


def test_guarantee_counts_are_scoped_and_counted(tmp_path):
    """Powers the Pipelines table's contract chip in one request.

    The first cut asked per pipeline, which is an N+1 that grows with
    the list; the counts must also stay inside the workspace, since the
    chip would otherwise reveal that a neighbour's pipeline exists.
    """
    from revoluzen_flow.contracts.store import ContractStore
    from revoluzen_flow.storage.database import Database

    db = Database(str(tmp_path / "t.db"))
    store = ContractStore(db)
    store.set_contract("wf-a", _contract(NO_EMPTY, cid="a"), workspace_id="mine")
    store.set_contract(
        "wf-b",
        _contract(NO_EMPTY, Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=1),
                  cid="b"),
        workspace_id="mine",
    )
    store.set_contract("wf-c", _contract(NO_EMPTY, cid="c"), workspace_id="theirs")

    assert store.guarantee_counts("mine") == {"wf-a": 1, "wf-b": 2}
    assert store.guarantee_counts("theirs") == {"wf-c": 1}
    assert store.guarantee_counts("nobody") == {}


def test_editing_a_blast_radius_after_the_fact_is_caught():
    # It is hashed, so it is evidence rather than decoration — someone
    # quietly changing "was 3" to "was 0" must break the chain.
    contract = _contract(NO_EMPTY)
    r = verify_contract(
        contract, _run(("src", 8), ("sink", 0)),
        blast_radius=[BlastRadius(step_id="sink", destination="default.t",
                                  mode="replace", rows_before=3, rows_written=0)],
    )
    r.blast_radius[0].rows_before = 0
    out = verify_chain([r], contracts={contract.id: contract})
    assert not out.ok
    assert "tampered" in out.reason
