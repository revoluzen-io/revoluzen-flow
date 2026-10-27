"""Per-step run progress must reach the canvas (2026-10-25).

Live progress existed only on the WebSocket-initiated path, through the
separate ``RealtimeExecutor``. The Run button posts to REST, which uses
``WorkflowExecutor``, so nothing ever reported a step starting or
finishing: instrumented at 60ms, a real run produced ZERO visual
transitions on any node — the canvas sat idle and then the results
appeared all at once.

``WorkflowExecutor`` now takes an optional ``on_event`` callback and the
REST handler passes one that broadcasts to whoever is watching the
pipeline over the WebSocket.

Three properties are pinned, and the last two matter as much as the first:

  1. The events arrive, in order, with the row counts attached.
  2. A listener that RAISES cannot break the run. Observability is never
     worth a failed pipeline, and a run that fails only when someone is
     watching it is the worst kind of bug to chase.
  3. With no listener, nothing changes at all.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.ir.schema import Workflow


@pytest.fixture
def chain(tmp_path):
    (tmp_path / "orders.csv").write_text(
        "order_id,amount,status\n1,250,paid\n2,89,paid\n3,1200,cancelled\n",
        encoding="utf-8",
    )
    return Workflow(**{
        "id": "evt-wf",
        "name": "evt",
        "steps": [
            {"id": "s1", "type": "csv_source", "label": "src",
             "params": {"file_path": "orders.csv"}, "position": {"x": 0, "y": 0}},
            {"id": "s2", "type": "filter", "label": "paid",
             "params": {"mode": "expression", "condition": "status = 'paid'"},
             "position": {"x": 1, "y": 0}},
            {"id": "s3", "type": "derived_column", "label": "calc",
             "params": {"columns": [{"name": "big", "expression": "amount * 2"}]},
             "position": {"x": 2, "y": 0}},
        ],
        "connections": [
            {"from_step": "s1", "to_step": "s2"},
            {"from_step": "s2", "to_step": "s3"},
        ],
    })


def _run(tmp_path, wf, on_event=None):
    return WorkflowExecutor(data_dir=str(tmp_path), on_event=on_event).execute_workflow(
        wf, preview_limit=10, full_run=True,
    )


def test_every_step_is_announced_in_order(tmp_path, chain):
    seen: list[dict] = []
    result = _run(tmp_path, chain, seen.append)
    assert result.status == "success"

    assert [(e["type"], e.get("step_id")) for e in seen] == [
        ("workflow_started", None),
        ("step_started", "s1"), ("step_completed", "s1"),
        ("step_started", "s2"), ("step_completed", "s2"),
        ("step_started", "s3"), ("step_completed", "s3"),
        ("workflow_completed", None),
    ]


def test_completion_events_carry_the_numbers_the_canvas_shows(tmp_path, chain):
    """A step_completed without a row count would light the node up and
    then leave it blank, which is worse than not animating at all."""
    seen: list[dict] = []
    _run(tmp_path, chain, seen.append)

    done = {e["step_id"]: e for e in seen if e["type"] == "step_completed"}
    assert {k: v["row_count"] for k, v in done.items()} == {"s1": 3, "s2": 2, "s3": 2}
    assert all(e["duration_ms"] is not None for e in done.values())
    assert all("timestamp" in e for e in seen)


def test_a_listener_that_raises_cannot_break_the_run(tmp_path, chain):
    def angry(_event):
        raise RuntimeError("listener exploded")

    result = _run(tmp_path, chain, angry)
    assert result.status == "success"
    assert result.step_results["s3"].row_count == 2


def test_no_listener_changes_nothing(tmp_path, chain):
    quiet = _run(tmp_path, chain, None)
    noisy = _run(tmp_path, chain, lambda _e: None)
    assert quiet.status == noisy.status == "success"
    assert {k: v.row_count for k, v in quiet.step_results.items()} \
        == {k: v.row_count for k, v in noisy.step_results.items()}


def test_a_failing_step_reports_step_error_not_step_completed(tmp_path, chain):
    broken = chain.model_copy(deep=True)
    broken.steps[1].params["condition"] = "no_such_column = 1"

    seen: list[dict] = []
    result = _run(tmp_path, broken, seen.append)

    assert result.status == "error"
    types_for_s2 = [e["type"] for e in seen if e.get("step_id") == "s2"]
    assert types_for_s2 == ["step_started", "step_error"], types_for_s2
    # And the run still closes itself out, so the canvas stops animating.
    assert seen[-1]["type"] == "workflow_completed"
    assert seen[-1]["status"] == "error"
