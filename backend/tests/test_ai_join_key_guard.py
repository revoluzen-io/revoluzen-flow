"""A join key has to exist on both sides, or the run fails later.

Third finding in `docs/repair-evaluation.md`: every recorded model fixed a
renamed join column by writing the new name into `join_key`. `same_key`
mode compiles to `left."k" = right."k"`, so that is a Binder Error at run
time naming an internal view — a long way from the edit that caused it.

`explain_step` was taught to report `mapped_keys` and what it means, and
the model still never called it, so the check lives where the mistake is
made.
"""

from __future__ import annotations

import asyncio
import csv

import pytest

from revoluzen_flow.ai.tools._edit_guards import (
    EditRefused,
    check_join_key_sides,
    join_sides,
)
from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ai.tools.modify_pipeline_step import DEFINITION as MODIFY
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow

PIPELINE_ID = "wf-join"

STEPS = [
    {"id": "orders", "type": "csv_source", "params": {"file_path": "orders.csv"}},
    {"id": "cust", "type": "csv_source", "params": {"file_path": "customers.csv"}},
    {"id": "named", "type": "join",
     "params": {"join_key": ["customer_id"], "join_type": "INNER"}},
]
CONNECTIONS = [
    {"from_step": "orders", "to_step": "named"},
    {"from_step": "cust", "to_step": "named"},
]

# Orders still says customer_id; the customer extract renamed it cust_id.
COLUMNS = {
    "orders": ["order_id", "customer_id", "order_amount"],
    "cust": ["cust_id", "customer_name"],
}


def _columns_for(step_id):
    return COLUMNS.get(step_id)


def _check(params, *, steps=None, connections=None, columns_for=_columns_for):
    check_join_key_sides(
        op="reconfigure",
        args={"step_id": "named", "params": params},
        steps=steps if steps is not None else STEPS,
        connections=connections if connections is not None else CONNECTIONS,
        columns_for=columns_for,
    )


# ── Which side is which ───────────────────────────────────────────────────

def test_the_first_edge_is_the_left_side():
    assert join_sides(STEPS, CONNECTIONS, "named") == ("orders", "cust")


def test_left_input_id_pins_the_left_side():
    steps = [dict(s) for s in STEPS]
    steps[2] = {**steps[2], "params": {**steps[2]["params"], "left_input_id": "cust"}}
    assert join_sides(steps, CONNECTIONS, "named") == ("cust", "orders")


def test_a_join_without_two_inputs_has_no_sides():
    assert join_sides(STEPS, CONNECTIONS[:1], "named") is None


# ── The refusal ───────────────────────────────────────────────────────────

def test_a_key_only_the_left_side_has_is_refused():
    with pytest.raises(EditRefused, match="mapped_keys"):
        _check({"join_key": ["customer_id"]})


def test_a_key_only_the_right_side_has_is_refused():
    with pytest.raises(EditRefused, match="mapped_keys"):
        _check({"join_key": ["cust_id"]})


def test_the_refusal_names_the_columns_on_the_other_side():
    with pytest.raises(EditRefused) as excinfo:
        _check({"join_key": ["customer_id"]})
    assert "cust_id" in str(excinfo.value)


def test_a_key_on_neither_side_says_so():
    with pytest.raises(EditRefused, match="neither side"):
        _check({"join_key": ["nonsense"]})


def test_a_comma_separated_key_string_is_checked_too():
    with pytest.raises(EditRefused):
        _check({"join_key": "customer_id"})


# ── What must still be allowed ────────────────────────────────────────────

def test_a_key_both_sides_have_is_allowed():
    columns = {"orders": ["customer_id"], "cust": ["customer_id", "name"]}
    _check({"join_key": ["customer_id"]}, columns_for=columns.get)


def test_a_correct_mapped_keys_edit_is_allowed():
    _check({
        "key_mode": "mapped_keys",
        "key_pairs": [{"left": "customer_id", "right": "cust_id"}],
    })


# ── mapped_keys written with the wrong field names ───────────────────────
#
# A recorded model switched to mapped_keys — the right idea — and wrote
# `left_key` / `right_key`. The node reads `left` / `right`, so every pair
# was skipped and the join matched every row against every row.

def test_left_key_and_right_key_are_refused():
    with pytest.raises(EditRefused, match="'left' and 'right'"):
        _check({
            "key_mode": "mapped_keys",
            "key_pairs": [{"left_key": "customer_id", "right_key": "cust_id"}],
        })


def test_the_refusal_shows_the_field_names_that_were_used():
    with pytest.raises(EditRefused) as excinfo:
        _check({
            "key_mode": "mapped_keys",
            "key_pairs": [{"left_key": "customer_id", "right_key": "cust_id"}],
        })
    assert "left_key" in str(excinfo.value)


def test_mapped_keys_with_no_pairs_is_refused():
    with pytest.raises(EditRefused, match="at least one entry"):
        _check({"key_mode": "mapped_keys", "key_pairs": []})


def test_a_mapped_key_naming_a_column_the_left_side_lacks_is_refused():
    with pytest.raises(EditRefused, match="left side"):
        _check({
            "key_mode": "mapped_keys",
            "key_pairs": [{"left": "cust_id", "right": "cust_id"}],
        })


def test_a_mapped_key_naming_a_column_the_right_side_lacks_is_refused():
    with pytest.raises(EditRefused, match="right side"):
        _check({
            "key_mode": "mapped_keys",
            "key_pairs": [{"left": "customer_id", "right": "customer_id"}],
        })


def test_an_unrelated_edit_to_a_mapped_keys_join_is_left_alone():
    """Saved pairs may already be wrong; that is not this edit's fault."""
    steps = [dict(s) for s in STEPS]
    steps[2] = {**steps[2], "params": {
        "key_mode": "mapped_keys",
        "key_pairs": [{"left_key": "customer_id", "right_key": "cust_id"}],
    }}
    _check({"join_type": "LEFT"}, steps=steps)


def test_a_custom_on_clause_is_left_alone():
    _check({"key_mode": "custom", "custom_on": "__join_left.a = __join_right.b"})


def test_an_edit_that_does_not_touch_the_key_is_left_alone():
    _check({"join_type": "LEFT"})


def test_unresolvable_columns_mean_silence_not_refusal():
    """A guard that cannot see the data must not block the edit."""
    _check({"join_key": ["customer_id"]}, columns_for=lambda _step: None)


def test_a_non_join_step_is_left_alone():
    steps = [{"id": "named", "type": "filter", "params": {"join_key": ["x"]}}]
    check_join_key_sides(
        op="reconfigure",
        args={"step_id": "named", "params": {"join_key": ["x"]}},
        steps=steps, connections=CONNECTIONS, columns_for=_columns_for,
    )


# ── Through the tool, against a real pipeline ─────────────────────────────

@pytest.fixture
def saved(tmp_path, workflow_store, monkeypatch):
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    monkeypatch.setitem(app_state, "data_dir", str(tmp_path))
    with (tmp_path / "orders.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "customer_id", "order_amount"])
        w.writerow([1, "c1", 500])
    with (tmp_path / "customers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cust_id", "customer_name"])
        w.writerow(["c1", "Acme"])

    workflow = Workflow(
        id=PIPELINE_ID, name="Join", workspace_id="default",
        steps=[
            Step(id="orders", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            Step(id="cust", type=StepType.CSV_SOURCE, label="Customers",
                 params={"file_path": "customers.csv"}),
            Step(id="named", type=StepType.JOIN, label="Add customer",
                 params={"join_key": ["customer_id"], "join_type": "INNER"}),
        ],
        connections=[
            StepConnection(from_step="orders", to_step="named"),
            StepConnection(from_step="cust", to_step="named"),
        ],
    )
    workflow_store.save(workflow, change_summary="test", created_by="test")
    return workflow.id


def _modify(**inputs):
    inputs.setdefault("idempotency_key", "safe_write.u-1.modify.named.1.0.0")
    ctx = ToolContext(
        tenant_id="default", user_id="u-1", workspace_id="default",
        environment="dev", selected_ids=(PIPELINE_ID,),
        user_intent="The join is failing, fix it.",
    )
    return asyncio.run(MODIFY.handler(inputs, ctx))


def test_the_tool_refuses_the_wrong_fix_the_models_all_made(saved):
    with pytest.raises(EditRefused, match="mapped_keys"):
        _modify(op="reconfigure", args={"step_id": "named", "params": {"join_key": ["cust_id"]}})


def test_the_tool_accepts_the_right_fix(saved):
    out = _modify(op="reconfigure", args={"step_id": "named", "params": {
        "key_mode": "mapped_keys",
        "key_pairs": [{"left": "customer_id", "right": "cust_id", "operator": "="}],
    }})
    assert out["draft_id"]
