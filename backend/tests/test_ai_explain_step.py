"""explain_step: a valid shape on every path, and the step's own options.

Both halves come from `docs/repair-evaluation.md`:
  - the not-found paths returned a short dict that `normalize_tool_output`
    rejected with "missing required key step_type";
  - no tool showed what a step's parameters mean, so all three recorded
    model runs fixed a renamed join column by writing the new name into
    `join_key` instead of switching to `key_mode: mapped_keys`.
"""

from __future__ import annotations

import asyncio

import pytest

from revoluzen_flow.ai.normalize import normalize_tool_output, register_output_schema
from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ai.tools.explain_step import DEFINITION
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow


@pytest.fixture
def saved(workflow_store, monkeypatch):
    from revoluzen_flow.main import app_state

    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    workflow = Workflow(
        id="wf-explain",
        name="Joined",
        workspace_id="default",
        steps=[
            Step(id="orders", type=StepType.CSV_SOURCE, label="Orders",
                 params={"file_path": "orders.csv"}),
            Step(id="cust", type=StepType.CSV_SOURCE, label="Customers",
                 params={"file_path": "customers.csv", "api_key": "sk-must-not-appear"}),
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


def _call(**inputs):
    ctx = ToolContext(
        tenant_id="default", user_id="u-1", workspace_id="default",
        environment="dev", selected_ids=("wf-explain",),
    )
    return asyncio.run(DEFINITION.handler(inputs, ctx))


# ── The shape, on every path ──────────────────────────────────────────────

def _normalizes(out):
    register_output_schema("explain_step", DEFINITION.output_schema)
    return normalize_tool_output("explain_step", out)


def test_a_step_that_exists_normalizes(saved):
    assert _normalizes(_call(step_id="named"))["found"] is True


def test_a_step_that_does_not_exist_normalizes_and_says_so(saved):
    """Used to raise SchemaError: missing required key 'step_type'."""
    out = _call(step_id="no-such-step")
    normalized = _normalizes(out)
    assert normalized["found"] is False
    assert "not in pipeline" in normalized["message"]


def test_the_message_survives_normalization(saved):
    """`message` was not in the output schema, so it was dropped as an extra."""
    normalized = _normalizes(_call(step_id="no-such-step"))
    assert normalized["message"], "the reason must reach the agent"


def test_a_missing_step_names_the_ones_that_exist(saved):
    out = _call(step_id="nope")
    assert "'named'" in out["message"]


def test_a_missing_pipeline_normalizes(workflow_store, monkeypatch):
    from revoluzen_flow.main import app_state
    monkeypatch.setitem(app_state, "workflow_store", workflow_store)
    out = asyncio.run(DEFINITION.handler(
        {"pipeline_id": "ghost", "step_id": "x"},
        ToolContext(tenant_id="default", user_id="u", workspace_id="default",
                    environment="dev"),
    ))
    assert _normalizes(out)["found"] is False


# ── The options ───────────────────────────────────────────────────────────

def test_a_join_reports_the_key_modes_it_accepts(saved):
    """The gap the recorded runs fell into, closed."""
    out = _call(step_id="named")
    key_mode = next(o for o in out["options"] if o["name"] == "key_mode")
    assert "mapped_keys" in key_mode["choices"]
    assert "different names" in key_mode["description"]


def test_the_options_say_which_ones_this_step_sets(saved):
    out = _call(step_id="named")
    by_name = {o["name"]: o for o in out["options"]}
    assert by_name["join_key"]["set_on_this_step"] is True
    assert by_name["key_pairs"]["set_on_this_step"] is False


def test_key_pairs_is_discoverable_from_the_step(saved):
    """Switching to mapped_keys needs key_pairs, so it has to be listed."""
    names = {o["name"] for o in _call(step_id="named")["options"]}
    assert {"key_mode", "key_pairs", "join_key"} <= names


def test_options_are_absent_rather_than_fatal_for_an_odd_type(saved):
    out = _call(step_id="orders")
    assert isinstance(out["options"], list)


# ── Still sanitized ───────────────────────────────────────────────────────

def test_credentials_are_still_redacted(saved):
    out = _call(step_id="cust")
    assert out["params"]["api_key"] == "***"
    assert "sk-must-not-appear" not in repr(out)
