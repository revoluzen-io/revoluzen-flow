"""The Execution Pool page must not invent capacity it does not have.

API runs never go through `WorkerPool.submit()` — they are gated by the
semaphore in `api/execution.py` and executed with anyio. The pool only
tracks them so the page can show what is running. Two things were wrong:

  * when every slot was busy the run was not recorded at all, so the page
    under-reported concurrency. Guaranteed whenever
    MAX_CONCURRENT_RUNS is 0 (the DEV default), because then the
    semaphore is a no-op and nothing caps runs at the slot count;
  * `utilization_pct` divided by a slot count derived from
    `multiprocessing.cpu_count()`, which is not a limit — a made-up
    denominator presented as capacity.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from revoluzen_flow.engine.worker_pool import QueuedJob, WorkerPool

pytestmark = pytest.mark.unit


def job(n: int, env: str = "dev") -> QueuedJob:
    return QueuedJob(
        id=f"job-{n}",
        workflow_id=f"wf-{n}",
        workflow_name=f"Pipeline {n}",
        environment=env,
        priority=3,
        queued_at=datetime.now(timezone.utc),
    )


# ── Capped vs uncapped ───────────────────────────────────────────────

def test_explicit_max_workers_is_a_real_cap():
    pool = WorkerPool(max_workers=4)
    s = pool.get_status()["pool"]
    assert s["capped"] is True
    assert s["concurrency_limit"] == 4
    assert s["utilization_pct"] == 0
    assert s["idle_workers"] == 4


def test_uncapped_pool_reports_no_limit(monkeypatch):
    """MAX_CONCURRENT_RUNS = 0 → the slot count is a guess, not a cap."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "MAX_CONCURRENT_RUNS", 0)

    pool = WorkerPool()  # falls back to max(4, cpu_count)
    s = pool.get_status()["pool"]
    assert s["capped"] is False
    assert s["concurrency_limit"] is None
    assert s["utilization_pct"] is None, "no cap means no percentage to report"
    assert s["idle_workers"] is None


def test_configured_limit_is_treated_as_capped(monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "MAX_CONCURRENT_RUNS", 4)

    pool = WorkerPool()
    s = pool.get_status()["pool"]
    assert s["capped"] is True
    assert s["concurrency_limit"] == 4


# ── Tracking slots grow rather than dropping runs ────────────────────

def test_every_claimed_run_is_counted_even_past_the_slot_count():
    """THE bug: runs beyond the slot count used to vanish from the page."""
    pool = WorkerPool(max_workers=2)
    workers = [pool.claim_tracking_slot(job(i)) for i in range(5)]

    s = pool.get_status()["pool"]
    assert s["busy_workers"] == 5, "all five runs must be visible"
    assert len({w.id for w in workers}) == 5, "each run needs its own slot"
    assert s["active_jobs"] == 5


def test_growth_shows_up_in_the_environment_split():
    pool = WorkerPool(max_workers=1)
    pool.claim_tracking_slot(job(1, "prod"))
    pool.claim_tracking_slot(job(2, "prod"))
    pool.claim_tracking_slot(job(3, "dev"))

    envs = pool.get_status()["environments"]
    assert envs["active_prod"] == 2
    assert envs["active_dev"] == 1


def test_idle_slots_are_reused_before_growing():
    pool = WorkerPool(max_workers=3)
    w1 = pool.claim_tracking_slot(job(1))
    pool.release_tracking_slot(job(1), w1, "success", 10.0)
    w2 = pool.claim_tracking_slot(job(2))
    assert w2.id == w1.id, "a free slot should be reused, not appended"
    assert len(pool._workers) == 3


def test_grown_slots_are_pruned_back_after_the_burst():
    """Otherwise one burst leaves phantom idle slots forever, overstating
    capacity as badly as the old code understated load."""
    pool = WorkerPool(max_workers=2)
    pairs = [(job(i), pool.claim_tracking_slot(job(i))) for i in range(6)]
    assert len(pool._workers) == 6

    for j, w in pairs:
        pool.release_tracking_slot(j, w, "success", 5.0)

    assert len(pool._workers) == 2, f"slots not pruned: {len(pool._workers)}"
    assert pool.get_status()["pool"]["busy_workers"] == 0


# ── Release must not free somebody else's slot ───────────────────────

def test_release_without_a_worker_does_not_free_another_job():
    """The old fallback released `_workers[0]`, marking a DIFFERENT
    running job idle while it was still working."""
    pool = WorkerPool(max_workers=2)
    running = job(1)
    w = pool.claim_tracking_slot(running)
    assert w.status == "busy"

    orphan = job(99)
    pool.release_tracking_slot(orphan, None, "error", 1.0)

    assert w.status == "busy", "an unrelated job's slot was freed"
    assert pool.get_status()["pool"]["busy_workers"] == 1


def test_release_clears_its_own_slot():
    pool = WorkerPool(max_workers=2)
    j = job(1)
    w = pool.claim_tracking_slot(j)
    pool.release_tracking_slot(j, w, "success", 12.0)
    assert w.status == "idle"
    assert pool.get_status()["pool"]["busy_workers"] == 0


# ── Capped pools still report a real percentage ──────────────────────

def test_utilization_is_real_when_capped():
    pool = WorkerPool(max_workers=4)
    pool.claim_tracking_slot(job(1))
    pool.claim_tracking_slot(job(2))
    s = pool.get_status()["pool"]
    assert s["utilization_pct"] == 50.0
    assert s["idle_workers"] == 2


def test_idle_never_goes_negative_when_growth_exceeds_the_cap():
    pool = WorkerPool(max_workers=2)
    for i in range(5):
        pool.claim_tracking_slot(job(i))
    s = pool.get_status()["pool"]
    assert s["busy_workers"] == 5
    assert s["idle_workers"] == 0, "must clamp, not report -3 idle workers"
