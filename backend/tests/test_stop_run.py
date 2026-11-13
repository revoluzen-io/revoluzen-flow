"""Stop really stops a run, and the record says who stopped it.

The editor's Stop, the Executions page's Cancel and the Pool page's Cancel
used to relabel a run "cancelled" while it carried on (and the label was
overwritten when it finished), or cancel a Python future the run did not use.
"""
from __future__ import annotations

import threading
import time

from revoluzen_flow.engine.cancellation import cancel_run, get_token, stopped_by_text
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.engine.worker_pool import QueuedJob, WorkerPool
from revoluzen_flow.ir.schema import Step, StepConnection, StepErrorType, StepType, Workflow
from revoluzen_flow.nodes.control_extras import ExecuteSqlTaskNode
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

SLOW_SQL = "SELECT count(*) FROM range(1000000) a, range(1000000) b"


def _step(step_id: str, sql: str = SLOW_SQL, **settings) -> Step:
    params = {"sql": sql}
    if settings:
        params["_settings"] = settings
    return Step(id=step_id, type=StepType.EXECUTE_SQL_TASK, label=step_id, params=params)


def _workflow(*steps: Step) -> Workflow:
    connections = [StepConnection(from_step=a.id, to_step=b.id) for a, b in zip(steps, steps[1:])]
    return Workflow(name="stop-probe", steps=list(steps), connections=connections)


def _run_in_background(workflow, tmp_path, run_id):
    box: dict = {}

    def target():
        box["result"] = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(
            workflow, run_id=run_id,
        )

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _stop_after(seconds, run_id, reason="Stopped by Test User"):
    deadline = time.time() + 10
    while get_token(run_id) is None and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(seconds)
    assert cancel_run(run_id, reason)


def test_stop_interrupts_a_running_query(tmp_path):
    run_id = "stop-probe-query"
    started = time.perf_counter()
    thread, box = _run_in_background(_workflow(_step("slow")), tmp_path, run_id)
    _stop_after(0.5, run_id)
    thread.join(timeout=15)
    took = time.perf_counter() - started

    assert not thread.is_alive(), "the run did not stop"
    assert took < 15
    result = box["result"]
    assert result.status == "cancelled"
    step = result.step_results["slow"]
    assert step.error_type == StepErrorType.CANCELLED
    assert step.error == "Stopped by Test User while this step was running."
    assert result.stop_message == "Stopped by Test User while “slow” was running."
    assert result.stopped_by_time_limit is False


def test_stop_between_steps_runs_nothing_more(tmp_path, monkeypatch):
    def blocking(self, ctx):
        time.sleep(1.0)
        return ctx.conn.sql("SELECT 1 AS done")

    monkeypatch.setattr(ExecuteSqlTaskNode, "execute", blocking)
    run_id = "stop-probe-between"
    thread, box = _run_in_background(_workflow(_step("wait"), _step("after")), tmp_path, run_id)
    _stop_after(0.3, run_id)
    thread.join(timeout=15)

    result = box["result"]
    assert result.status == "cancelled"
    assert result.step_results["wait"].status == "success"
    assert "after" not in result.step_results
    assert result.stop_message == (
        "Stopped by Test User before “after” started, "
        "so it and the steps after it did not run."
    )


def test_stop_cuts_retries_short(tmp_path, monkeypatch):
    def failing(self, ctx):
        raise RuntimeError("the partner API is down")

    monkeypatch.setattr(ExecuteSqlTaskNode, "execute", failing)
    step = _step("flaky", retry_on_fail=True, max_retries=10, retry_delay_ms=500,
                 retry_strategy="fixed")
    run_id = "stop-probe-retries"
    started = time.perf_counter()
    thread, box = _run_in_background(_workflow(step), tmp_path, run_id)
    _stop_after(0.6, run_id)
    thread.join(timeout=15)

    assert time.perf_counter() - started < 3
    result = box["result"]
    assert result.status == "cancelled"
    assert result.step_results["flaky"].error_type == StepErrorType.CANCELLED


def test_a_run_stopped_while_waiting_for_a_slot_never_takes_one():
    pool = WorkerPool(max_workers=1)
    release = threading.Event()
    holder = QueuedJob(id="holder-run", workflow_id="wf-a", workflow_name="Holder")
    waiter = QueuedJob(id="waiting-run", workflow_id="wf-b", workflow_name="Waiter")
    calls: list[str] = []

    first = threading.Thread(
        target=lambda: pool.run_admitted(holder, lambda: release.wait(10)), daemon=True,
    )
    first.start()
    deadline = time.time() + 5
    while "holder-run" not in pool.live_run_ids() and time.time() < deadline:
        time.sleep(0.05)

    box: dict = {}
    second = threading.Thread(
        target=lambda: box.setdefault(
            "result", pool.run_admitted(waiter, lambda: calls.append("ran") or "stopped-run"),
        ),
        daemon=True,
    )
    second.start()
    deadline = time.time() + 5
    while "waiting-run" not in pool.live_run_ids(workflow_id="wf-b") and time.time() < deadline:
        time.sleep(0.05)
    assert "waiting-run" in pool.live_run_ids(workflow_id="wf-b")

    started = time.perf_counter()
    assert cancel_run("waiting-run", "Stopped by Test User", create=True)
    second.join(timeout=5)
    assert not second.is_alive(), "the stopped run kept waiting for a slot"
    assert time.perf_counter() - started < 3
    # It ran its work without a slot, so the executor can record it as
    # stopped; the slot stayed with the holder the whole time.
    assert box["result"] == "stopped-run"
    assert "waiting-run" not in pool.live_run_ids()
    assert "holder-run" in pool.live_run_ids()

    release.set()
    first.join(timeout=5)


def test_the_reason_names_who_stopped_it():
    class User:
        name = "Priya"
        email = "priya@example.com"

    class NoName:
        name = ""
        email = "ops@example.com"

    assert stopped_by_text(User()) == "Stopped by Priya"
    assert stopped_by_text(NoName()) == "Stopped by ops@example.com"
    assert stopped_by_text(None) == "Stopped from the app"


# ── Through the API ──────────────────────────────────────────────────


def test_a_run_started_by_hand_is_listed_while_it_runs_then_once_stopped(authed_client):
    created = authed_client.post("/api/workflows", json={
        "name": f"listed-{time.perf_counter_ns()}",
        "steps": [{"id": "slow", "type": "execute_sql_task", "label": "Slow query",
                   "params": {"sql": SLOW_SQL}}],
        "connections": [],
    })
    assert created.status_code in (200, 201), created.text
    body = created.json()
    wid = body.get("id") or body["workflow"]["id"]

    def listed():
        runs = authed_client.get("/api/monitor/executions", params={"workflow_id": wid}).json()
        return runs if isinstance(runs, list) else runs.get("executions") or runs.get("items") or []

    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.update(response=authed_client.post(f"/api/execute/workflow/{wid}", params={"full_run": True})),
        daemon=True,
    )
    thread.start()

    seen = None
    deadline = time.time() + 15
    while time.time() < deadline and seen is None:
        rows = listed()
        if rows and rows[0]["status"] == "running":
            seen = rows[0]
        else:
            time.sleep(0.1)
    assert seen is not None, "the run was not listed until it had ended"

    deadline = time.time() + 15
    while time.time() < deadline:
        if authed_client.post(f"/api/workflows/{wid}/cancel").json()["stopped"]:
            break
        time.sleep(0.2)
    thread.join(timeout=20)
    assert not thread.is_alive()

    rows = listed()
    assert len(rows) == 1, "the run was recorded twice"
    assert rows[0]["id"] == seen["id"] and rows[0]["status"] == "cancelled"


def test_the_stop_endpoint_stops_the_pipelines_run(authed_client):
    created = authed_client.post("/api/workflows", json={
        "name": f"stop-api-{time.perf_counter_ns()}",
        "steps": [{"id": "slow", "type": "execute_sql_task", "label": "Slow query",
                   "params": {"sql": SLOW_SQL}}],
        "connections": [],
    })
    assert created.status_code in (200, 201), created.text
    body = created.json()
    wid = body.get("id") or body["workflow"]["id"]

    box: dict = {}

    def run():
        box["response"] = authed_client.post(
            f"/api/execute/workflow/{wid}", params={"full_run": True},
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    stopped = None
    deadline = time.time() + 15
    while time.time() < deadline:
        r = authed_client.post(f"/api/workflows/{wid}/cancel")
        assert r.status_code == 200, r.text
        if r.json()["stopped"]:
            stopped = r.json()
            break
        time.sleep(0.2)
    assert stopped is not None, "the run never showed up as live"

    thread.join(timeout=20)
    assert not thread.is_alive(), "the run carried on after Stop"
    run_body = box["response"].json()
    assert run_body["status"] == "cancelled"

    runs = authed_client.get("/api/monitor/executions", params={"workflow_id": wid}).json()
    runs = runs if isinstance(runs, list) else runs.get("executions") or runs.get("items") or []
    record = runs[0]
    assert record["status"] == "cancelled"
    message = record.get("error_message") or ""
    assert message.startswith("Stopped by ")
    # Depending on timing the stop lands before the step or during it.
    assert (
        "while “Slow query” was running." in message
        or "before “Slow query” started" in message
    ), message
