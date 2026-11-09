"""Planner JSON → IR: the index-to-id translation both callers now share.

`draft_pipeline_from_intent` skipped this and returned the planner's raw
output, so a Copilot-drafted pipeline arrived with `from_step: 0` — a step
id matching nothing — and every step disconnected.
"""

from __future__ import annotations

import asyncio

import pytest

from revoluzen_flow.ai.tools.base import ToolContext
from revoluzen_flow.ai.tools.draft_pipeline_from_intent import DEFINITION as DRAFT
from revoluzen_flow.planner.ir_from_json import planner_json_to_ir

# What the planner's SYSTEM_PROMPT asks the model to produce: no step ids,
# connections by 0-based array index.
PLANNER_JSON = {
    "name": "Orders",
    "steps": [
        {"type": "csv_source", "label": "Read orders", "params": {"file_path": "orders.csv"}},
        {"type": "filter", "label": "Large", "params": {"condition": "amount > 100"}},
        {"type": "csv_sink", "label": "Write", "params": {"file_path": "out.csv"}},
    ],
    "connections": [{"from_step": 0, "to_step": 1}, {"from_step": 1, "to_step": 2}],
}


def test_indices_become_real_step_ids():
    ir = planner_json_to_ir(PLANNER_JSON)
    ids = [s["id"] for s in ir["steps"]]
    assert all(ids), "every step needs an id"
    assert len(set(ids)) == 3, "ids must be distinct"
    assert ir["connections"] == [
        {"from_step": ids[0], "to_step": ids[1]},
        {"from_step": ids[1], "to_step": ids[2]},
    ]


def test_no_connection_still_refers_to_a_bare_index():
    ir = planner_json_to_ir(PLANNER_JSON)
    for conn in ir["connections"]:
        assert not isinstance(conn["from_step"], int)
        assert not isinstance(conn["to_step"], int)


def test_zero_is_an_index_not_a_falsy_blank():
    """`from_step: 0` is step one. Truthiness checks lose it."""
    ir = planner_json_to_ir(PLANNER_JSON)
    assert ir["connections"][0]["from_step"] == ir["steps"][0]["id"]


def test_indices_sent_as_strings_still_resolve():
    data = {**PLANNER_JSON, "connections": [{"from_step": "0", "to_step": "1"}]}
    ir = planner_json_to_ir(data)
    assert ir["connections"][0]["from_step"] == ir["steps"][0]["id"]


def test_a_model_that_invents_step_ids_is_honoured():
    data = {
        "steps": [
            {"id": "a", "type": "csv_source", "params": {}},
            {"id": "b", "type": "csv_sink", "params": {}},
        ],
        "connections": [{"from_step": "a", "to_step": "b"}],
    }
    ir = planner_json_to_ir(data)
    assert [s["id"] for s in ir["steps"]] == ["a", "b"]
    assert ir["connections"] == [{"from_step": "a", "to_step": "b"}]


def test_a_connection_named_by_label_resolves():
    data = {**PLANNER_JSON, "connections": [{"from_step": "Read orders", "to_step": "Large"}]}
    ir = planner_json_to_ir(data)
    assert ir["connections"][0]["to_step"] == ir["steps"][1]["id"]


@pytest.mark.parametrize("conn", [
    {"from_step": 0, "to_step": 9},      # out of range
    {"from_step": 1, "to_step": 1},      # self-edge
    {"from_step": True, "to_step": 1},   # bool is an int; must not mean "step 1"
    {"from_step": None, "to_step": 1},
])
def test_a_connection_that_names_nothing_is_dropped(conn):
    ir = planner_json_to_ir({**PLANNER_JSON, "connections": [conn]})
    assert ir["connections"] == []


def test_no_steps_means_no_ir():
    assert planner_json_to_ir({"steps": []}) is None
    assert planner_json_to_ir(None) is None


def test_the_draft_tool_returns_connected_steps(monkeypatch):
    """End to end through the tool that was missing the translation."""
    from revoluzen_flow.planner import ai_client

    async def fake_planner(messages, **kwargs):
        return dict(PLANNER_JSON)

    monkeypatch.setattr(ai_client, "ai_generate_pipeline", fake_planner)

    out = asyncio.run(DRAFT.handler(
        {"intent": "load orders and filter", "idempotency_key": "safe_write.u.draft.x.1.0.0"},
        ToolContext(tenant_id="default", user_id="u", workspace_id="default",
                    environment="dev"),
    ))
    ids = {s["id"] for s in out["ir"]["steps"]}
    assert out["step_count"] == 3
    assert out["ir"]["connections"], "the draft must carry its connections"
    for conn in out["ir"]["connections"]:
        assert conn["from_step"] in ids
        assert conn["to_step"] in ids
