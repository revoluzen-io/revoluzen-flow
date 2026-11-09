"""The Dashboard chart counts every run in its window, however busy the workspace.

It used to bucket the newest rows of the run list, capped at 200, so at 25 runs an
hour everything older than eight hours came out empty beside a headline of 600 runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from revoluzen_flow.monitoring.store import ExecutionRecord, ExecutionStore
from revoluzen_flow.storage.database import Database


def make_store(tmp_path) -> ExecutionStore:
    return ExecutionStore(db=Database(str(tmp_path / "runs.db")))


def add(store, minutes_ago, status="success", workspace="default", project="default"):
    store.record(ExecutionRecord(
        workflow_id="w1", workspace_id=workspace, project_id=project, status=status,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    ))


def total(rows, status=None):
    return sum(r["count"] for r in rows if status is None or r["status"] == status)


def test_counts_more_runs_than_the_run_list_returns(tmp_path):
    store = make_store(tmp_path)
    for i in range(350):
        add(store, i * 3)  # about 17 hours, and more rows than the list's limit of 200
    assert len(store.list_all(200, workspace_id="default")) == 200
    assert total(store.run_counts(24, workspace_id="default")) == 350


def test_leaves_out_runs_older_than_the_window(tmp_path):
    store = make_store(tmp_path)
    add(store, 30)
    add(store, 25 * 60)
    assert total(store.run_counts(24, workspace_id="default")) == 1
    assert total(store.run_counts(48, workspace_id="default")) == 2


def test_keeps_statuses_apart(tmp_path):
    store = make_store(tmp_path)
    add(store, 5)
    add(store, 5)
    add(store, 5, status="error")
    rows = store.run_counts(24, workspace_id="default")
    assert total(rows, "success") == 2
    assert total(rows, "error") == 1


def test_stays_inside_the_workspace_and_project(tmp_path):
    store = make_store(tmp_path)
    add(store, 5, workspace="other")
    add(store, 5, project="sales")
    add(store, 5, project="finance")
    assert total(store.run_counts(24, workspace_id="default")) == 2
    assert total(store.run_counts(24, workspace_id="default", project_id="sales")) == 1


def test_a_slot_is_a_time_a_browser_can_read(tmp_path):
    store = make_store(tmp_path)
    add(store, 5)
    slot = store.run_counts(24, workspace_id="default")[0]["at"]
    parsed = datetime.fromisoformat(slot.replace("Z", "+00:00"))
    assert parsed.minute % 10 == 0 and parsed.second == 0
    assert parsed.tzinfo is not None
