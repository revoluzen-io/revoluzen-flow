"""Join key suggestions from names, types and shared values."""

from __future__ import annotations

import asyncio

from revoluzen_flow.intelligence.join_discovery import join_inputs, suggest_join_keys
from revoluzen_flow.ir.schema import Workflow

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

ORDERS = [
    {"order_id": i, "customer_id": 100 + (i % 7), "status": "open" if i % 2 else "paid", "amount": i * 3.5}
    for i in range(1, 60)
]
CUSTOMERS = [{"id": 100 + c, "name": f"Customer {c}", "tier": "gold" if c % 2 else "silver"} for c in range(10)]


def test_a_differently_named_key_is_found_from_shared_values_and_the_step_name():
    out = suggest_join_keys(
        ORDERS, CUSTOMERS,
        left_schema=[{"name": "order_id", "type": "BIGINT"}, {"name": "customer_id", "type": "BIGINT"},
                     {"name": "status", "type": "VARCHAR"}, {"name": "amount", "type": "DOUBLE"}],
        right_schema=[{"name": "id", "type": "BIGINT"}, {"name": "name", "type": "VARCHAR"}, {"name": "tier", "type": "VARCHAR"}],
        left_label="orders", right_label="customers",
    )
    best = out[0]
    assert (best.left, best.right) == ("customer_id", "id")
    assert best.shape == "many-to-one"
    assert best.left_matched_pct == 100.0
    assert any("names the 'customers' step's id" in e for e in best.evidence)
    # A two-value status column is a flag, not a key.
    assert all(s.left != "status" for s in out)


def test_the_same_name_on_both_sides_wins_and_reads_as_same_key():
    left = [{"order_id": i, "qty": i % 5} for i in range(1, 40)]
    right = [{"order_id": i, "shipped": i % 3 == 0} for i in range(1, 40)]
    best = suggest_join_keys(left, right)[0]
    assert best.as_dict()["same_name"] is True
    assert best.shape == "one-to-one"


def test_keys_read_as_text_on_one_side_still_match():
    left = [{"cust": i} for i in range(1, 30)]
    right = [{"cust": str(i) if i % 2 else f"{i}.0"} for i in range(1, 30)]
    best = suggest_join_keys(left, right)[0]
    assert best.left_matched_pct == 100.0


def test_a_many_to_many_pair_is_called_out():
    left = [{"region": r} for r in "aabbccdd"]
    right = [{"region": r} for r in "abcdabcd"]
    best = suggest_join_keys(left, right)[0]
    assert best.shape == "many-to-many"
    assert any("multiply rows" in e for e in best.evidence)


def test_the_key_outranks_a_region_both_sides_share_once_the_samples_are_allowed_for():
    # 2,000 sampled orders of 30,000, against the first 2,000 of 6,000 customers:
    # a third of the orders' customers are in the customers' sample, while every
    # region is, so on the samples alone the region looked like the better key.
    regions = ["AMER", "EMEA", "APAC", "LATAM"]
    orders = [{"order_id": i, "customer_id": f"C{((i // 2) * 7919) % 6000:05d}", "region": regions[i % 4]} for i in range(2000)]
    customers = [{"customer_id": f"C{c:05d}", "region": regions[c % 4]} for c in range(2000)]
    plain = {(s.left, s.right): s for s in suggest_join_keys(orders, customers, left_label="Orders", right_label="Customers")}
    out = suggest_join_keys(orders, customers, left_label="Orders", right_label="Customers", left_total=30000, right_total=6000)
    best = out[0]
    assert (best.left, best.right) == ("customer_id", "customer_id")
    assert best.shape == "many-to-one" and best.estimated is True
    assert plain[("customer_id", "customer_id")].left_matched_pct < 40 and best.left_matched_pct > 90
    assert any("holds 2,000 of Customers' 6,000 rows" in e for e in best.evidence)
    region = next(s for s in out if s.left == "region")
    assert region.shape == "many-to-many" and region.score < best.score and region.estimated is False
    # Even with no row counts, a value repeating on both sides ranks below the key.
    assert plain[("region", "region")].score < plain[("customer_id", "customer_id")].score


def test_a_key_with_a_few_repeats_still_outranks_a_region_and_the_repeats_are_said():
    # The CRM export lists a customer again when their tier changes: 40 of the
    # 2,000 sampled customer rows repeat an ID. The ID was "neither side
    # unique", so the region every row shares ranked first and the join
    # multiplied every order by thousands.
    regions = ["AMER", "EMEA", "APAC", "LATAM"]
    orders = [{"order_id": i, "customer_id": f"C{((i // 2) * 7919) % 6000:05d}", "region": regions[i % 4]} for i in range(2000)]
    customers = []
    for c in range(1960):
        customers.append({"customer_id": f"C{c:05d}", "region": regions[c % 4], "tier": "silver"})
        if c % 49 == 0:
            customers.append({"customer_id": f"C{c:05d}", "region": regions[c % 4], "tier": "gold"})
    assert len(customers) == 2000
    out = suggest_join_keys(orders, customers, left_label="Orders", right_label="Customers", left_total=30000, right_total=6120)
    best = out[0]
    assert (best.left, best.right) == ("customer_id", "customer_id")
    assert best.estimated is True and best.left_matched_pct > 90
    assert best.repeat_note == "customer_id repeats in 40 of the 2,000 sampled Customers rows: those rows will match more than once"
    assert best.as_dict()["repeat_note"] == best.repeat_note
    region = next(s for s in out if s.left == "region")
    assert region.score < best.score and region.repeat_note is None
    assert any("multiply rows" in e for e in region.evidence)


def test_suggestions_carry_names_and_numbers_only():
    secret = [{"email": f"person{i}@example.com", "id": i} for i in range(1, 30)]
    other = [{"email": f"person{i}@example.com", "score": i} for i in range(1, 30)]
    text = repr([s.as_dict() for s in suggest_join_keys(secret, other)])
    assert "example.com" not in text and "person1" not in text


def test_join_inputs_follow_the_pinned_left_side():
    wf = Workflow.model_validate({
        "name": "j", "steps": [
            {"id": "a", "type": "csv_source", "params": {}}, {"id": "b", "type": "csv_source", "params": {}},
            {"id": "j", "type": "join", "params": {"left_input_id": "b"}},
        ],
        "connections": [{"from_step": "a", "to_step": "j"}, {"from_step": "b", "to_step": "j"}],
    })
    assert join_inputs(wf, "j") == ("b", "a")


def _csvs(data_dir):
    orders = f"{data_dir}/jd_orders.csv"
    with open(orders, "w", encoding="utf-8") as f:
        f.write("order_id,customer_id,amount\n")
        f.writelines(f"{r['order_id']},{r['customer_id']},{r['amount']}\n" for r in ORDERS)
    customers = f"{data_dir}/jd_customers.csv"
    with open(customers, "w", encoding="utf-8") as f:
        f.write("id,name\n")
        f.writelines(f"{r['id']},{r['name']}\n" for r in CUSTOMERS)
    return orders, customers


def _pipeline(orders, customers):
    return {
        "name": "join-discovery",
        "steps": [
            {"id": "o", "type": "csv_source", "label": "orders", "params": {"file_path": orders}, "position": {"x": 0, "y": 0}},
            {"id": "c", "type": "csv_source", "label": "customers", "params": {"file_path": customers}, "position": {"x": 0, "y": 1}},
            {"id": "j", "type": "join", "label": "orders with customers", "params": {"join_type": "LEFT"}, "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "o", "to_step": "j"}, {"from_step": "c", "to_step": "j"}],
    }


def test_the_canvas_endpoint_suggests_keys_for_an_unsaved_join(authed_client, data_dir):
    orders, customers = _csvs(data_dir)
    r = authed_client.post("/api/execute/workflow/ephemeral/step/j/join-keys", json={"workflow": _pipeline(orders, customers)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["left_label"], body["right_label"]) == ("orders", "customers")
    assert (body["suggestions"][0]["left"], body["suggestions"][0]["right"]) == ("customer_id", "id")
    assert "Customer 1" not in r.text


def test_the_copilot_tool_says_to_use_mapped_keys_for_different_names(authed_client, data_dir):
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.tools.suggest_join_keys import DEFINITION

    orders, customers = _csvs(data_dir)
    saved = authed_client.post("/api/workflows", json=_pipeline(orders, customers))
    assert saved.status_code in (200, 201), saved.text
    wid = saved.json().get("id") or saved.json()["workflow"]["id"]
    out = asyncio.run(DEFINITION.handler(
        {"pipeline_id": wid, "step_id": "j"},
        ToolContext(tenant_id="default", user_id=None, workspace_id="default", environment="dev"),
    ))
    assert out["suggestions"][0]["left"] == "customer_id"
    assert "mapped_keys" in out["message"] and "do not put either name in join_key" in out["message"]
