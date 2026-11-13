"""After-pipeline triggers: a pipeline runs when another one finishes.

Unit tests drive the event record and the dispatcher with a fake scheduler and
a real SQLite database: which outcomes fire, which runs count, that loops are
refused when saved and again at run time, that a busy trigger runs once more
rather than once per finish, and that a pipeline which cannot run drops the
event with a reason.

The last class runs it end to end: pipeline A is started by another
application through its published endpoint, and pipeline B, set to run after
A, runs at its published version, recorded as started after A.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from revoluzen_flow.ir.schema import PipelineStatus, Step, StepType, Workflow
from revoluzen_flow.ir.versioning import WorkflowStore
from revoluzen_flow.monitoring.store import ExecutionStore
from revoluzen_flow.scheduling.models import Schedule, ScheduleType
from revoluzen_flow.scheduling.store import ScheduleStore
from revoluzen_flow.storage.database import Database
from revoluzen_flow.triggers.chain import (
    MAX_CHAIN_LENGTH,
    dispatch_events,
    find_loop,
    on_run_finished,
    outcome_matches,
)
from revoluzen_flow.triggers.store import TriggerStore


class FakeScheduler:
    def __init__(self):
        self.started: list[dict] = []
        self.busy: set[str] = set()
        self.refuse = False
        self.woken = 0

    def is_trigger_busy(self, trigger_id):
        return trigger_id in self.busy

    def start_trigger_run(self, row, **options):
        if self.refuse:
            return False
        self.started.append({"row": row, **options})
        return True

    def wake_after_pipeline_triggers(self):
        self.woken += 1


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "chain.db"))
    scheduler = FakeScheduler()
    state = {
        "db": db,
        "store": WorkflowStore(db=db),
        "schedule_store": ScheduleStore(db=db),
        "execution_store": ExecutionStore(db=db),
        "scheduler": scheduler,
        "data_dir": str(tmp_path),
    }
    return SimpleNamespace(state=state, scheduler=scheduler, events=TriggerStore(db))


def _pipeline(env, name, *, published=True):
    saved = env.state["store"].save(Workflow(
        name=name,
        steps=[Step(id="s", type=StepType.TRANSFORM, label="S", params={"expression": "1"})],
    ))
    if published:
        env.state["store"].update_status(saved.workflow.id, PipelineStatus.PUBLISHED)
    return saved.workflow.id


def _after(env, workflow_id, upstream_id, outcome="success", **fields):
    schedule = env.state["schedule_store"].create(Schedule(
        workflow_id=workflow_id, schedule_type=ScheduleType.AFTER_PIPELINE,
        after_workflow_id=upstream_id, after_outcome=outcome, **fields,
    ))
    return schedule.id


def _finish(env, workflow_id, status="success", triggered_by="schedule", execution_id="exe-a", chain=None):
    return on_run_finished(
        env.state, workflow_id=workflow_id, workspace_id="default", workflow_name="A",
        status=status, execution_id=execution_id, triggered_by=triggered_by,
        run_metadata={"chain": chain} if chain else None,
    )


class TestWhichRunsCount:
    @pytest.mark.parametrize("wanted,status,fires", [
        ("success", "success", True), ("success", "error", False),
        ("failure", "error", True), ("failure", "success", False),
        ("always", "success", True), ("always", "error", True),
        ("always", "cancelled", False), ("always", "skipped", False),
    ])
    def test_outcomes(self, wanted, status, fires):
        assert outcome_matches(wanted, status) is fires

    @pytest.mark.parametrize("trigger,counts", [
        ("schedule", True), ("file_arrival", True), ("after_pipeline", True), ("api", True), ("webhook", True),
        ("manual", False), ("websocket", False), ("test", False), ("backfill", False), ("replay:abc12345", False),
    ])
    def test_only_runs_nobody_is_watching_start_the_next(self, env, trigger, counts):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        _after(env, b, a)
        assert _finish(env, a, triggered_by=trigger) == (1 if counts else 0)

    def test_switched_off_and_other_outcomes_do_not_fire(self, env):
        a, b, c, d = (_pipeline(env, n) for n in "ABCD")
        _after(env, b, a, "success")
        _after(env, c, a, "failure")
        _after(env, d, a, "always", enabled=False)
        assert _finish(env, a, status="error") == 1
        assert [e.trigger_id for e in env.events.pending_events()] != []
        assert env.scheduler.woken == 1


class TestLoops:
    def test_a_pipeline_cannot_run_after_itself(self, env):
        a = _pipeline(env, "A")
        assert find_loop(env.state["schedule_store"], "default", a, a) == [a, a]

    def test_a_loop_through_others_is_found_with_its_path(self, env):
        a, b, c = (_pipeline(env, n) for n in "ABC")
        _after(env, b, a)  # A -> B
        _after(env, c, b)  # B -> C
        assert find_loop(env.state["schedule_store"], "default", c, a) == [a, b, c, a]
        assert find_loop(env.state["schedule_store"], "default", a, c) is None

    def test_the_trigger_being_edited_does_not_count_against_itself(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        trigger = _after(env, b, a)
        assert find_loop(env.state["schedule_store"], "default", b, a) == [a, b, a]
        # Turning "B after A" into "A after B" removes the old edge.
        assert find_loop(env.state["schedule_store"], "default", b, a, ignore_trigger_id=trigger) is None

    def test_a_loop_made_some_other_way_stops_at_run_time(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        trigger = _after(env, b, a)
        _finish(env, a, chain=[b])  # B led to A, and A would now start B again
        assert dispatch_events(env.scheduler, env.state) == 0
        assert env.scheduler.started == []
        assert env.events.pending_events() == []
        assert "loop" in env.state["schedule_store"].get(trigger).status_message

    def test_a_chain_too_long_stops(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        _after(env, b, a)
        _finish(env, a, chain=[f"p{i}" for i in range(MAX_CHAIN_LENGTH)])
        assert dispatch_events(env.scheduler, env.state) == 0


class TestDispatch:
    def test_starts_the_trigger_with_what_led_to_it(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        trigger = _after(env, b, a)
        _finish(env, a, execution_id="exe-a-1")
        assert dispatch_events(env.scheduler, env.state) == 1
        run = env.scheduler.started[0]
        assert run["row"]["id"] == trigger
        assert run["triggered_by"] == "after_pipeline"
        assert run["run_metadata"]["after_pipeline"] == {
            "workflow_id": a, "workflow_name": "A", "execution_id": "exe-a-1", "status": "success",
        }
        assert run["run_metadata"]["chain"] == [a]
        assert env.events.pending_events() == []
        assert 'Started because "A" succeeded' in env.state["schedule_store"].get(trigger).status_message

    def test_a_busy_trigger_runs_once_more_not_once_per_finish(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        trigger = _after(env, b, a)
        env.scheduler.busy.add(trigger)
        _finish(env, a, execution_id="exe-1")
        assert dispatch_events(env.scheduler, env.state) == 0
        _finish(env, a, execution_id="exe-2")
        assert [e.execution_id for e in env.events.pending_events()] == ["exe-2"]
        env.scheduler.busy.discard(trigger)
        assert dispatch_events(env.scheduler, env.state) == 1
        assert dispatch_events(env.scheduler, env.state) == 0
        assert env.scheduler.started[0]["run_metadata"]["after_pipeline"]["execution_id"] == "exe-2"

    def test_an_unpublished_pipeline_is_not_started_and_says_why(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B", published=False)
        trigger = _after(env, b, a)
        _finish(env, a)
        assert dispatch_events(env.scheduler, env.state) == 0
        assert env.events.pending_events() == [], "an old finish must not start it whenever it is published"
        assert "not published" in env.state["schedule_store"].get(trigger).status_message

    def test_the_overlap_setting_makes_it_wait(self, env):
        from revoluzen_flow.monitoring.store import ExecutionRecord

        a = _pipeline(env, "A")
        b_saved = env.state["store"].save(Workflow(
            name="B", steps=[Step(id="s", type=StepType.TRANSFORM, label="S", params={"expression": "1"})],
            metadata={"execution_settings": {"enable_overlap_detection": True, "overlap_policy": "queue"}},
        ))
        b = b_saved.workflow.id
        env.state["store"].update_status(b, PipelineStatus.PUBLISHED)
        env.state["execution_store"].record(ExecutionRecord(workflow_id=b, workflow_name="B", status="running"))
        trigger = _after(env, b, a)
        _finish(env, a)
        assert dispatch_events(env.scheduler, env.state) == 0
        assert len(env.events.pending_events()) == 1, "it waits for the other run of B"
        assert "overlap setting is queue" in env.state["schedule_store"].get(trigger).status_message

    def test_a_trigger_turned_off_or_pointed_elsewhere_drops_the_event(self, env):
        a, b, c = (_pipeline(env, n) for n in "ABC")
        trigger = _after(env, b, a)
        _finish(env, a)
        env.state["schedule_store"].update(trigger, {"after_workflow_id": c})
        assert dispatch_events(env.scheduler, env.state) == 0
        assert env.events.pending_events() == []

    def test_turned_away_at_capacity_it_tries_again(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        _after(env, b, a)
        _finish(env, a)
        env.scheduler.refuse = True
        assert dispatch_events(env.scheduler, env.state) == 0
        assert len(env.events.pending_events()) == 1
        env.scheduler.refuse = False
        assert dispatch_events(env.scheduler, env.state) == 1


class TestLegacyEventRows:
    def test_an_old_event_row_reads_as_the_trigger_it_described_switched_off(self, env):
        db = env.state["db"]
        db.insert_json("schedules", "legacy1", {
            "id": "legacy1", "workflow_id": "b", "schedule_type": "event",
            "event_trigger": "pipeline_completed", "event_source_id": "a", "enabled": True,
        }, workflow_id="b", project_id="default", workspace_id="default", enabled=1,
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
        store = env.state["schedule_store"]
        row = store.list_all()[0]
        assert row["schedule_type"] == "after_pipeline" and row["after_workflow_id"] == "a"
        assert row["enabled"] is False and "event_trigger" not in row
        assert store.get("legacy1").schedule_type == ScheduleType.AFTER_PIPELINE

    def test_an_old_file_event_has_no_folder_and_stays_off(self, env):
        env.state["db"].insert_json("schedules", "legacy2", {
            "id": "legacy2", "workflow_id": "b", "schedule_type": "event", "event_trigger": "file_uploaded",
            "enabled": True,
        }, workflow_id="b", project_id="default", workspace_id="default", enabled=1,
            created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
        schedule = env.state["schedule_store"].get("legacy2")
        assert schedule.schedule_type == ScheduleType.FILE_ARRIVAL and not schedule.enabled

    def test_a_status_note_does_not_undo_an_edit(self, env):
        a, b = _pipeline(env, "A"), _pipeline(env, "B")
        trigger = _after(env, b, a)
        store = env.state["schedule_store"]
        store.update(trigger, {"name": "Edited meanwhile"})
        store.record_check(trigger, "Started because A succeeded.")
        saved = store.get(trigger)
        assert saved.name == "Edited meanwhile" and saved.status_message == "Started because A succeeded."


# ── End to end ───────────────────────────────────────────────────────────

from tests.conftest_fixtures_v2 import (  # noqa: E402,F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


@pytest.fixture(scope="module", autouse=True)
def _no_scheduler_loop():
    old = os.environ.get("REVOLUZEN_FLOW_ENABLE_SCHEDULER")
    os.environ["REVOLUZEN_FLOW_ENABLE_SCHEDULER"] = "0"
    yield
    if old is None:
        os.environ.pop("REVOLUZEN_FLOW_ENABLE_SCHEDULER", None)
    else:
        os.environ["REVOLUZEN_FLOW_ENABLE_SCHEDULER"] = old


def _make(authed_client, name, condition="amount >= 0"):
    from revoluzen_flow.main import app_state

    r = authed_client.post("/api/workflows", json={
        "name": name,
        "steps": [
            {"id": "src", "type": "source", "label": "Orders",
             "params": {"connector_type": "csv", "file_path": "orders.csv", "header": True}},
            {"id": "flt", "type": "filter", "label": "Keep", "params": {"condition": condition}},
        ],
        "connections": [{"from_step": "src", "to_step": "flt"}],
    })
    assert r.status_code in (200, 201), r.text
    wid = r.json().get("id") or r.json()["workflow"]["id"]
    app_state["store"].update_status(wid, PipelineStatus.PUBLISHED)
    return wid


def _runs(workflow_id, trigger):
    from revoluzen_flow.main import app_state

    return [r for r in app_state["execution_store"].list_by_workflow(workflow_id) if r.get("triggered_by") == trigger]


def _wait(workflow_id, trigger, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = [r for r in _runs(workflow_id, trigger) if r.get("status") in ("success", "error")]
        if done:
            return done
        time.sleep(0.2)
    raise AssertionError(f"no {trigger} run of {workflow_id} within {timeout}s")


class TestEndToEnd:
    def test_saving_a_loop_is_refused_naming_the_pipelines(self, authed_client):
        a, b = _make(authed_client, "Loop A"), _make(authed_client, "Loop B")
        ok = authed_client.post("/api/schedules", json={
            "workflow_id": b, "schedule_type": "after_pipeline", "after_workflow_id": a,
        })
        assert ok.status_code == 200, ok.text
        loop = authed_client.post("/api/schedules", json={
            "workflow_id": a, "schedule_type": "after_pipeline", "after_workflow_id": b,
        })
        assert loop.status_code == 409, loop.text
        detail = loop.json()["detail"]
        assert detail["code"] == "trigger_loop"
        assert '"Loop A" → "Loop B" → "Loop A"' in detail["message"]
        itself = authed_client.post("/api/schedules", json={
            "workflow_id": a, "schedule_type": "after_pipeline", "after_workflow_id": a,
        })
        assert itself.status_code == 409

    def test_an_unknown_pipeline_to_wait_for_is_404(self, authed_client):
        a = _make(authed_client, "Waits for nothing")
        r = authed_client.post("/api/schedules", json={
            "workflow_id": a, "schedule_type": "after_pipeline", "after_workflow_id": "nope",
        })
        assert r.status_code == 404

    def test_b_runs_after_a_finishes_when_another_application_starts_a(self, authed_client, client):
        from revoluzen_flow.main import app_state

        a = _make(authed_client, "Extract orders")
        b = _make(authed_client, "Load orders", condition="amount >= 200")
        c = _make(authed_client, "Report failure")
        trigger = authed_client.post("/api/schedules", json={
            "workflow_id": b, "schedule_type": "after_pipeline", "after_workflow_id": a, "after_outcome": "success",
        }).json()
        authed_client.post("/api/schedules", json={
            "workflow_id": c, "schedule_type": "after_pipeline", "after_workflow_id": a, "after_outcome": "failure",
        })
        key = authed_client.post("/api/gateway/keys", json={"name": "chain-test"}).json()["api_key"]
        endpoint = authed_client.post("/api/gateway/endpoints", json={
            "workflow_id": a, "path": "chain/extract", "name": "extract",
        }).json()

        started = client.post(f"/api/published/{endpoint['path']}", headers={"X-API-Key": key})
        assert started.status_code == 200, started.text
        a_run = started.json()["execution_id"]

        app_state["scheduler"]._dispatch_after_pipeline()
        done = _wait(b, "after_pipeline")
        run = app_state["execution_store"].get(done[0]["id"])
        assert run.status == "success", run.error_message
        assert run.metadata["after_pipeline"]["execution_id"] == a_run
        assert run.metadata["after_pipeline"]["workflow_id"] == a
        assert next(s for s in run.step_logs if s.step_id == "flt").rows_processed == 3
        assert _runs(c, "after_pipeline") == [], "only the success trigger fires on a success"

        schedule = authed_client.get(f"/api/schedules/{trigger['id']}").json()
        assert schedule["status_message"].startswith('Started because "Extract orders" succeeded')

        not_now = authed_client.post(f"/api/schedules/{trigger['id']}/run-now")
        assert not_now.status_code == 400

    def test_a_manual_run_does_not_start_the_next(self, authed_client):
        from revoluzen_flow.main import app_state
        from revoluzen_flow.triggers.store import trigger_store

        a, b = _make(authed_client, "Hand run A"), _make(authed_client, "Hand run B")
        authed_client.post("/api/schedules", json={
            "workflow_id": b, "schedule_type": "after_pipeline", "after_workflow_id": a,
        })
        r = authed_client.post(f"/api/execute/workflow/{a}", params={"full_run": "true"})
        assert r.status_code == 200, r.text
        assert [e for e in trigger_store(app_state).pending_events() if e.upstream_id == a] == []
