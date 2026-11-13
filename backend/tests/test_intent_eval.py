"""Request-to-pipeline accuracy: draft, run on fixture files, compare rows."""

from __future__ import annotations

from revoluzen_flow.eval.intent import CASES, compare_rows, draft_replay, draft_rule, run

# Which requests the rule planner turns into a pipeline that returns the right
# rows. A change to the planner, the steps or the engine that alters this set
# fails here; when the planner gets better, add the case it now gets right.
RULE_PLANNER_CORRECT = {
    "filter_paid", "total_by_customer", "count_by_status", "dedupe_orders", "join_customers",
    "paid_total_by_customer", "daily_revenue", "paid_revenue_by_region",
}


def test_rows_compare_in_any_order_with_columns_found_by_value():
    expected = [{"customer_id": "c1", "total": 150}, {"customer_id": "c2", "total": 70}]
    got = [{"customer_id": "c2", "sum_amount": 70.0}, {"customer_id": "c1", "sum_amount": 150}]
    ok, notes = compare_rows(expected, got)
    assert ok and notes == ["'total' read from 'sum_amount'"]

    assert compare_rows(expected, got[:1])[0] is False                       # a row short
    assert compare_rows(expected, [{"customer_id": "c1", "sum_amount": 150},
                                   {"customer_id": "c2", "sum_amount": 71}])[0] is False  # a wrong total
    ok, notes = compare_rows([{"average": 220 / 3}], [{"avg_amount": 73.33333333333333}])
    assert ok                                                                # numbers within tolerance


def test_the_rule_planner_scores_what_it_can_do_and_names_what_it_cannot():
    report = run(draft_rule)
    assert report["cases"] == len(CASES)
    correct = {r["id"] for r in report["results"] if r["correct"]}
    assert correct == RULE_PLANNER_CORRECT, report["results"]
    assert report["accuracy"] == round(len(RULE_PLANNER_CORRECT) / len(CASES), 4)
    by_id = {r["id"]: r for r in report["results"]}
    # It cannot sort and keep the top rows: it drafts, runs, and is wrong.
    assert by_id["top_two_orders"]["ran"] and not by_id["top_two_orders"]["correct"]


def test_a_recorded_draft_is_scored_again_without_a_model():
    # What a model drafted for one request, as the planner returns it.
    recorded = {
        "top_two_orders": {
            "steps": [
                {"type": "csv_source", "label": "Orders", "params": {"file_path": "orders.csv"}},
                {"type": "transform", "label": "Top 2",
                 "params": {"expression": "SELECT * FROM source_table ORDER BY amount DESC LIMIT 2"}},
                {"type": "file_sink", "label": "Write", "params": {"file_path": "out/top.csv"}},
            ],
            "connections": [{"from_step": 0, "to_step": 1}, {"from_step": 1, "to_step": 2}],
        },
    }
    cases = [c for c in CASES if c.id in ("top_two_orders", "filter_paid")]
    report = run(draft_replay(recorded), cases)
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["top_two_orders"]["correct"], by_id["top_two_orders"]
    assert not by_id["filter_paid"]["drafted"]  # nothing was recorded for it
