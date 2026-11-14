"""Across pipelines: repeated step logic, and one cause behind several failures."""

from __future__ import annotations

from types import SimpleNamespace

from revoluzen_flow.incidents.shared import error_fingerprint, shared_causes
from revoluzen_flow.steward.repeated_logic import detect_repeated_logic

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

_SQL = "SELECT customer_id, SUM(amount) AS total FROM orders WHERE status = 'paid' GROUP BY customer_id"


def _wf(wid, name, *steps):
    return {"id": wid, "name": name, "nodes": list(steps)}


def _step(sid, stype, label, **params):
    return {"id": sid, "type": stype, "label": label, "params": params}


def test_the_same_step_in_several_pipelines_is_one_finding():
    workflows = [
        _wf("p1", "Daily totals", _step("a", "transform", "Totals", sql=_SQL)),
        # Same logic: other label, other spacing and case, a trailing semicolon.
        _wf("p2", "Weekly totals", _step("b", "transform", "Sum per customer",
                                         sql=_SQL.lower().replace(" from", "\n  from") + ";")),
        _wf("p3", "Unrelated", _step("c", "transform", "Other", sql="SELECT * FROM refunds WHERE amount > 0 AND kind = 'x'")),
        # Too small to matter, even in three pipelines.
        _wf("p4", "A", _step("d", "filter", "f", condition="x > 1")),
        _wf("p5", "B", _step("e", "filter", "f", condition="x > 1")),
        _wf("p6", "C", _step("g", "filter", "f", condition="x > 1")),
    ]
    found = detect_repeated_logic(workflows)
    assert len(found) == 1, [f.title for f in found]
    f = found[0]
    assert f.kind.value == "repeated_logic"
    assert f.title == "The same transform step is in 2 pipelines"
    assert {u["pipeline"] for u in f.evidence["uses"]} == {"Daily totals", "Weekly totals"}
    assert "Run Pipeline" in f.body

    # Dismissed once, it stays dismissed.
    assert detect_repeated_logic(workflows, suppressed_signatures={f.evidence["source_signature"]}) == []


def test_repeated_data_quality_rules_point_to_a_shared_rule_set():
    rules = [{"column": "email", "op": "not_null"}, {"column": "amount", "op": "gte", "value": 0}]
    workflows = [
        _wf("p1", "Orders", _step("a", "data_quality", "Check", rules=rules, mode="drop")),
        _wf("p2", "Returns", _step("b", "data_quality", "Validate", rules=rules, mode="drop")),
    ]
    [f] = detect_repeated_logic(workflows)
    assert "shared rule set" in f.body
    assert f.evidence["logic"] == "email not_null; amount gte"

    # Steps that only pick the same shared rule set are the fix, not a copy.
    shared = [
        _wf("p1", "Orders", _step("a", "data_quality", "Check", rules=[], rule_sets=["rs_1"], mode="drop")),
        _wf("p2", "Returns", _step("b", "data_quality", "Check", rules=[], rule_sets=["rs_1"], mode="drop")),
    ]
    assert detect_repeated_logic(shared) == []


def _inc(iid, wid, error, failure_class="dependency", step="read"):
    return SimpleNamespace(
        id=iid, workflow_id=wid, workflow_name=wid.upper(), workspace_id="default", status="open",
        error=error, failure_class=failure_class, failed_step=step, last_seen_at="2026-11-11T00:00:00",
    )


def test_open_incidents_that_share_a_connection_or_an_error_are_grouped():
    steps = {
        "w1": [SimpleNamespace(id="s1", label="read", params={"connection_id": "c_wh"})],
        "w2": [SimpleNamespace(id="s2", label="read", params={"connection_id": "c_wh"})],
        "w3": [SimpleNamespace(id="s3", label="read", params={})],
        "w4": [SimpleNamespace(id="s4", label="read", params={})],
        "w5": [SimpleNamespace(id="s5", label="read", params={})],
    }
    store = SimpleNamespace(get=lambda wid, workspace_id=None: SimpleNamespace(workflow=SimpleNamespace(steps=steps[wid])))
    conns = SimpleNamespace(get=lambda cid, workspace_id=None: SimpleNamespace(name="Warehouse"))
    app_state = {"store": store, "connection_store": conns}
    incidents = [
        _inc("i1", "w1", "login refused for user 'etl'"),
        _inc("i2", "w2", "timed out after 30 s"),
        _inc("i3", "w3", "Binder Error: column \"region\" not found in table 'orders_2026'", "data_quality"),
        _inc("i4", "w4", "Binder Error: column \"region\" not found in table 'orders_2025'", "data_quality"),
        _inc("i5", "w5", "something else entirely", "unknown"),
    ]
    groups = shared_causes(app_state, incidents)
    assert [g["count"] for g in groups] == [2, 2]
    by_kind = {("connection" if g["connection"] else "error"): g for g in groups}
    assert by_kind["connection"]["connection"]["name"] == "Warehouse"
    assert by_kind["connection"]["cause"] == 'the connection "Warehouse" cannot be reached, or refused the login'
    assert {p["workflow_id"] for p in by_kind["connection"]["pipelines"]} == {"w1", "w2"}
    assert {p["workflow_id"] for p in by_kind["error"]["pipelines"]} == {"w3", "w4"}
    # The reason shown is true of both: the table names that differ are left out.
    assert by_kind["error"]["cause"] == 'Binder Error: column "…" not found in table "…"'

    assert error_fingerprint("Timed out after 30 s at /data/x.csv") == "timed out after <n> s at <path>"


def test_the_steward_scan_reports_repeated_logic(authed_client):
    for name in ("repeat-one", "repeat-two"):
        r = authed_client.post("/api/workflows", json={
            "name": name,
            "steps": [
                {"id": "src", "type": "csv_source", "label": "orders", "params": {"file_path": "orders.csv"}, "position": {"x": 0, "y": 0}},
                {"id": "t", "type": "transform", "label": f"totals {name}", "params": {"sql": _SQL}, "position": {"x": 1, "y": 0}},
            ],
            "connections": [{"from_step": "src", "to_step": "t"}],
        })
        assert r.status_code in (200, 201), r.text
    findings = authed_client.get("/api/steward/findings").json()["findings"]
    repeated = [f for f in findings if f["kind"] == "repeated_logic"]
    assert repeated and {u["pipeline"] for u in repeated[0]["evidence"]["uses"]} >= {"repeat-one", "repeat-two"}

    incidents = authed_client.get("/api/incidents").json()
    assert incidents["shared_causes"] == []
