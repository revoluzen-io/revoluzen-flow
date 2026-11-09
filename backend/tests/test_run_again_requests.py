"""Request-level coverage for the three "run it again" endpoints.

Replay shipped returning 500 on every call — a bad function-local import
— and the whole suite stayed green, because nothing ever sent a request
to it. `test_execution_ephemeral.py` exercises only the validation gate,
which returns before the execution path, so a crash past that gate was
invisible too.

These tests send real HTTP through the app and assert the execution path
itself responds. The load-bearing assertion in every one is **not 500**:
the bugs this file exists to catch (ImportError on a function-local
import, NameError on an out-of-scope local) all surface as a 500 and as
nothing else. A pipeline failing for a legitimate reason still returns
200 with a status in the body, so "not 500" and "ran" are different
claims and both are made below.

The fixture pipeline uses `db_source` with an inline query, which runs
against the run's own in-memory DuckDB — no connection, no file, no
network, so a failure here is the endpoint's fault and not the
environment's.
"""

from __future__ import annotations

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


RUNNABLE_STEPS = [
    {
        "id": "src",
        "type": "db_source",
        "label": "inline",
        # No connection_id → the executor runs this against the run's own
        # DuckDB. Self-contained on purpose.
        "params": {"query": "SELECT 1 AS id, 'a' AS name"},
        "position": {"x": 0, "y": 0},
    },
]


def _body(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_raw": resp.text[:400]}


def _assert_not_500(resp, what: str):
    """The assertion this file exists for.

    A 500 here means the endpoint crashed before doing its job — the
    exact signature of both bugs that reached a running server with the
    suite green.
    """
    assert resp.status_code != 500, (
        f"{what} returned 500 — the execution path crashed.\n"
        f"body: {_body(resp)}"
    )


def _assert_reached_the_executor(resp, what: str):
    """Stronger than "not 500": the run actually produced an outcome.

    A saved pipeline cannot be made to SUCCEED connection-free here:
    `ir.migrations` rewrites `db_source` to `source` +
    `connector_type='database'` when the workflow is persisted, so the
    saved fixture needs a connection it deliberately does not have.
    Ephemeral runs skip that migration, so the success path is asserted
    strictly there instead (`test_ephemeral_run_executes`).

    What matters for these endpoints is that the request reached the
    executor and came back with a terminal result, rather than dying on
    an import or a missing local.
    """
    _assert_not_500(resp, what)
    assert resp.status_code == 200, _body(resp)
    body = _body(resp)
    assert body.get("status") in ("success", "error"), (
        f"{what} produced no run outcome: {body}"
    )
    assert body.get("execution_id") or body.get("id"), (
        f"{what} recorded no execution: {body}"
    )


@pytest.fixture
def pipeline_id(authed_client):
    """A saved, runnable pipeline."""
    r = authed_client.post("/api/workflows", json={
        "name": "run-again-probe",
        "steps": RUNNABLE_STEPS,
        "connections": [],
    })
    assert r.status_code in (200, 201), f"could not create pipeline: {_body(r)}"
    wf = _body(r)
    wid = wf.get("id") or wf.get("workflow", {}).get("id")
    assert wid, f"no id in create response: {wf}"
    return wid


@pytest.fixture
def finished_run(authed_client, pipeline_id):
    """One completed run, to replay and resume from."""
    r = authed_client.post(f"/api/execute/workflow/{pipeline_id}")
    _assert_not_500(r, "initial run")
    assert r.status_code == 200, _body(r)
    body = _body(r)
    exe_id = body.get("execution_id") or body.get("id")
    assert exe_id, f"run returned no execution id: {body}"
    return exe_id


# ── Re-run ───────────────────────────────────────────────────────────

def test_rerun_executes(authed_client, pipeline_id):
    r = authed_client.post(f"/api/execute/workflow/{pipeline_id}")
    _assert_reached_the_executor(r, "Re-run")


def test_rerun_in_sample_mode_executes(authed_client, pipeline_id):
    r = authed_client.post(f"/api/execute/workflow/{pipeline_id}?safety_mode=sample")
    _assert_not_500(r, "Re-run (sample)")
    assert r.status_code == 200


# ── Replay ───────────────────────────────────────────────────────────

def test_replay_executes(authed_client, finished_run):
    """THE regression. This returned 500 on every call:
    `from revoluzen_flow.monitoring.store import get_execution_store`
    imported a name that module does not define."""
    r = authed_client.post(f"/api/execute/replay/{finished_run}")
    _assert_reached_the_executor(r, "Replay")
    assert _body(r).get("replay_of") == finished_run, (
        "the replay is not linked to the run it came from"
    )


def test_replay_creates_a_new_execution(authed_client, finished_run):
    """A replay is a new run, not a re-read of the old record."""
    r = authed_client.post(f"/api/execute/replay/{finished_run}")
    _assert_not_500(r, "Replay")
    new_id = _body(r).get("execution_id") or _body(r).get("id")
    assert new_id and new_id != finished_run, (
        f"replay reused the source run id {finished_run}"
    )


def test_replay_of_unknown_execution_is_404_not_500(authed_client):
    """A missing id is a client error. 500 would hide a real crash."""
    r = authed_client.post("/api/execute/replay/does-not-exist-abcdef")
    _assert_not_500(r, "Replay (unknown id)")
    assert r.status_code in (400, 404), _body(r)


# ── Resume from failed ───────────────────────────────────────────────

def test_resume_executes(authed_client, pipeline_id, finished_run):
    r = authed_client.post(
        f"/api/execute/workflow/{pipeline_id}/resume?run_id={finished_run}"
    )
    _assert_reached_the_executor(r, "Resume from failed")


def test_resume_records_a_new_linked_run(authed_client, pipeline_id, finished_run):
    """Resume used to return a result and write nothing, so the run never
    appeared in history and the page stayed on the failed run."""
    r = authed_client.post(
        f"/api/execute/workflow/{pipeline_id}/resume?run_id={finished_run}"
    )
    new_id = _body(r).get("execution_id")
    assert new_id and new_id != finished_run

    run = _body(authed_client.get(f"/api/monitor/executions/{new_id}"))
    assert run["workflow_id"] == pipeline_id
    assert run["triggered_by"] == f"resume:{finished_run[:8]}"
    assert run["metadata"]["resumed_from"] == finished_run
    assert run["status"] == _body(r)["status"]


def test_resume_of_a_run_from_another_pipeline_is_404(authed_client, finished_run):
    other = authed_client.post("/api/workflows", json={
        "name": "other-pipeline", "steps": RUNNABLE_STEPS, "connections": [],
    })
    other_id = _body(other).get("id") or _body(other)["workflow"]["id"]
    r = authed_client.post(
        f"/api/execute/workflow/{other_id}/resume?run_id={finished_run}"
    )
    assert r.status_code == 404, _body(r)


def test_run_keeps_the_version_it_executed(authed_client, pipeline_id, finished_run):
    """The run page showed the pipeline's current version as the run's."""
    first = _body(authed_client.get(f"/api/monitor/executions/{finished_run}"))
    ran_at = first["workflow_version"]
    assert ran_at is not None

    current = _body(authed_client.get(f"/api/workflows/{pipeline_id}"))
    wf = current.get("workflow") or current
    saved = authed_client.put(f"/api/workflows/{pipeline_id}", json={
        **wf, "name": "run-again-probe-renamed",
    })
    assert saved.status_code == 200, _body(saved)
    assert _body(authed_client.get(f"/api/workflows/{pipeline_id}"))["version"] > ran_at

    again = _body(authed_client.get(f"/api/monitor/executions/{finished_run}"))
    assert again["workflow_version"] == ran_at


def test_public_run_route_ignores_a_resume_query_param(authed_client, pipeline_id, finished_run):
    """Only the resume route, which checks the run, may start a resume."""
    r = authed_client.post(
        f"/api/execute/workflow/{pipeline_id}?resume_from_run_id={finished_run}"
    )
    run = _body(authed_client.get(f"/api/monitor/executions/{_body(r)['execution_id']}"))
    assert run["triggered_by"] == "manual"


def test_resume_without_run_id_is_422_not_500(authed_client, pipeline_id):
    """`run_id` is a required query param — a missing one is validation."""
    r = authed_client.post(f"/api/execute/workflow/{pipeline_id}/resume")
    _assert_not_500(r, "Resume (no run_id)")
    assert r.status_code == 422, _body(r)


# ── Canvas Run / Sample (ephemeral) ──────────────────────────────────

def test_ephemeral_run_executes(authed_client):
    """Broken during the admission refactor by an out-of-scope
    `environment` local — a 500 on every canvas Run, suite still green."""
    r = authed_client.post("/api/execute/workflow/ephemeral", json={
        "workflow": {"name": "probe", "steps": RUNNABLE_STEPS, "connections": []},
        "safety_mode": "live",
        "environment": "dev",
    })
    _assert_not_500(r, "Ephemeral run")
    assert r.status_code == 200, _body(r)
    assert _body(r).get("status") == "success", _body(r)


def test_ephemeral_sample_executes(authed_client):
    r = authed_client.post("/api/execute/workflow/ephemeral", json={
        "workflow": {"name": "probe", "steps": RUNNABLE_STEPS, "connections": []},
        "safety_mode": "sample",
        "environment": "dev",
    })
    _assert_not_500(r, "Ephemeral sample")
    assert r.status_code == 200, _body(r)


# ── The pool sees them ───────────────────────────────────────────────

def test_runs_are_counted_by_the_pool(authed_client, pipeline_id):
    """Every path goes through admission, so the counters must move.

    Guards against a future edit quietly bypassing the single admission
    point and re-creating the un-metered run paths.
    """
    before = _body(authed_client.get("/api/pool/status")).get("counters", {})
    authed_client.post(f"/api/execute/workflow/{pipeline_id}")
    after = _body(authed_client.get("/api/pool/status")).get("counters", {})
    assert after.get("total_submitted", 0) > before.get("total_submitted", 0), (
        "the run did not pass through pool admission"
    )
