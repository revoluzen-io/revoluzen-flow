"""Runs blocked waiting for capacity must be visible.

A run parked in admission holds no slot, so without this it looks like
nothing is happening: `busy_workers` counts only what is running and
`queue_depth` counts only `submit()` jobs, which no caller uses. An
operator whose schedules had quietly stopped firing would see "nothing
running, nothing queued" — the least useful possible answer.

It is worse for a scheduled run, which also holds its schedule's
overlap lock (`_running_jobs`, taken in `_execute_schedule` BEFORE the
thread starts) for the whole wait, suppressing that schedule's next
fire. Interval schedules self-heal on the next tick; cron ones lose the
window outright. That is exactly the state an operator needs to see.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from revoluzen_flow.engine.worker_pool import QueuedJob, WorkerPool

pytestmark = pytest.mark.unit


def job(n: int, **kw) -> QueuedJob:
    return QueuedJob(
        id=f"job-{n}", workflow_id=f"wf-{n}", workflow_name=f"Pipeline {n}",
        environment=kw.pop("environment", "dev"), priority=3,
        queued_at=datetime.now(timezone.utc), **kw,
    )


def test_nothing_waiting_when_capacity_is_free():
    pool = WorkerPool(max_workers=2)
    s = pool.get_status()["pool"]
    assert s["waiting_for_capacity"] == 0
    assert s["waiting"] == []


def test_fast_path_never_marks_a_run_as_waiting():
    """A run that gets a slot immediately must not flicker into the list."""
    pool = WorkerPool(max_workers=2)
    seen = []

    def work():
        seen.append(pool.get_status()["pool"]["waiting_for_capacity"])

    pool.run_admitted(job(1), work)
    assert seen == [0]


def test_a_blocked_run_shows_as_waiting():
    pool = WorkerPool(max_workers=1)
    hold = threading.Event()
    started = threading.Event()

    def occupy():
        started.set()
        hold.wait(timeout=5)

    t1 = threading.Thread(target=lambda: pool.run_admitted(job(1), occupy))
    t1.start()
    assert started.wait(timeout=3)

    t2 = threading.Thread(
        target=lambda: pool.run_admitted(
            job(2, triggered_by="schedule", schedule_id="sched-7"), lambda: None
        )
    )
    t2.start()

    # Give it a moment to park in admission.
    deadline = time.time() + 3
    while time.time() < deadline:
        s = pool.get_status()["pool"]
        if s["waiting_for_capacity"] == 1:
            break
        time.sleep(0.02)

    s = pool.get_status()["pool"]
    assert s["waiting_for_capacity"] == 1, "blocked run was invisible"
    entry = s["waiting"][0]
    assert entry["workflow_name"] == "Pipeline 2"
    assert entry["triggered_by"] == "schedule"
    assert entry["schedule_id"] == "sched-7", "operator needs to know WHICH schedule"
    assert entry["waiting_ms"] >= 0

    hold.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert pool.get_status()["pool"]["waiting_for_capacity"] == 0


def test_waiting_clears_once_admitted():
    pool = WorkerPool(max_workers=1)
    hold = threading.Event()
    inside = threading.Event()

    t1 = threading.Thread(
        target=lambda: pool.run_admitted(job(1), lambda: (inside.set(), hold.wait(5)))
    )
    t1.start()
    assert inside.wait(timeout=3)

    admitted = threading.Event()
    t2 = threading.Thread(
        target=lambda: pool.run_admitted(job(2), lambda: admitted.set())
    )
    t2.start()
    time.sleep(0.15)
    assert pool.get_status()["pool"]["waiting_for_capacity"] == 1

    hold.set()
    assert admitted.wait(timeout=5)
    t1.join(timeout=5)
    t2.join(timeout=5)
    s = pool.get_status()["pool"]
    assert s["waiting_for_capacity"] == 0
    assert s["waiting"] == []


def test_waiting_is_separate_from_queue_depth():
    """`queue_depth` counts submit() jobs; these are parked callers. A
    reader conflating them would double-count or miss them entirely."""
    pool = WorkerPool(max_workers=1)
    hold = threading.Event()
    inside = threading.Event()

    t1 = threading.Thread(
        target=lambda: pool.run_admitted(job(1), lambda: (inside.set(), hold.wait(5)))
    )
    t1.start()
    assert inside.wait(timeout=3)
    t2 = threading.Thread(target=lambda: pool.run_admitted(job(2), lambda: None))
    t2.start()
    time.sleep(0.15)

    s = pool.get_status()["pool"]
    assert s["waiting_for_capacity"] == 1
    assert s["queue_depth"] == 0

    hold.set()
    t1.join(timeout=5)
    t2.join(timeout=5)


def test_longest_waiter_is_listed_first():
    pool = WorkerPool(max_workers=1)
    hold = threading.Event()
    inside = threading.Event()
    t0 = threading.Thread(
        target=lambda: pool.run_admitted(job(0), lambda: (inside.set(), hold.wait(5)))
    )
    t0.start()
    assert inside.wait(timeout=3)

    waiters = []
    for n in (1, 2):
        t = threading.Thread(target=lambda n=n: pool.run_admitted(job(n), lambda: None))
        t.start()
        waiters.append(t)
        time.sleep(0.2)

    s = pool.get_status()["pool"]
    assert s["waiting_for_capacity"] == 2
    ms = [w["waiting_ms"] for w in s["waiting"]]
    assert ms == sorted(ms, reverse=True), "longest wait should be first"

    hold.set()
    t0.join(timeout=5)
    for t in waiters:
        t.join(timeout=5)


def test_uncapped_pool_never_reports_waiting(monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "MAX_CONCURRENT_RUNS", 0)
    pool = WorkerPool()
    n = len(pool._workers) + 3
    hold = threading.Event()
    started = threading.Semaphore(0)

    threads = [
        threading.Thread(
            target=lambda i=i: pool.run_admitted(
                job(i), lambda: (started.release(), hold.wait(5))
            )
        )
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for _ in range(n):
        assert started.acquire(timeout=3)

    assert pool.get_status()["pool"]["waiting_for_capacity"] == 0
    hold.set()
    for t in threads:
        t.join(timeout=5)
