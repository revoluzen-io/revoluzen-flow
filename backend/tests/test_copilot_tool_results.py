"""What the Copilot reads from a tool: whole rows, and failures that are failures."""

from __future__ import annotations

import asyncio
import json

from revoluzen_flow.ai.agent import TOOL_RESULT_CHARS, fit_tool_result


def _runs(n: int) -> list[dict]:
    return [
        {
            "id": f"run{i:04d}",
            "workflow_name": f"Nightly History: Repeat Purchases {i}",
            "status": "error",
            "started_at": "2026-11-12T05:00:00+00:00",
            "error": "[WinError 10061] No connection could be made because the target machine actively refused it",
        }
        for i in range(n)
    ]


def test_a_long_result_keeps_whole_rows_and_says_how_many_it_shows():
    out = fit_tool_result({"executions": _runs(80), "total": 80})
    assert len(out) <= TOOL_RESULT_CHARS
    data = json.loads(out)  # still JSON: nothing was cut mid-row
    shown = data["executions"]
    assert 0 < len(shown) < 80
    assert all(r["workflow_name"].startswith("Nightly History: Repeat Purchases ") for r in shown)
    assert f"first {len(shown)} of 80 executions" in data["_shortened"]
    assert data["total"] == 80


def test_a_short_result_goes_as_it_is():
    payload = {"executions": _runs(3), "total": 3}
    assert json.loads(fit_tool_result(payload)) == payload


def test_failures_leave_out_the_runs_somebody_stopped(monkeypatch):
    import revoluzen_flow.main as main
    from revoluzen_flow.ai.tools.base import ToolContext
    from revoluzen_flow.ai.tools.list_executions import DEFINITION

    rows = [
        {"id": "a", "workflow_name": "Partner Price List", "status": "error", "started_at": "2026-11-12T05:00:00+00:00"},
        {"id": "b", "workflow_name": "Nightly History: Repeat Purchases", "status": "cancelled", "started_at": "2026-11-12T05:01:00+00:00"},
        {"id": "c", "workflow_name": "Orders Daily Load", "status": "success", "started_at": "2026-11-12T05:02:00+00:00"},
    ]

    class Store:
        def list_all(self, limit=200, workspace_id=None):
            return rows

    monkeypatch.setitem(main.app_state, "execution_store", Store())
    ctx = ToolContext(tenant_id="default", user_id=None, workspace_id="default", environment="dev")
    failed = asyncio.run(DEFINITION.handler({"status": "error"}, ctx))
    assert [r["id"] for r in failed["executions"]] == ["a"]
    stopped = asyncio.run(DEFINITION.handler({"status": "cancelled"}, ctx))
    assert [r["id"] for r in stopped["executions"]] == ["b"]
    # A run that recorded no row count says so, rather than "0 rows".
    assert failed["executions"][0]["rows_processed"] is None
