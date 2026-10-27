"""Every run path shares one gate.

Before this, `MAX_CONCURRENT_RUNS=4` capped API runs at four while the
scheduler (no gate at all) and backfills (their own ThreadPoolExecutor)
ran unbounded beside them, none of them visible on the Execution Pool
page. These tests drive `run_through_pool` the way each caller does and
assert the cap holds ACROSS paths, not within one.
"""

from __future__ import annotations

import threading
import time

import pytest

from revoluzen_flow.engine.admission import run_through_pool
from revoluzen_flow.engine.worker_pool import WorkerPool

pytestmark = pytest.mark.unit


def call(pool, n: int, fn, *, triggered_by="manual", env="dev"):
    return run_through_pool(
        {"worker_pool": pool},
        fn=fn,
        run_id=f"run-{n}",
        workflow_id=f"wf-{n}",
        workflow_name=f"Pipeline {n}",
        environment=env,
        triggered_by=triggered_by,
    )


# ── Degrading ────────────────────────────────────────────────────────

def test_runs_without_a_pool():
    """CLI, tests and embedded use have no pool — a missing pool must
    never stop a pipeline executing."""
    assert run_through_pool(
        {}, fn=lambda: "ran", run_id="r", workflow_id="w", workflow_name="W"
    ) == "ran"
    assert run_through_pool(
        None, fn=lambda: "ran", run_id="r", workflow_id="w", workflow_name="W"
    ) == "ran"


def test_result_and_exceptions_pass_through():
    pool = WorkerPool(max_workers=2)
    assert call(pool, 1, lambda: 7) == 7
    with pytest.raises(ValueError):
        call(pool, 2, lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert pool.get_status()["pool"]["busy_workers"] == 0


# ── The cap is shared across paths ───────────────────────────────────

def test_one_cap_covers_api_schedule_and_backfill_together():
    """THE point of the change. A cap of 2 means two runs on the host,
    not two per path."""
    pool = WorkerPool(max_workers=2)
    lock = threading.Lock()
    live = {"n": 0}
    peak = {"n": 0}

    def work():
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        time.sleep(0.03)
        with lock:
            live["n"] -= 1

    paths = ["manual", "schedule", "backfill", "replay"] * 3
    threads = [
        threading.Thread(target=lambda i=i, p=p: call(pool, i, work, triggered_by=p))
        for i, p in enumerate(paths)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert peak["n"] <= 2, (
        f"{peak['n']} ran at once with a cap of 2 — the paths are not sharing a gate"
    )
    assert pool.get_status()["pool"]["busy_workers"] == 0


def test_scheduled_and_backfill_runs_are_visible_on_the_pool():
    """They used to be invisible — neither registered with the pool."""
    pool = WorkerPool(max_workers=4)
    seen: list[dict] = []

    def work():
        seen.append(dict(pool.get_status()["pool"]))

    call(pool, 1, work, triggered_by="schedule", env="prod")
    call(pool, 2, work, triggered_by="backfill")

    assert all(s["busy_workers"] == 1 for s in seen), "run was not registered"
    hist = pool.get_status()["throughput"]["environment_distribution"]
    assert hist.get("prod", 0) == 1
    assert pool.get_status()["counters"]["total_completed"] == 2


def test_every_path_lands_in_run_history_with_its_trigger():
    pool = WorkerPool(max_workers=2)
    for i, p in enumerate(["manual", "schedule", "backfill"]):
        call(pool, i, lambda: None, triggered_by=p)
    triggers = {c.triggered_by for c in pool._history}
    assert triggers == {"manual", "schedule", "backfill"}


# ── Nested runs still do not deadlock through this entry point ───────

def test_sub_pipeline_through_the_same_entry_point_does_not_deadlock():
    """An exec_pipeline node runs a child pipeline from inside a run. If
    that re-entered admission it would wait for a slot the parent holds."""
    pool = WorkerPool(max_workers=1)

    def parent():
        return call(pool, 2, lambda: "child")

    done: list = []
    t = threading.Thread(target=lambda: done.append(call(pool, 1, parent)))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "DEADLOCK: nested run waited on the parent's slot"
    assert done == ["child"]


# ── Uncapped is still uncapped ───────────────────────────────────────

def test_uncapped_does_not_serialise_the_paths(monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "MAX_CONCURRENT_RUNS", 0)
    pool = WorkerPool()
    n = len(pool._workers) + 3

    started = threading.Semaphore(0)
    hold = threading.Event()

    def work():
        started.release()
        hold.wait(timeout=5)

    threads = [
        threading.Thread(target=lambda i=i: call(pool, i, work))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for i in range(n):
        assert started.acquire(timeout=3), f"run {i} was blocked on an uncapped pool"
    hold.set()
    for t in threads:
        t.join(timeout=5)
