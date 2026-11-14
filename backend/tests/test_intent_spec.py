"""The intent spec, the contract made from it, and the publish gate.

A spec says how to tell a pipeline worked. Its criteria that name a check
become Execution Contract guarantees; every receipt names the spec version
it was judged against; and a version whose last real run broke its contract
does not go live as it stands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from revoluzen_flow.contracts.chain import verify_chain
from revoluzen_flow.contracts.facts import destination_confirmed, measure_contract_facts
from revoluzen_flow.contracts.from_spec import SpecHasNoChecks, contract_from_spec
from revoluzen_flow.contracts.gate import ContractBroken, check_publish, last_verdict_for
from revoluzen_flow.contracts.schema import (
    BlastRadius,
    ContractVerification,
    ExecutionContract,
    Guarantee,
    GuaranteeType,
)
from revoluzen_flow.contracts.verify import (
    _receipt_hash,
    _receipt_payload,
    contract_digest,
    verify_contract,
)
from revoluzen_flow.ir.schema import StepRunResult, Workflow, WorkflowRunResult
from revoluzen_flow.ir.spec import PipelineSpec, SpecCriterion, spec_digest, spec_problems
from revoluzen_flow.ir.versioning import definition_hash

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


# ── The spec itself ─────────────────────────────────────────────────────

def test_spec_digest_ignores_who_saved_it_and_when():
    spec = PipelineSpec(business_owner="Finance", acceptance=[SpecCriterion(id="a", text="never empty", check="no_empty_output")])
    first = spec_digest("Daily orders for finance", spec)
    resaved = spec.model_copy(update={"updated_at": "2026-11-11T00:00:00Z", "updated_by": "priya"})
    assert spec_digest("Daily orders for finance", resaved) == first
    assert spec_digest("A different goal", spec) != first
    assert spec_digest("", None) is None


def test_spec_problems_name_unknown_checks_and_negative_numbers():
    spec = PipelineSpec(acceptance=[
        SpecCriterion(id="a", check="bogus"),
        SpecCriterion(id="a", check="row_count_min", min_rows=-1),
    ])
    problems = spec_problems(spec)
    assert any("'bogus' is not a check" in p for p in problems)
    assert any("used twice" in p for p in problems)
    assert any("min rows cannot be negative" in p for p in problems)


def test_a_workflow_round_trips_its_spec_and_the_spec_is_not_part_of_what_runs():
    wf = Workflow(name="orders", spec=PipelineSpec(outcome="finance closes by 9am"))
    again = Workflow.model_validate(wf.model_dump(mode="json"))
    assert again.spec.outcome == "finance closes by 9am"
    # Editing the spec must not count as a change to publish or invalidate a test.
    assert definition_hash(again) == definition_hash(Workflow(id=wf.id, name="orders"))


# ── Spec to contract ────────────────────────────────────────────────────

def _spec():
    return PipelineSpec(acceptance=[
        SpecCriterion(id="c1", text="One row per order", check="no_duplicate_key", key="order_id"),
        SpecCriterion(id="c2", text="At least 3 regions", check="row_count_min", min_rows=3),
        SpecCriterion(id="c3", text="Finance signs it off", check=""),
        SpecCriterion(id="c4", text="Fresh", check="freshness_max_age", time_column="ts"),  # no max age
    ])


def test_criteria_with_a_check_become_guarantees_and_name_their_criterion():
    contract, summary = contract_from_spec(
        workflow_id="wf1", workflow_name="Orders", goal="orders for finance",
        spec=_spec(), spec_version=4, existing=None,
    )
    assert [g.type for g in contract.guarantees] == [GuaranteeType.NO_DUPLICATE_KEY, GuaranteeType.ROW_COUNT_MIN]
    assert [g.criterion for g in contract.guarantees] == ["c1", "c2"]
    assert contract.guarantees[0].description == "One row per order"
    assert contract.spec_version == 4
    assert contract.spec_digest == spec_digest("orders for finance", _spec())
    assert summary["checked_by_people"] == [{"criterion": "c3", "text": "Finance signs it off"}]
    assert summary["incomplete"][0]["criterion"] == "c4"
    assert "max age seconds" in summary["incomplete"][0]["reason"]


def test_making_the_contract_again_keeps_hand_written_guarantees_and_replaces_spec_ones():
    existing = ExecutionContract(
        id="kept-id", name="Kept name", default_output_step="out",
        guarantees=[
            Guarantee(type=GuaranteeType.ALL_STEPS_SUCCEEDED, description="by hand"),
            Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=1, criterion="old"),
        ],
    )
    contract, summary = contract_from_spec(
        workflow_id="wf1", workflow_name="Orders", goal="", spec=_spec(),
        spec_version=5, existing=existing,
    )
    assert contract.id == "kept-id" and contract.name == "Kept name"
    assert contract.default_output_step == "out"
    assert [g.criterion for g in contract.guarantees] == ["c1", "c2", None]
    assert summary["kept"] == 1 and summary["from_spec"] == 2


def test_a_spec_with_nothing_to_check_makes_no_contract():
    with pytest.raises(SpecHasNoChecks):
        contract_from_spec(
            workflow_id="wf1", workflow_name="Orders", goal="",
            spec=PipelineSpec(acceptance=[SpecCriterion(text="looks right to finance")]),
            spec_version=1, existing=None,
        )


# ── Facts measured at the end of a run ──────────────────────────────────

def _run(*step_ids):
    return WorkflowRunResult(
        workflow_id="wf1", status="success",
        step_results={s: StepRunResult(step_id=s, status="success", row_count=3) for s in step_ids},
    )


def test_duplicates_and_the_newest_timestamp_are_measured_from_the_output():
    conn = duckdb.connect(":memory:")
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    rel = conn.sql(
        "SELECT * FROM (VALUES (1, 'a', TIMESTAMP '2026-11-11 10:00:00'),"
        " (2, 'a', TIMESTAMP '2026-11-11 11:00:00'), (3, 'b', NULL), (4, NULL, NULL), (5, NULL, NULL))"
        " AS t(id, region, ts)"
    )
    contract = ExecutionContract(id="c", guarantees=[
        Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="region"),
        Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="id"),
        Guarantee(type=GuaranteeType.FRESHNESS_MAX_AGE, time_column="ts", max_age_seconds=7200),
        Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="missing_col"),
    ])
    facts = measure_contract_facts(contract, _run("src", "out"), {"out": rel}, conn, now=now)
    # 'a' twice is one duplicate; the two rows with no region are not duplicates.
    assert facts["duplicate_key_counts"] == {"region": 1, "id": 0}
    assert facts["column_age_seconds"] == {"ts": 3600.0}
    assert "not in the output" in facts["unmeasured"]["no_duplicate_key:missing_col"]


def test_the_verifier_uses_the_measured_facts_and_says_why_one_is_missing():
    contract = ExecutionContract(id="c", guarantees=[
        Guarantee(type=GuaranteeType.FRESHNESS_MAX_AGE, time_column="ts", max_age_seconds=60, description="fresh"),
        Guarantee(type=GuaranteeType.NO_DUPLICATE_KEY, key="nope"),
    ])
    receipt = verify_contract(contract, _run("out"), facts={
        "column_age_seconds": {"ts": 30.4},
        "unmeasured": {"no_duplicate_key:nope": "column 'nope' is not in the output of step 'out'"},
    })
    fresh, dup = receipt.clauses
    assert fresh.passed and "30s old" in fresh.detail
    assert not dup.passed and dup.detail == "column 'nope' is not in the output of step 'out'"


def test_destination_confirmed_needs_every_write_reported_and_unblocked():
    assert destination_confirmed([]) is None
    assert destination_confirmed([BlastRadius(step_id="out", rows_written=3)]) is True
    assert destination_confirmed([BlastRadius(step_id="out", rows_written=3, blocked=True)]) is False
    assert destination_confirmed([BlastRadius(step_id="out", rows_written=None)]) is False


# ── Receipts ────────────────────────────────────────────────────────────

def test_a_receipt_names_the_spec_version_and_the_contract_it_was_judged_against():
    contract = ExecutionContract(
        id="c", spec_version=7, spec_digest="ab" * 32,
        guarantees=[Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT)],
    )
    receipt = verify_contract(contract, _run("out"))
    assert receipt.spec_version == 7 and receipt.spec_digest == "ab" * 32
    assert receipt.contract_digest == contract_digest(contract)
    assert "spec: version 7" in receipt.render_markdown()
    assert verify_chain([receipt], contracts={"c": contract}).ok


def test_receipts_made_before_the_spec_fields_existed_still_recompute():
    """The receipt hash covers the contract. A contract that uses none of
    the new fields must hash exactly as it did before they were added."""
    contract = ExecutionContract(id="c", guarantees=[Guarantee(type=GuaranteeType.NO_EMPTY_OUTPUT, description="rows")])
    old_contract_dump = contract.model_dump(mode="json")
    old_contract_dump.pop("spec_version")
    old_contract_dump.pop("spec_digest")
    for g in old_contract_dump["guarantees"]:
        g.pop("time_column")
        g.pop("criterion")
    clauses = verify_contract(contract, _run("out")).clauses
    old_payload = {
        "contract": old_contract_dump, "run_id": "r1", "verdict": "verified",
        "clauses": [c.model_dump(mode="json") for c in clauses],
        "verified_at": "2026-11-01T00:00:00+00:00", "prev_hash": None,
        "resumed_from": None, "provenance": None,
    }
    old_hash = _receipt_hash(old_payload)
    assert old_hash == _receipt_hash(_receipt_payload(
        contract, "r1", "verified", clauses, "2026-11-01T00:00:00+00:00", None, None, None,
    ))
    old_receipt = ContractVerification(
        contract_id="c", run_id="r1", verdict="verified", clauses=clauses,
        verified_at="2026-11-01T00:00:00+00:00", receipt_hash=old_hash,
    )
    assert verify_chain([old_receipt], contracts={"c": contract}).ok


# ── The publish gate ────────────────────────────────────────────────────

class _Store:
    def __init__(self, contract, receipts):
        self.contract, self.receipts = contract, receipts

    def get_contract(self, workflow_id):
        return self.contract

    def list_receipts(self, workflow_id, limit=50):
        return list(self.receipts)


def _receipt_on(wf, contract, *, verdict_rows: int, run_id: str):
    run = _run("out")
    run.step_results["out"].row_count = verdict_rows
    return verify_contract(contract, run, run_id=run_id, pipeline_hash=definition_hash(wf))


def test_the_gate_refuses_only_a_failed_verdict_on_this_definition_and_contract():
    wf = Workflow(id="wf1", name="orders")
    contract = ExecutionContract(id="c", guarantees=[Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=10)])
    failed = _receipt_on(wf, contract, verdict_rows=3, run_id="r-bad")
    with pytest.raises(ContractBroken, match="r-bad"):
        check_publish(_Store(contract, [failed]), "wf1", wf)

    # A newer pass on the same pair lets it through.
    passed = _receipt_on(wf, contract, verdict_rows=30, run_id="r-good")
    assert check_publish(_Store(contract, [passed, failed]), "wf1", wf)["verdict"] == "verified"

    # A changed pipeline: the old failure says nothing about the fix.
    changed = Workflow(id="wf1", name="orders", parameters=[{"name": "region"}])
    assert check_publish(_Store(contract, [failed]), "wf1", changed) is None

    # A changed contract: the old verdict answered a different question.
    looser = ExecutionContract(id="c", guarantees=[Guarantee(type=GuaranteeType.ROW_COUNT_MIN, min_rows=1)])
    assert last_verdict_for(_Store(looser, [failed]), "wf1", wf, looser) is None

    # Receipts from before the fingerprints existed never block.
    legacy = failed.model_copy(update={"contract_digest": None})
    assert check_publish(_Store(contract, [legacy]), "wf1", wf) is None


# ── End to end, through the API ─────────────────────────────────────────

ROWS = 30


@pytest.fixture
def orders_csv(data_dir):
    path = f"{data_dir}/spec_orders.csv"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with open(path, "w", encoding="utf-8") as f:
        f.write("id,region,ts\n")
        f.writelines(
            f"{i},{'abc'[i % 3]},{(now - timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S')}\n"
            for i in range(ROWS)
        )
    return path


@pytest.fixture
def pipeline_id(authed_client, orders_csv):
    r = authed_client.post("/api/workflows", json={
        "name": "spec-orders",
        "steps": [
            {"id": "src", "type": "csv_source", "label": "orders",
             "params": {"file_path": orders_csv}, "position": {"x": 0, "y": 0}},
            {"id": "out", "type": "local_table_sink", "label": "write",
             "params": {"schema_name": "default", "table_name": "spec_orders", "mode": "replace"},
             "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "src", "to_step": "out"}],
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("id") or body["workflow"]["id"]


def _set_spec(client, wid, key):
    r = client.patch(f"/api/workflows/{wid}/docs", json={
        "business_purpose": "Orders by region for the finance close",
        "spec": {
            "business_owner": "Finance operations",
            "consumers": "Close dashboard",
            "sources": ["ERP orders export", "  "],
            "outcome": "Finance closes the month a day sooner",
            "acceptance": [
                {"id": "k", "text": f"One row per {key}", "check": "no_duplicate_key", "key": key},
                {"id": "f", "text": "Newest order within a day", "check": "freshness_max_age",
                 "time_column": "ts", "max_age_seconds": 86400},
                {"id": "p", "text": "Finance signs off the totals", "check": ""},
            ],
        },
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_spec_to_contract_to_receipt_to_publish_gate(authed_client, pipeline_id):
    saved = _set_spec(authed_client, pipeline_id, "region")
    assert saved["spec"]["sources"] == ["ERP orders export"]
    assert saved["spec"]["updated_by"]

    wf = authed_client.get(f"/api/workflows/{pipeline_id}").json()
    wf = wf.get("workflow", wf)
    assert wf["spec"]["acceptance"][0]["key"] == "region"

    made = authed_client.post(f"/api/execution-contracts/{pipeline_id}/from-spec")
    assert made.status_code == 200, made.text
    assert made.json()["from_spec"] == 2
    assert made.json()["checked_by_people"][0]["text"] == "Finance signs off the totals"
    assert made.json()["spec_version"] == saved["version"]

    run = authed_client.post(f"/api/execute/workflow/{pipeline_id}?full_run=true")
    assert run.json()["status"] == "success", run.text
    receipts = authed_client.get(f"/api/execution-contracts/{pipeline_id}/receipts").json()["receipts"]
    latest = receipts[0]
    assert latest["verdict"] == "failed"
    assert latest["spec_version"] == saved["version"]
    clauses = {c["type"]: c for c in latest["clauses"]}
    assert not clauses["no_duplicate_key"]["passed"]          # three regions, 30 rows
    assert clauses["freshness_max_age"]["passed"], clauses     # measured, no longer "not provided"

    status = authed_client.get(f"/api/execution-contracts/{pipeline_id}/status").json()
    assert status["last_verdict"]["verdict"] == "failed"
    assert status["contract"]["behind_spec"] is False

    assert authed_client.post(f"/api/workflows/{pipeline_id}/test").json()["status"] == "success"
    refused = authed_client.post(f"/api/workflows/{pipeline_id}/publish")
    assert refused.status_code == 400
    assert "broke its contract" in refused.json()["detail"]

    # Fix the requirement: one row per id. The contract changes, so the old
    # verdict no longer counts against this version.
    _set_spec(authed_client, pipeline_id, "id")
    behind = authed_client.get(f"/api/execution-contracts/{pipeline_id}/status").json()
    assert behind["contract"]["behind_spec"] is True
    assert authed_client.post(f"/api/execution-contracts/{pipeline_id}/from-spec").status_code == 200
    run = authed_client.post(f"/api/execute/workflow/{pipeline_id}?full_run=true")
    assert run.json()["status"] == "success", run.text
    status = authed_client.get(f"/api/execution-contracts/{pipeline_id}/status").json()
    assert status["last_verdict"]["verdict"] == "verified", status
    assert authed_client.post(f"/api/workflows/{pipeline_id}/test").json()["status"] == "success"
    published = authed_client.post(f"/api/workflows/{pipeline_id}/publish")
    assert published.status_code == 200, published.text


def test_the_copilot_reads_the_spec_and_how_each_criterion_is_checked(authed_client, pipeline_id):
    import asyncio

    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.tools.get_pipeline_spec import DEFINITION

    _set_spec(authed_client, pipeline_id, "id")
    assert authed_client.post(f"/api/execution-contracts/{pipeline_id}/from-spec").status_code == 200
    out = asyncio.run(DEFINITION.handler(
        {"pipeline_id": pipeline_id},
        ToolContext(tenant_id="default", user_id=None, workspace_id="default", environment="dev"),
    ))
    assert out["has_spec"] is True
    assert out["goal"] == "Orders by region for the finance close"
    assert out["business_owner"] == "Finance operations"
    by_id = {c["id"]: c for c in out["criteria"]}
    assert by_id["k"]["in_contract"] is True and by_id["k"]["checked_by"] == "One row per key"
    assert by_id["p"]["checked_by"] == "a person" and by_id["p"]["in_contract"] is False
    assert out["contract"]["from_spec"] == 2


def test_a_spec_with_an_unknown_check_is_not_saved(authed_client, pipeline_id):
    r = authed_client.patch(f"/api/workflows/{pipeline_id}/docs", json={
        "spec": {"acceptance": [{"text": "x", "check": "vibes"}]},
    })
    assert r.status_code == 400
    assert "'vibes' is not a check" in r.json()["detail"]


def test_contracts_are_not_reachable_from_another_workspace(authed_client, pipeline_id):
    _set_spec(authed_client, pipeline_id, "id")
    assert authed_client.post(f"/api/execution-contracts/{pipeline_id}/from-spec").status_code == 200
    other = {"X-Workspace-Id": "someone-else"}
    for method, path in [
        ("get", f"/api/execution-contracts/{pipeline_id}"),
        ("get", f"/api/execution-contracts/{pipeline_id}/receipts"),
        ("delete", f"/api/execution-contracts/{pipeline_id}"),
        ("post", f"/api/execution-contracts/{pipeline_id}/from-spec"),
    ]:
        r = getattr(authed_client, method)(path, headers=other)
        assert r.status_code in (403, 404), (method, path, r.status_code, r.text)
    # Still there for its own workspace.
    assert authed_client.get(f"/api/execution-contracts/{pipeline_id}").json()["contract"] is not None
