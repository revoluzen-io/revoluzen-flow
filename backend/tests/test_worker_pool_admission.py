"""`run_admitted` — the single admission point.

Every run path used to invent its own control: the API an
asyncio.Semaphore, backfills a private ThreadPoolExecutor, the scheduler
nothing at all. So MAX_CONCURRENT_RUNS=4 capped API runs at 4 while
schedules and backfills ran unbounded beside them, invisible to the Pool
page. `run_admitted` is the one gate they all pass through.

The two tests that matter most are the re-entrancy one (a sub-pipeline
must not take a second slot — with a cap of 1 that deadlocks) and the
uncapped one (0 must still mean 0).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from revoluzen_flow.engine.worker_pool import QueuedJob, WorkerPool

pytestmark = pytest.mark.unit


def job(n: int, env: str = "dev") -> QueuedJob:
    return QueuedJob(
        id=f"job-{n}", workflow_id=f"wf-{n}", workflow_name=f"Pipeline {n}",
        environment=env, priority=3, queued_at=datetime.now(timezone.utc),
    )


# ── It runs the work and returns the result ──────────────────────────

def test_returns_the_functions_result():
    pool = WorkerPool(max_workers=2)
    assert pool.run_admitted(job(1), lambda x: x * 2, {"x": 21}) == 42


def test_runs_on_the_calling_thread():
    """The caller's thread does the work — no hand-off, no extra thread."""
    pool = WorkerPool(max_workers=2)
    here = threading.get_ident()
    assert pool.run_admitted(job(1), lambda: threading.get_ident()) == here


def test_exception_propagates_and_still_frees_the_slot():
    pool = WorkerPool(max_workers=1)

    def boom():
        raise ValueError("pipeline failed")

    with pytest.raises(ValueError, match="pipeline failed"):
        pool.run_admitted(job(1), boom)

    assert pool.get_status()["pool"]["busy_workers"] == 0, "slot leaked on error"
    # And a later run can still be admitted.
    assert pool.run_admitted(job(2), lambda: "ok") == "ok"


def test_failure_is_recorded_as_an_error_in_history():
    pool = WorkerPool(max_workers=1)
    with pytest.raises(RuntimeError):
        pool.run_admitted(job(1), lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert pool.get_status()["counters"]["total_errors"] == 1


def test_status_is_taken_from_the_result():
    pool = WorkerPool(max_workers=1)
    pool.run_admitted(job(1), lambda: {"status": "success"})
    assert pool.get_status()["counters"]["total_errors"] == 0


# ── Visibility while running ─────────────────────────────────────────

def test_the_run_is_visible_on_the_pool_while_it_runs():
    pool = WorkerPool(max_workers=2)
    seen: dict = {}

    def work():
        seen.update(pool.get_status()["pool"])
        return None

    pool.run_admitted(job(1, "prod"), work)
    assert seen["busy_workers"] == 1
    assert pool.get_status()["pool"]["busy_workers"] == 0


# ── Re-entrancy: sub-pipelines must not take a second slot ───────────

def test_nested_run_does_not_take_a_second_slot():
    """A cap of 1 plus a sub-pipeline is a guaranteed deadlock without this."""
    pool = WorkerPool(max_workers=1)

    def parent():
        inner = pool.run_admitted(job(2), lambda: "child-done")
        assert pool.get_status()["pool"]["busy_workers"] == 1, "child took a slot"
        return inner

    done = []
    t = threading.Thread(target=lambda: done.append(pool.run_admitted(job(1), parent)))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "DEADLOCK: nested run waited for a slot"
    assert done == ["child-done"]


def test_depth_is_restored_so_later_runs_are_admitted_normally():
    pool = WorkerPool(max_workers=1)
    pool.run_admitted(job(1), lambda: pool.run_admitted(job(2), lambda: None))
    # If depth leaked, this run would skip admission and never be counted.
    pool.run_admitted(job(3), lambda: None)
    assert pool.get_status()["counters"]["total_completed"] >= 1


def test_depth_is_restored_even_when_the_nested_run_raises():
    pool = WorkerPool(max_workers=1)

    def parent():
        try:
            pool.run_admitted(job(2), lambda: (_ for _ in ()).throw(ValueError()))
        except ValueError:
            pass
        return "recovered"

    assert pool.run_admitted(job(1), parent) == "recovered"
    assert pool.get_status()["pool"]["busy_workers"] == 0


# ── Capped: admission actually blocks ────────────────────────────────

def test_capped_pool_blocks_the_second_run_until_the_first_finishes():
    pool = WorkerPool(max_workers=1)
    started_second = threading.Event()
    release_first = threading.Event()
    order: list[str] = []

    def first():
        order.append("first-start")
        release_first.wait(timeout=5)
        order.append("first-end")

    def second():
        order.append("second-start")

    t1 = threading.Thread(target=lambda: pool.run_admitted(job(1), first))
    t1.start()
    while pool.get_status()["pool"]["busy_workers"] == 0:
        time.sleep(0.01)

    t2 = threading.Thread(
        target=lambda: (pool.run_admitted(job(2), second), started_second.set())
    )
    t2.start()

    # The second run must NOT have started while the only slot is held.
    assert not started_second.wait(timeout=0.4), "cap was not enforced"
    assert order == ["first-start"]

    release_first.set()
    t1.join(timeout=5)
    assert started_second.wait(timeout=5), "second run never admitted"
    t2.join(timeout=5)
    assert order == ["first-start", "first-end", "second-start"]


def test_capped_pool_never_exceeds_its_cap_under_load():
    pool = WorkerPool(max_workers=3)
    peak = {"n": 0}
    lock = threading.Lock()
    live = {"n": 0}

    def work():
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        time.sleep(0.02)
        with lock:
            live["n"] -= 1

    threads = [
        threading.Thread(target=lambda i=i: pool.run_admitted(job(i), work))
        for i in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert peak["n"] <= 3, f"ran {peak['n']} concurrently with a cap of 3"
    assert pool.get_status()["pool"]["busy_workers"] == 0


# ── Uncapped: 0 still means 0 ────────────────────────────────────────

def test_uncapped_pool_never_blocks(monkeypatch):
    """MAX_CONCURRENT_RUNS=0 is documented as no limit — admission must
    not invent one from the CPU-count slot guess."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "MAX_CONCURRENT_RUNS", 0)
    pool = WorkerPool()
    base = len(pool._workers)

    hold = threading.Event()
    running = threading.Semaphore(0)

    def work():
        running.release()
        hold.wait(timeout=5)

    n = base + 4
    threads = [
        threading.Thread(target=lambda i=i: pool.run_admitted(job(i), work))
        for i in range(n)
    ]
    for t in threads:
        t.start()

    # Every one must start without waiting for a slot.
    for i in range(n):
        assert running.acquire(timeout=3), f"only {i} of {n} runs were admitted"

    assert pool.get_status()["pool"]["busy_workers"] == n
    hold.set()
    for t in threads:
        t.join(timeout=5)
    assert pool.get_status()["pool"]["busy_workers"] == 0
