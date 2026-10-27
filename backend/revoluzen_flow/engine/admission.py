"""The one door every pipeline run goes through.

Revoluzen Flow had four ways to start a pipeline and three unrelated
things limiting them:

  * an API run was gated by an ``asyncio.Semaphore`` in
    ``api/execution.py``, sized from ``MAX_CONCURRENT_RUNS``;
  * a scheduled run was gated by nothing at all — the scheduler called
    the executor straight from its schedule thread;
  * a backfill window was gated by the backfill's own private
    ``ThreadPoolExecutor``;
  * ``WorkerPool.submit()``, the path actually designed for this, had
    no callers.

So ``MAX_CONCURRENT_RUNS=4`` did not cap the host at four runs. It
capped *API* runs at four, while schedules and backfills ran unbounded
beside them — and because only the API path registered with the pool,
neither appeared on the Execution Pool page. Operators were reading a
number that described one third of the traffic.

``run_through_pool`` is the single admission point. It keeps each
caller's synchronous shape (the work runs on the calling thread) and
adds exactly two things: a slot, and visibility.

Degrading: with no pool in ``app_state`` the function still runs the
work. A missing pool is a mis-wired process, not a reason to refuse to
execute a pipeline — and tests, the CLI and embedded uses have no pool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("revoluzen_flow.admission")


def run_through_pool(
    app_state: Any,
    *,
    fn: Callable[[], Any],
    run_id: str,
    workflow_id: str,
    workflow_name: str,
    workspace_id: str = "default",
    project_id: str = "default",
    environment: str = "dev",
    triggered_by: str = "manual",
    schedule_id: str | None = None,
    priority: int = 3,
) -> Any:
    """Run `fn` holding a worker-pool slot. Returns/raises what `fn` does.

    Blocks before starting when the pool is capped and full. Nested
    calls (a pipeline running a sub-pipeline) reuse the parent's slot
    rather than taking a second one — `WorkerPool.run_admitted` owns
    that re-entrancy, and without it a cap of 1 would deadlock.
    """
    pool = None
    try:
        pool = (app_state or {}).get("worker_pool")
    except Exception:  # noqa: BLE001 — app_state may be a non-mapping in tests
        pool = None

    if pool is None:
        return fn()

    try:
        from revoluzen_flow.engine.worker_pool import QueuedJob
        job = QueuedJob(
            id=run_id,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            project_id=project_id,
            workspace_id=workspace_id,
            environment=environment if environment in ("dev", "prod") else "dev",
            priority=min(max(int(priority or 3), 1), 5),
            triggered_by=triggered_by,
            schedule_id=schedule_id,
            queued_at=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001
        # Could not describe the job — run it rather than drop it.
        logger.warning("admission bookkeeping failed; running ungated", exc_info=True)
        return fn()

    return pool.run_admitted(job, lambda: fn())


__all__ = ["run_through_pool"]
