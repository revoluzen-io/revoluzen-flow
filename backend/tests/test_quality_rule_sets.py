"""Shared quality rule sets: one set of Data Quality rules, many pipelines."""

from __future__ import annotations

import json
from types import SimpleNamespace

from revoluzen_flow.quality_rules.store import stamp_rule_set_versions

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def _csv(data_dir, name, rows):
    path = f"{data_dir}/{name}.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("order_id,amount\n")
        f.writelines(f"{i},{a}\n" for i, a in rows)
    return path


def _pipeline(client, name, path, rule_sets, own_rules=None):
    r = client.post("/api/workflows", json={
        "name": name,
        "steps": [
            {"id": "src", "type": "csv_source", "label": "orders", "params": {"file_path": path}, "position": {"x": 0, "y": 0}},
            {"id": "dq", "type": "data_quality", "label": "check",
             "params": {"rules": own_rules or [], "rule_sets": rule_sets, "mode": "fail"}, "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "src", "to_step": "dq"}],
    })
    assert r.status_code in (200, 201), r.text
    return r.json().get("id") or r.json()["workflow"]["id"]


def _run(client, wid):
    body = client.post(f"/api/execute/workflow/{wid}?full_run=true").json()
    return body.get("status"), json.dumps(body)


def test_a_set_is_checked_saved_and_refused_like_a_steps_rules(authed_client):
    bad = authed_client.post("/api/quality-rule-sets", json={"name": "broken", "rules": [{"column": "amount", "op": "between", "value": 5}]})
    assert bad.status_code == 400 and "between" in json.dumps(bad.json())
    assert authed_client.post("/api/quality-rule-sets", json={"name": "empty", "rules": []}).status_code == 400

    ok = authed_client.post("/api/quality-rule-sets", json={"name": "order basics", "rules": [{"column": "amount", "op": "gte", "value": 0}]})
    assert ok.status_code == 201, ok.text
    assert ok.json()["version"] == 1 and ok.json()["rule_count"] == 1
    again = authed_client.post("/api/quality-rule-sets", json={"name": "order basics", "rules": [{"column": "amount", "op": "gte", "value": 0}]})
    assert again.status_code == 400 and "already exists" in json.dumps(again.json())


def test_a_step_runs_a_sets_rules_and_a_fix_to_the_set_reaches_every_pipeline(authed_client, data_dir):
    rs = authed_client.post("/api/quality-rule-sets", json={
        "name": "orders strict", "rules": [{"column": "amount", "op": "gt", "value": 0}],
    }).json()
    path = _csv(data_dir, "rs_orders", [(1, 10), (2, 0), (3, 5)])
    first = _pipeline(authed_client, "rs-first", path, [rs["id"]])
    second = _pipeline(authed_client, "rs-second", path, [rs["id"]])

    for wid in (first, second):
        status, body = _run(authed_client, wid)
        assert status != "success", body
        assert "orders strict v1" in body, body  # the error names the set and its version

    # One fix to the set: zero is allowed. Both pipelines pass on their next run.
    put = authed_client.put(f"/api/quality-rule-sets/{rs['id']}", json={"rules": [{"column": "amount", "op": "gte", "value": 0}]})
    assert put.status_code == 200, put.text
    assert put.json()["version"] == 2
    assert {u["id"] for u in put.json()["used_by"]} == {first, second}
    for wid in (first, second):
        assert _run(authed_client, wid)[0] == "success"

    detail = authed_client.get(f"/api/quality-rule-sets/{rs['id']}").json()
    assert detail["history"][0]["version"] == 1
    assert detail["history"][0]["rules"] == [{"column": "amount", "op": "gt", "value": 0}]

    # A set a saved pipeline uses cannot be deleted.
    gone = authed_client.delete(f"/api/quality-rule-sets/{rs['id']}")
    assert gone.status_code == 409 and "rs-first" in json.dumps(gone.json())


def test_a_set_on_a_missing_column_or_a_deleted_set_stops_the_run_by_name(authed_client, data_dir):
    rs = authed_client.post("/api/quality-rule-sets", json={
        "name": "customer record", "rules": [{"column": "email", "op": "not_null"}],
    }).json()
    path = _csv(data_dir, "rs_no_email", [(1, 10)])
    wid = _pipeline(authed_client, "rs-missing-column", path, [rs["id"]])
    status, body = _run(authed_client, wid)
    assert status != "success"
    assert "customer record" in body and "email" in body, body

    wid2 = _pipeline(authed_client, "rs-deleted", path, ["rs_doesnotexist"])
    status, body = _run(authed_client, wid2)
    assert status != "success" and "no longer exists" in body, body

    unused = authed_client.post("/api/quality-rule-sets", json={"name": "unused", "rules": [{"column": "x", "op": "not_null"}]}).json()
    assert authed_client.delete(f"/api/quality-rule-sets/{unused['id']}").status_code == 200


def test_a_missing_column_names_what_the_input_has_and_a_likely_rename(authed_client, data_dir):
    # A partner renamed list_price to price_inr: the error lists the input's
    # columns (the AI diagnosis said it had none) and names the likely rename.
    rs = authed_client.post("/api/quality-rule-sets", json={
        "name": "price checks", "rules": [{"column": "sku", "op": "not_null"}, {"column": "list_price", "op": "gt", "value": 0}],
    }).json()
    path = f"{data_dir}/renamed_prices.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("sku,supplier,price_inr,currency\nSKU-1,Meridian Wholesale,10.5,INR\n")
    wid = _pipeline(authed_client, "rs-renamed-column", path, [rs["id"]])
    status, body = _run(authed_client, wid)
    assert status != "success"
    assert "checks column list_price, which this step's input does not have" in body, body
    assert "The input has sku, supplier, price_inr, currency." in body, body
    assert "price_inr may be list_price under a new name." in body, body


def test_an_edited_set_changes_the_step_cache_key():
    class _Store:
        def __init__(self):
            self.version = 1

        def get(self, set_id, workspace_id=None):
            return SimpleNamespace(version=self.version)

    store = _Store()
    step = SimpleNamespace(type=SimpleNamespace(value="data_quality"), params={"rule_sets": ["rs_a"]})
    workflow = SimpleNamespace(steps=[step])
    stamp_rule_set_versions(workflow, {"quality_rule_set_store": store})
    assert step.params["_rule_set_versions"] == {"rs_a": 1}
    store.version = 2
    stamp_rule_set_versions(workflow, {"quality_rule_set_store": store})
    assert step.params["_rule_set_versions"] == {"rs_a": 2}

    from revoluzen_flow.engine.step_cache import _canonical_params
    assert _canonical_params(step.params)["_rule_set_versions"] == {"rs_a": 2}
