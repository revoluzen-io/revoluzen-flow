"""A run stops at its time limit, is marked failed, and says why.

The Save dialog's "Maximum Running Time" used to flag a run only after it had
finished; nothing stopped it. A published endpoint stored a timeout that
nothing read. And a cancelled run fell through to "success".
"""
from __future__ import annotations

import time

import pytest

from revoluzen_flow.engine.cancellation import active_token_count, create_token
from revoluzen_flow.engine.executor import WorkflowExecutor
from revoluzen_flow.engine.run_time_limit import (
    TIME_LIMIT_STEP_ID,
    configured_limit_seconds,
    describe_limit,
    effective_limit_seconds,
)
from revoluzen_flow.ir.schema import Step, StepConnection, StepErrorType, StepType, Workflow
from revoluzen_flow.nodes.control_extras import ExecuteSqlTaskNode
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

# A cross join of a trillion rows: never finishes inside a test, so only the
# limit can end it.
SLOW_SQL = "SELECT count(*) FROM range(1000000) a, range(1000000) b"


def _sql_step(step_id: str, sql: str = SLOW_SQL, **settings) -> Step:
    params = {"sql": sql}
    if settings:
        params["_settings"] = settings
    return Step(id=step_id, type=StepType.EXECUTE_SQL_TASK, label=step_id, params=params)


def _workflow(*steps: Step, minutes: float | None = None, chain: bool = False) -> Workflow:
    metadata = {}
    if minutes is not None:
        metadata["execution_settings"] = {"max_runtime_minutes": minutes, "enable_timeout": True}
    connections = []
    if chain:
        connections = [
            StepConnection(from_step=a.id, to_step=b.id) for a, b in zip(steps, steps[1:])
        ]
    return Workflow(
        name="time-limit-probe", steps=list(steps), connections=connections, metadata=metadata,
    )


def _run(workflow: Workflow, tmp_path, **kwargs):
    started = time.perf_counter()
    result = WorkflowExecutor(data_dir=str(tmp_path)).execute_workflow(workflow, **kwargs)
    return result, time.perf_counter() - started


# ── The limit stops the run ──────────────────────────────────────────


def test_a_query_past_the_limit_is_interrupted_and_the_run_fails(tmp_path):
    result, took = _run(_workflow(_sql_step("slow"), minutes=0.02), tmp_path)

    assert took < 15, f"the run was not stopped (took {took:.1f}s)"
    assert result.status == "error"
    assert result.stopped_by_time_limit is True
    assert result.time_limit_seconds == pytest.approx(1.2)
    step = result.step_results["slow"]
    assert step.status == "error"
    assert step.error_type == StepErrorType.TIMEOUT
    assert step.error == (
        "Stopped: the run went past its time limit of 1.2 seconds while this step was running."
    )


def test_the_shorter_of_the_two_limits_wins(tmp_path):
    result, took = _run(
        _workflow(_sql_step("slow"), minutes=30), tmp_path, time_limit_seconds=1,
    )
    assert took < 15
    assert result.status == "error"
    assert result.time_limit_seconds == 1
    assert result.step_results["slow"].error_type == StepErrorType.TIMEOUT


def test_a_step_that_cannot_be_interrupted_stops_the_run_before_the_next(tmp_path, monkeypatch):
    """Plain Python (an HTTP call, a sleep) can't be interrupted, so the run
    stops when it returns, and the next step never starts."""
    def blocking(self, ctx):
        time.sleep(1.2)
        return ctx.conn.sql("SELECT 1 AS done")

    monkeypatch.setattr(ExecuteSqlTaskNode, "execute", blocking)
    workflow = _workflow(_sql_step("wait"), _sql_step("after"), chain=True)

    result, took = _run(workflow, tmp_path, time_limit_seconds=0.4)

    assert took < 10
    assert result.status == "error"
    assert result.stopped_by_time_limit is True
    assert result.step_results["wait"].status == "success"
    assert "after" not in result.step_results
    stop = result.step_results[TIME_LIMIT_STEP_ID]
    assert stop.status == "error"
    assert stop.error_type == StepErrorType.TIMEOUT
    assert stop.error == (
        "Stopped: the run went past its time limit of 0.4 seconds, "
        "so the remaining steps did not run."
    )


def test_retries_stop_at_the_limit(tmp_path, monkeypatch):
    """A failing step with generous retries would otherwise keep going."""
    calls = {"n": 0}

    def failing(self, ctx):
        calls["n"] += 1
        raise RuntimeError("the partner API is down")

    monkeypatch.setattr(ExecuteSqlTaskNode, "execute", failing)
    step = _sql_step(
        "flaky", retry_on_fail=True, max_retries=10, retry_delay_ms=500, retry_strategy="fixed",
    )

    result, took = _run(_workflow(step), tmp_path, time_limit_seconds=1)

    assert took < 3, f"retries carried on past the limit (took {took:.1f}s)"
    assert calls["n"] <= 4
    assert result.status == "error"
    assert result.step_results["flaky"].error_type == StepErrorType.TIMEOUT


def test_a_step_set_to_continue_on_error_still_stops_the_run(tmp_path):
    step = _sql_step("slow", on_error="continue")
    result, took = _run(_workflow(step, _sql_step("after", "SELECT 1"), chain=True), tmp_path,
                        time_limit_seconds=1)
    assert took < 15
    assert result.status == "error"
    assert result.step_results["slow"].status == "error"
    assert result.step_results["slow"].error_type == StepErrorType.TIMEOUT
    assert "after" not in result.step_results


# ── Without a limit nothing changes ──────────────────────────────────


def test_a_pipeline_without_a_limit_runs_as_before(tmp_path):
    tokens_before = active_token_count()
    result, _ = _run(_workflow(_sql_step("quick", "SELECT 42 AS answer")), tmp_path)
    assert result.status == "success"
    assert result.time_limit_seconds is None
    assert result.stopped_by_time_limit is False
    assert active_token_count() == tokens_before


def test_a_run_inside_its_limit_succeeds(tmp_path):
    result, _ = _run(_workflow(_sql_step("quick", "SELECT 42 AS answer"), minutes=5), tmp_path)
    assert result.status == "success"
    assert result.time_limit_seconds == 300
    assert result.stopped_by_time_limit is False


def test_a_cancelled_run_is_not_reported_as_success(tmp_path):
    run_id = "cancel-probe-run"
    create_token(run_id).cancel()
    result, _ = _run(_workflow(_sql_step("quick", "SELECT 1")), tmp_path, run_id=run_id)
    assert result.status == "cancelled"
    assert "quick" not in result.step_results


# ── Reading the setting ──────────────────────────────────────────────


@pytest.mark.parametrize("settings, expected", [
    (None, None),
    ({}, None),
    ({"max_runtime_minutes": 0, "enable_timeout": True}, None),
    ({"max_runtime_minutes": 30, "enable_timeout": False}, None),
    ({"max_runtime_minutes": "not a number"}, None),
    ({"max_runtime_minutes": -5}, None),
    ({"max_runtime_minutes": 30}, 1800),
    ({"max_runtime_minutes": 30, "enable_timeout": True}, 1800),
])
def test_the_save_dialog_setting_is_read(settings, expected):
    metadata = {} if settings is None else {"execution_settings": settings}
    assert configured_limit_seconds(Workflow(name="x", metadata=metadata)) == expected


def test_limits_combine_to_the_shortest():
    assert effective_limit_seconds(None, None) is None
    assert effective_limit_seconds(1800, None) == 1800
    assert effective_limit_seconds(1800, 300) == 300
    assert effective_limit_seconds(0, 300) == 300
    assert effective_limit_seconds("bad", 60) == 60


@pytest.mark.parametrize("seconds, words", [
    (1, "1 second"),
    (1.2, "1.2 seconds"),
    (45, "45 seconds"),
    (60, "1 minute"),
    (1800, "30 minutes"),
    (3600, "1 hour"),
    (5400, "1 hour 30 minutes"),
    (7200, "2 hours"),
])
def test_limits_are_described_in_words(seconds, words):
    assert describe_limit(seconds) == words


# ── Through the API ──────────────────────────────────────────────────


def _save(authed_client, minutes=None):
    r = authed_client.post("/api/workflows", json={
        "name": f"time-limit-api-{time.perf_counter_ns()}",
        "steps": [{"id": "slow", "type": "execute_sql_task", "label": "Slow query",
                   "params": {"sql": SLOW_SQL}}],
        "connections": [],
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    wid = body.get("id") or body["workflow"]["id"]
    if minutes is not None:
        # The way the Save dialog stores it: a save after creating.
        wf = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        wf["metadata"] = {
            **(wf.get("metadata") or {}),
            "execution_settings": {"max_runtime_minutes": minutes, "enable_timeout": True},
        }
        saved = authed_client.put(f"/api/workflows/{wid}", json={"workflow": wf})
        assert saved.status_code == 200, saved.text
        stored = authed_client.get(f"/api/workflows/{wid}").json()["workflow"]
        assert stored["metadata"]["execution_settings"]["max_runtime_minutes"] == minutes
    return wid


def test_the_run_button_path_stops_and_records_the_reason(authed_client):
    wid = _save(authed_client, minutes=0.02)
    started = time.perf_counter()
    r = authed_client.post(f"/api/execute/workflow/{wid}", params={"full_run": True})
    took = time.perf_counter() - started
    assert r.status_code == 200, r.text
    body = r.json()
    assert took < 20
    assert body["status"] == "error"
    assert body["timeout_exceeded"] is True
    assert body["timeout_message"] == "Stopped at its time limit of 1.2 seconds."

    runs = authed_client.get("/api/monitor/executions", params={"workflow_id": wid}).json()
    runs = runs if isinstance(runs, list) else runs.get("executions") or runs.get("items") or []
    assert runs, "the stopped run was not recorded"
    record = runs[0]
    assert record["status"] == "error"
    assert "time limit of 1.2 seconds" in (record.get("error_message") or "")


def test_a_published_endpoint_stops_at_its_timeout(client, authed_client):
    wid = _save(authed_client)
    path = f"time-limit/{time.perf_counter_ns()}"
    published = authed_client.post("/api/gateway/endpoints", json={
        "workflow_id": wid, "path": path, "name": "probe",
        "require_api_key": False, "timeout_seconds": 1,
    })
    assert published.status_code == 200, published.text

    started = time.perf_counter()
    r = client.post(f"/api/published/{path}", json={})
    took = time.perf_counter() - started
    assert took < 20, f"the endpoint's timeout was ignored (took {took:.1f}s)"
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error"
