"""Run every built-in template on small data and check what it does.

``test_template_steps_match_nodes.py`` holds each step to its node's
``param_schema()``. This module runs the pipelines, because a step can use names
the node reads and still not do what its label says (an Aggregate that groups by
the wrong thing, a Join on the wrong side, a Switch whose case matches no row).

The systems a template reaches for are replaced by files and nothing else is
touched: a database or REST source reads a CSV / JSON file of the same rows, and
a database, warehouse or REST destination writes a CSV file. Every other step
runs exactly as the template has it, through the real executor. Managed Table
sinks and the Slack step stay as they are (Slack without a webhook URL logs the
message and passes the rows on).

Each fixture has a row the pipeline must treat differently from the rest (an
empty key, a duplicate, an amount out of range), so a step that passes
everything is seen.
"""
from __future__ import annotations

import json
import os
import pathlib
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Step as IrStep, StepConnection, StepType, Workflow, WorkflowParameter
from tests._catalog_reader import CATALOG, Template, read_catalog

pytestmark = pytest.mark.skipif(
    not CATALOG.exists(), reason=f"frontend template catalog not found at {CATALOG}"
)

TEMPLATES: dict[str, Template] = {t.id: t for t in read_catalog()} if CATALOG.exists() else {}
FILE_CONNECTORS = {"csv", "json", "parquet", "excel", "xml"}
SEED_ORDERS = pathlib.Path(__file__).parents[1] / "revoluzen_flow" / "seed_data" / "orders.csv"


# ── Fixtures: one (kind, text) per source step ─────────────────────────────

def csv(*lines: str) -> tuple[str, str]:
    return "csv", "\n".join(lines) + "\n"


def js(records: list[dict]) -> tuple[str, str]:
    return "json", json.dumps(records)


FIXTURES: dict[tuple[str, str], tuple[str, str]] = {
    ("first_pipeline_csv", "s1"): ("csv", SEED_ORDERS.read_text(encoding="utf-8") if SEED_ORDERS.exists() else ""),
    ("csv_to_postgres", "s1"): csv("order_id,amount", "1,10", ",20", "3,30"),
    ("file_to_managed_table", "s1"): csv(
        "order_id,customer_id,product,region,quantity",
        "1, C-1 , Widget ,  North ,5",
        "2,C-2,Gadget,South,3",
        ",C-3,Bolt,East,1",
    ),
    ("api_to_managed_table", "s1"): js([
        {"id": "1", "updated_at": "2026-01-01T00:00:00", "data": {"name": "a", "qty": 1}},
        {"id": "2", "updated_at": "2026-01-02T00:00:00", "data": {"name": "b", "qty": 2}},
    ]),
    ("sharepoint_to_managed_table", "s1"): csv("id,name", "1,Ann", "2,Bo"),
    ("oracle_to_managed_table", "s1"): js([
        {"invoice_id": "100", "invoice_date": "2026-03-01", "total": 5},
        {"invoice_id": "101", "invoice_date": "2026-03-02", "total": 6},
    ]),
    ("sqlserver_to_postgres", "s1"): csv(
        "order_id,updated_at", "1,2026-01-01", ",2026-01-02", "3,2026-01-03",
    ),
    ("incremental_db_extract", "s1"): csv("event_id,updated_at", "7,2026-01-01", "8,2026-01-02"),
    ("daily_sync_pipeline", "s1"): csv("id,name", "1,a", ",b", "3,c"),
    ("failure_alert_flow", "s1"): js([
        {"workflow_name": "Nightly load", "status": "success", "started_at": "2026-11-08T01:00:00", "error_message": None},
        {"workflow_name": "Orders sync", "status": "error", "started_at": "2026-11-08T02:00:00", "error_message": "connection refused"},
        {"workflow_name": "Billing", "status": "error", "started_at": "2026-11-08T03:00:00", "error_message": None},
    ]),
    ("file_cleanup_to_table", "s1"): csv(
        "customer_id,email,updated_at",
        "C1,old@x.io,2026-01-01",
        "C1,new@x.io,2026-02-01",
        ",ghost@x.io,2026-01-05",
        "C2,two@x.io,2026-01-09",
    ),
    ("webhook_to_managed_table", "s1"): js([{"id": "evt_1", "payload": {"plan": "pro", "seats": 4}}]),
    ("api_paginated_ingest", "s1"): js([
        {"id": 1, "data": {"sku": "a"}}, {"id": 2, "data": {"sku": "b"}}, {"id": 3, "data": {"sku": "c"}},
    ]),
    ("api_incremental", "s1"): js([
        {"id": "5", "updated_at": "2026-04-01T10:00:00", "data": {"k": "v"}},
        {"id": "6", "updated_at": "2026-04-02T10:00:00", "data": {"k": "w"}},
    ]),
    ("db_to_warehouse", "s1"): csv(
        "order_id,customer_id,region,amount,day",
        "1,c1,north,10,2026-05-01",
        "2,c2,north,20,2026-05-01",
        "3,c3,south,5,2026-05-01",
        "4,c1,north,7,2026-05-02",
    ),
    ("scheduled_alert", "s1"): csv("job_name,failures", "etl,7", "sync,2", "ml,9"),
    ("multi_source_join", "s1"): csv(
        "customer_id,name,email,created_at",
        "c1,Ann,a@x.io,2026-01-01", "c2,Bo,b@x.io,2026-01-02", "c3,Cy,c@x.io,2026-01-03",
    ),
    ("multi_source_join", "s2"): js([{"customer_id": "c1", "ltv": 1500}, {"customer_id": "c2", "ltv": 150}]),
    ("fanout_regional", "s1"): csv(
        "region,country,amount",
        "EMEA,FR,10", "EMEA,FR,5", "EMEA,DE,7", "AMER,US,20", "APAC,JP,3", "LATAM,BR,99",
    ),
    ("ai_classify_route", "s1"): js([
        {"id": 1, "subject": "production is down again"},
        {"id": 2, "subject": "how do I export a report"},
        {"id": 3, "subject": "win a free prize now"},
    ]),
    ("data_quality_dlq", "s1"): csv(
        "event_id,amount,email",
        "1,50,ok@x.io", ",60,nokey@x.io", "3,9999999,big@x.io", "4,70,not-an-email",
    ),
    ("reverse_etl", "s1"): csv(
        "customer_id,segment,lifetime_value,last_seen_at",
        "c1,gold,900,2026-06-01", "c2,basic,40,2026-06-02",
    ),
    ("webhook_enrich", "s1"): js([{"event_type": "signup", "user_id": "u1"}]),
    ("webhook_enrich", "s3"): csv("user_id,name,email,team", "u1,Ann,a@x.io,data", "u2,Bo,b@x.io,ops"),
    ("medallion_multi_hop", "s1"): csv(
        "id,updated_at,amount,category",
        "1,2026-01-05,10,A",
        "1,2026-01-06,12,A",
        "2,2026-01-10,20,B",
        "3,2026-02-01,5000000,B",
        ",2026-01-11,8,A",
    ),
    ("kimball_dimensional", "s1"): csv(
        "customer_id,cust_name,effective_from,end_date",
        "c1,Ann,2025-01-01,", "c2,Bo,2025-02-01,2025-12-31",
    ),
    ("kimball_dimensional", "s2"): csv("product_id,prod_name", "p1,Widget", "p2,Gadget"),
    ("kimball_dimensional", "s3"): csv(
        "order_id,customer_id,product_id,amount", "1,c1,p1,10", "2,c2,p2,20", "3,c1,p2,30",
    ),
    ("inmon_3nf_edw", "s1"): csv(
        "customer_id,order_id,line_no,customer_name,customer_email,product_id,quantity,amount,order_date",
        "1,10,1,Ann,a@x.io,p1,2,20,2026-01-01",
        "1,10,2,Ann,a@x.io,p2,1,5,2026-01-01",
        "2,11,1,Bo,b@x.io,p1,1,10,2026-01-02",
    ),
    ("data_vault_2", "s1"): csv(
        "customer_id,product_id,order_id,name,email,address",
        "c1,p1,o1,Ann,a@x.io,1 Main St",
        "c1,p2,o2,Ann,a@x.io,1 Main St",
        "c2,p1,o3,Bo,b@x.io,2 Side St",
    ),
    ("one_big_table", "s1"): csv(
        "order_id,customer_id,product_id,amount,order_date",
        "1,c1,p1,1500,2026-01-01", "2,c2,p1,150,2026-01-02", "3,c3,p2,15,2026-01-03",
    ),
    ("one_big_table", "s2"): csv("customer_id,total_spent", "c1,6000", "c2,1500", "c3,10"),
    ("one_big_table", "s3"): csv("product_id,product_name", "p1,Widget", "p2,Gadget"),
}


# ── Running a template ─────────────────────────────────────────────────────

class Run:
    """One run of a template on its fixtures, with ways to read what it wrote."""

    def __init__(self, template: Template, root: pathlib.Path, result: Any, state: dict):
        self.template, self.root, self.result, self.state = template, root, result, state

    def step(self, step_id: str):
        return self.result.step_results[step_id]

    def rows(self, step_id: str) -> int:
        return self.step(step_id).row_count

    def csv_rows(self, step_id: str) -> list[dict]:
        """Rows a replaced destination wrote, as dicts."""
        path = self.root / "out" / f"{self.template.id}_{step_id}.csv"
        return _read(f"read_csv_auto('{_posix(path)}')")

    def table(self, schema: str, name: str) -> list[dict]:
        """Rows of a Managed Table."""
        glob = self.root / "tables" / "default" / schema / name / "part-*.parquet"
        return _read(f"read_parquet('{_posix(glob)}')")


def _posix(path: pathlib.Path) -> str:
    return str(path).replace("\\", "/")


def _read(source: str) -> list[dict]:
    con = duckdb.connect()
    try:
        rel = con.sql(f"SELECT * FROM {source}")
        cols = rel.columns
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()


def _write(root: pathlib.Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def localize(template: Template, root: pathlib.Path, fixtures: dict, step_params: dict | None = None) -> Workflow:
    """The template's pipeline with its source and destination systems swapped for files."""
    steps = []
    for s in template.steps:
        params = {**s.params, **(step_params or {}).get((template.id, s.id), {})}
        connector = str(params.get("connector_type", "")).lower()
        if s.type == "source":
            kind, text = fixtures[(template.id, s.id)]
            if connector in FILE_CONNECTORS:
                _write(root, params["file_path"], text)
            else:
                relative = f"fixtures/{template.id}_{s.id}.{kind}"
                _write(root, relative, text)
                params = {"connector_type": kind, "file_path": relative}
                if kind == "csv":
                    params["header"] = True
        elif s.type == "destination" and connector not in FILE_CONNECTORS:
            params = {"connector_type": "csv", "file_path": f"out/{template.id}_{s.id}.csv"}
        elif s.type == "destination" and connector == "csv":
            params["file_path"] = f"out/{template.id}_{s.id}.csv"
        steps.append(IrStep(id=s.id, type=StepType(s.type), label=s.label, params=params))
    return Workflow(
        id=f"run-{template.id}",
        name=template.name,
        steps=steps,
        connections=[
            StepConnection(
                from_step=c["from_step"], to_step=c["to_step"], from_port=c.get("from_port") or "output",
            )
            for c in template.connections
        ],
        parameters=[WorkflowParameter(**p) for p in template.parameters],
    )


@pytest.fixture
def run_template(tmp_path, datastore, schema_history_store):
    """run_template(id, **fixture overrides) -> Run. The same folder serves a second call."""
    state = {"data_dir": str(tmp_path), "datastore": datastore, "schema_history_store": schema_history_store}

    def _run(template_id: str, overrides: dict | None = None, step_params: dict | None = None) -> Run:
        template = TEMPLATES[template_id]
        fixtures = {**FIXTURES, **(overrides or {})}
        wf = localize(template, tmp_path, fixtures, step_params)
        result = WorkflowExecutor(data_dir=str(tmp_path), app_state=state).execute_workflow(wf, full_run=True)
        return Run(template, tmp_path, result, state)

    return _run


def _failed_steps(run: Run) -> dict:
    return {sid: sr.error for sid, sr in run.result.step_results.items() if sr.status != "success"}


# ── Every template runs ────────────────────────────────────────────────────

@pytest.mark.parametrize("template_id", list(TEMPLATES), ids=list(TEMPLATES))
def test_every_template_runs_end_to_end(template_id, run_template):
    run = run_template(template_id)
    assert run.result.status == "success", _failed_steps(run)
    assert _failed_steps(run) == {}
    assert len(run.result.step_results) == len(TEMPLATES[template_id].steps)


def test_every_template_with_a_source_has_a_fixture():
    missing = [
        f"{t.id}/{s.id}" for t in TEMPLATES.values() for s in t.steps
        if s.type == "source" and (t.id, s.id) not in FIXTURES
    ]
    assert missing == []


# ── What the steps do ──────────────────────────────────────────────────────

def test_first_pipeline_keeps_active_orders_and_stamps_them(run_template):
    run = run_template("first_pipeline_csv")
    active = sum(1 for line in SEED_ORDERS.read_text(encoding="utf-8").splitlines()[1:] if ",active," in line)
    out = run.csv_rows("s4")
    assert len(out) == active > 0
    assert all(r["loaded_at"] for r in out)


def test_csv_to_postgres_drops_the_row_without_an_id(run_template):
    run = run_template("csv_to_postgres")
    assert [r["order_id"] for r in run.csv_rows("s4")] == [1, 3]


def test_file_to_managed_table_trims_and_drops_the_empty_row(run_template):
    run = run_template("file_to_managed_table")
    rows = {r["order_id"]: r for r in run.table("default", "orders_clean")}
    assert sorted(rows) == [1, 2]
    assert rows[1]["customer_id"] == "C-1" and rows[1]["product"] == "Widget" and rows[1]["region"] == "North"


def test_api_to_managed_table_flattens_casts_and_merges_twice_into_the_same_rows(run_template):
    run = run_template("api_to_managed_table")
    table = run.table("default", "api_records")
    assert sorted(r["id"] for r in table) == [1, 2] and "data_name" in table[0]
    again = run_template("api_to_managed_table")
    assert again.result.status == "success"
    assert len(again.table("default", "api_records")) == 2  # merge on id, not append


def test_sharepoint_stops_when_a_row_has_no_id_and_loads_when_all_have_one(run_template):
    assert len(run_template("sharepoint_to_managed_table").table("default", "sharepoint_data")) == 2
    bad = run_template("sharepoint_to_managed_table", {("sharepoint_to_managed_table", "s1"): csv("id,name", "1,Ann", ",Bo")})
    assert bad.result.status == "error"
    assert "failed validation" in (bad.step("s2").error or "")


def test_oracle_casts_the_invoice_columns(run_template):
    table = run_template("oracle_to_managed_table").table("default", "oracle_invoices")
    assert sorted(r["invoice_id"] for r in table) == [100, 101]
    assert str(table[0]["invoice_date"]).startswith("2026-03")


def test_sqlserver_sync_drops_null_ids_and_stamps_synced_at(run_template):
    out = run_template("sqlserver_to_postgres").csv_rows("s4")
    assert [r["order_id"] for r in out] == [1, 3] and all(r["synced_at"] for r in out)


def test_incremental_extract_merges_by_event_id(run_template):
    run_template("incremental_db_extract")
    again = run_template("incremental_db_extract")
    assert sorted(r["event_id"] for r in again.table("default", "events")) == [7, 8]


def test_daily_sync_drops_null_ids(run_template):
    assert [r["id"] for r in run_template("daily_sync_pipeline").csv_rows("s3")] == [1, 3]


def test_failure_alert_formats_only_the_failed_runs(run_template):
    run = run_template("failure_alert_flow")
    assert run.rows("s2") == 2
    text = " | ".join(row["message"] for row in run.step("s2").sample_data)
    assert "Orders sync" in text and "connection refused" in text
    assert "Billing" in text and "unknown error" in text  # no error_message on that run
    assert "Nightly load" not in text  # it succeeded


def test_file_cleanup_keeps_the_newest_row_per_customer(run_template):
    rows = {r["customer_id"]: r for r in run_template("file_cleanup_to_table").table("default", "customers_clean")}
    assert sorted(rows) == ["C1", "C2"]
    assert rows["C1"]["email"] == "new@x.io"


def test_file_cleanup_stops_when_a_required_email_is_missing(run_template):
    bad = run_template("file_cleanup_to_table", {
        ("file_cleanup_to_table", "s1"): csv("customer_id,email,updated_at", "C1,,2026-01-01", "C2,two@x.io,2026-01-02"),
    })
    assert bad.result.status == "error" and "failed validation" in (bad.step("s4").error or "")


def test_webhook_to_managed_table_appends_each_event(run_template):
    run_template("webhook_to_managed_table")
    table = run_template("webhook_to_managed_table").table("default", "webhook_events")
    assert len(table) == 2 and "payload_plan" in table[0] and all(r["received_at"] for r in table)


def test_paginated_ingest_merges_by_id(run_template):
    assert len(run_template("api_paginated_ingest").table("default", "api_paged_records")) == 3


def test_api_incremental_flattens_and_casts(run_template):
    out = run_template("api_incremental").csv_rows("s4")
    assert sorted(r["id"] for r in out) == [5, 6] and "data_k" in out[0]


def test_daily_rollup_groups_by_day_and_region(run_template):
    out = {(str(r["day"]), r["region"]): r for r in run_template("db_to_warehouse").csv_rows("s3")}
    assert len(out) == 3
    assert out[("2026-05-01", "north")]["revenue"] == 30 and out[("2026-05-01", "north")]["order_count"] == 2
    assert out[("2026-05-02", "north")]["revenue"] == 7


def test_scheduled_alert_pages_only_for_jobs_over_the_threshold(run_template):
    run = run_template("scheduled_alert")
    assert run.rows("s3") == 2  # etl (7) and ml (9); sync (2) is under 5


def test_customer_360_left_joins_and_segments(run_template):
    out = {r["customer_id"]: r for r in run_template("multi_source_join").csv_rows("s5")}
    assert sorted(out) == ["c1", "c2", "c3"]  # LEFT: c3 has no LTV and is kept
    assert (out["c1"]["segment"], out["c2"]["segment"], out["c3"]["segment"]) == ("enterprise", "smb", "self_serve")


def test_regional_fanout_sends_each_region_to_its_branch_and_drops_the_rest(run_template):
    run = run_template("fanout_regional")
    assert (run.rows("s3a"), run.rows("s3b"), run.rows("s3c")) == (2, 1, 1)  # FR + DE, US, JP
    out = {r["country"]: r["total"] for r in run.csv_rows("s5")}
    assert out == {"FR": 15, "DE": 7, "US": 20, "JP": 3}  # LATAM matches no case


def test_ai_classify_routes_each_ticket_to_its_own_destination(run_template):
    # The built-in hash provider only recognises a label's example when it is the
    # label's whole text, so this run gives each label one example and sends
    # tickets that read exactly like them. What it pins is the routing: every
    # category leaves the Switch by its own port and lands in its own destination.
    one_example = {("ai_classify_route", "s2"): {"labels": [
        {"name": "urgent", "examples": ["production is down"]},
        {"name": "standard", "examples": ["how do I export a report"]},
        {"name": "spam", "examples": ["win a free prize"]},
    ]}}
    tickets = js([
        {"id": 1, "subject": "production is down"},
        {"id": 2, "subject": "how do I export a report"},
        {"id": 3, "subject": "win a free prize"},
    ])
    run = run_template("ai_classify_route", {("ai_classify_route", "s1"): tickets}, one_example)
    assert (run.rows("s4a"), run.rows("s4b"), run.rows("s4c")) == (1, 1, 1)
    assert [r["id"] for r in run.table("default", "spam_archive")] == [3]
    assert run.csv_rows("s4b")[0]["category"] == "standard"
    # Appended, not replaced: a second batch adds to the archive.
    again = run_template("ai_classify_route", {("ai_classify_route", "s1"): tickets}, one_example)
    assert len(again.table("default", "spam_archive")) == 2


def test_dead_letter_template_splits_good_and_bad_rows(run_template):
    run = run_template("data_quality_dlq")
    assert [r["event_id"] for r in run.csv_rows("s3")] == [1]
    assert len(run.csv_rows("s4")) == 3


def test_reverse_etl_reshapes_to_the_payload(run_template):
    out = run_template("reverse_etl").csv_rows("s3")
    assert sorted(r["external_id"] for r in out) == ["c1", "c2"]
    assert "gold" in next(r["traits"] for r in out if r["external_id"] == "c1")


def test_webhook_enrich_adds_the_user_columns(run_template):
    out = run_template("webhook_enrich").csv_rows("s5")
    assert len(out) == 1 and (out[0]["name"], out[0]["team"]) == ("Ann", "data")


def test_webhook_enrich_stops_when_the_event_has_no_user(run_template):
    bad = run_template("webhook_enrich", {("webhook_enrich", "s1"): js([{"event_type": "signup", "user_id": None}])})
    assert bad.result.status == "error" and "failed validation" in (bad.step("s2").error or "")


def test_medallion_appends_raw_dedupes_validates_and_rolls_up(run_template):
    run = run_template("medallion_multi_hop")
    assert len(run.table("bronze", "orders")) == 5  # everything as loaded, duplicates and bad rows too
    silver = {r["id"]: r for r in run.table("silver", "orders")}
    assert sorted(silver) == [1, 2]  # id 1 once (the newer row), id 3 over the amount limit, null id dropped
    assert silver[1]["amount"] == 12
    gold = {(r["category"], str(r["month"])[:7]): r for r in run.table("gold", "orders_monthly")}
    assert set(gold) == {("A", "2026-01"), ("B", "2026-01")}
    assert gold[("A", "2026-01")]["total"] == 12 and gold[("A", "2026-01")]["row_count"] == 1
    # A second run appends to Bronze, merges into Silver and replaces Gold.
    again = run_template("medallion_multi_hop")
    assert len(again.table("bronze", "orders")) == 10
    assert len(again.table("silver", "orders")) == 2
    assert len(again.table("gold", "orders_monthly")) == 2


def test_kimball_builds_surrogate_keys_and_joins_the_fact_to_both_dimensions(run_template):
    run = run_template("kimball_dimensional")
    dim = {r["customer_id"]: r for r in run.table("dw", "dim_customer")}
    assert sorted(r["customer_sk"] for r in dim.values()) == [1, 2]
    assert dim["c1"]["is_current"] is True and dim["c2"]["is_current"] is False
    assert sorted(r["product_sk"] for r in run.table("dw", "dim_product")) == [1, 2]
    fact = run.table("dw", "fact_orders")
    assert len(fact) == 3 and all(r["customer_sk"] and r["product_sk"] for r in fact)  # LEFT joins keep every order


def test_inmon_splits_into_entities(run_template):
    run = run_template("inmon_3nf_edw")
    assert (run.rows("s3"), run.rows("s4"), run.rows("s5")) == (2, 2, 3)  # DISTINCT customers and orders, all lines
    assert len(run.csv_rows("s10")) == 3


def test_inmon_stops_on_a_line_without_a_customer(run_template):
    bad = run_template("inmon_3nf_edw", {("inmon_3nf_edw", "s1"): csv(
        "customer_id,order_id,line_no,customer_name,customer_email,product_id,quantity,amount,order_date",
        ",10,1,Ann,a@x.io,p1,2,20,2026-01-01",
    )})
    assert bad.result.status == "error"


def test_data_vault_merges_hubs_links_and_satellites_by_hash_key(run_template):
    run = run_template("data_vault_2")
    assert len(run.table("vault", "hub_customer")) == 2 and len(run.table("vault", "hub_product")) == 2
    assert len(run.table("vault", "link_order")) == 3
    assert len(run.table("vault", "sat_customer")) == 2  # c1's two rows share one hashdiff
    again = run_template("data_vault_2")
    assert len(again.table("vault", "hub_customer")) == 2  # a second load adds nothing


def test_one_big_table_joins_everything_and_derives_the_bands(run_template):
    rows = {r["order_id"]: r for r in run_template("one_big_table").table("analytics", "obt_orders_enriched")}
    assert sorted(rows) == [1, 2, 3]
    assert [rows[i]["revenue_band"] for i in (1, 2, 3)] == ["high", "mid", "low"]
    assert [rows[i]["customer_segment"] for i in (1, 2, 3)] == ["platinum", "gold", "standard"]
    assert rows[1]["product_name"] == "Widget" and rows[1]["days_since_order"] > 0


@pytest.mark.parametrize("template_id", ["webhook_to_managed_table", "webhook_enrich"])
def test_a_webhook_run_receives_its_parameter(template_id):
    """${param.event_id} in the first step's path is replaced by the value the endpoint passed."""
    from revoluzen_flow.engine.parameters import resolve_workflow_parameters

    template = TEMPLATES[template_id]
    real = Workflow(
        id="p", name="p",
        steps=[IrStep(id=s.id, type=StepType(s.type), label=s.label, params=dict(s.params)) for s in template.steps],
        connections=[],
        parameters=[WorkflowParameter(**p) for p in template.parameters],
    )
    resolved = resolve_workflow_parameters(real, {"event_id": "evt_42"})
    assert next(s for s in resolved.steps if s.id == "s1").params["path"] == "/v1/events/evt_42"
    # The default is used when the call names no event.
    default = resolve_workflow_parameters(real, {})
    assert next(s for s in default.steps if s.id == "s1").params["path"] == "/v1/events/latest"
