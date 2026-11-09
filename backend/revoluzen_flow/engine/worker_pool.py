"""
Worker Pool Manager — priority-aware concurrent pipeline execution.

Replaces the simple asyncio Semaphore with a priority-aware worker pool
that tracks every running/queued pipeline and exposes real-time metrics
for the Execution Pool UI (Spark-style admin page).

Architecture:
  - N workers (configurable, default = CPU count)
  - Each pipeline runs in its own thread (DuckDB gets its own connection)
  - Priority queue: P1 (critical) → P5 (background)
  - Preemption: P1 can pause P5 if all workers busy (future)
  - Live metrics: worker utilization, queue depth, throughput
  - Execution mode: "thread" (default, GIL-friendly for DuckDB) or "process"
    (full isolation — one pipeline's OOM doesn't crash others)
  - Resource governor: per-job memory limits via DuckDB memory_limit
  - Concurrency caps: max N jobs per workflow, dedup running workflows
  - Persistent queue: SQLite-backed so queued jobs survive restarts

Worker isolation: each pipeline gets its own DuckDB connection with
independent memory budget. One pipeline's OOM spill doesn't affect others.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from revoluzen_flow.engine.queue import JobQueue

logger = logging.getLogger("revoluzen_flow.worker_pool")


def _status_of(result: Any) -> str:
    """Derive a job status from whatever the run returned.

    Mirrors `_run_job`'s logic so a run admitted through
    `run_admitted` lands in history with the same status it would have
    had through `submit()`.
    """
    if result is None:
        return "success"
    status = getattr(result, "status", None)
    if status is None and isinstance(result, dict):
        status = result.get("status")
    return str(status) if status else "success"


# ── Priority levels ────────────────────────────────────────────────────

class Priority(IntEnum):
    """Pipeline execution priority. Lower value = higher priority."""
    P1_CRITICAL = 1      # SLA-bound, revenue-impacting
    P2_HIGH = 2           # Business reporting, daily aggregates
    P3_NORMAL = 3         # Default for all pipelines
    P4_LOW = 4            # Backfills, exploratory, one-offs
    P5_BACKGROUND = 5     # Housekeeping, archival


PRIORITY_LABELS = {
    1: "P1 — Critical",
    2: "P2 — High",
    3: "P3 — Normal",
    4: "P4 — Low",
    5: "P5 — Background",
}


# ── Data classes ───────────────────────────────────────────────────────

@dataclass
class WorkerSlot:
    """Represents one worker thread in the pool."""
    id: int
    status: str = "idle"           # idle | busy
    current_job_id: str | None = None
    current_workflow_id: str | None = None
    current_workflow_name: str | None = None
    current_priority: int = 3
    current_environment: str = "dev"  # dev | prod
    started_at: datetime | None = None
    jobs_completed: int = 0
    total_busy_ms: float = 0


@dataclass
class QueuedJob:
    """A pipeline waiting in the priority queue."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str = ""
    workflow_name: str = ""
    project_id: str = "default"
    workspace_id: str = "default"
    environment: str = "dev"       # dev | prod
    priority: int = 3
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_by: str = "manual"   # manual | schedule | event
    schedule_id: str | None = None
    # Step 6: optional wall-clock timeout. None = unlimited. The pool's
    # watchdog checks started_at + timeout_s against now(); when
    # exceeded, _timeout_flagged is set and the job's status is
    # recorded as "timed_out" on completion. Enforcement is cooperative
    # in PR5 — pipeline code can call is_timed_out() to self-abort.
    # Hard-kill of running threads waits until pipelines move to
    # subprocess execution (post-PR5 migration work).
    timeout_s: int | None = None
    started_at: datetime | None = None
    _timeout_flagged: bool = False
    # Long-running notification one-shot. Watchdog flips this to True
    # after firing the first alert so we don't spam every check tick.
    _long_running_notified: bool = False
    # Internal — set when dispatched to a worker
    _future: Future | None = field(default=None, repr=False)
    _fn: Callable | None = field(default=None, repr=False)
    _kwargs: dict = field(default_factory=dict, repr=False)

    def is_timed_out(self) -> bool:
        """Cooperative deadline check. True once the watchdog has
        flagged this job. Pipeline code may poll this to abort early."""
        return self._timeout_flagged

    def deadline(self) -> datetime | None:
        """When this job must finish by, or None for unlimited."""
        if self.timeout_s is None or self.started_at is None:
            return None
        return self.started_at + timedelta(seconds=self.timeout_s)


@dataclass
class CompletedJob:
    """Record of a finished pipeline execution (kept in rolling buffer)."""
    id: str
    workflow_id: str
    workflow_name: str
    priority: int
    environment: str      # dev | prod
    status: str           # success | error | cancelled
    queued_at: datetime
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    wait_ms: float        # time spent in queue
    worker_id: int
    triggered_by: str = "manual"
    error: str | None = None


# ── Persistent Queue (SQLite-backed) ──────────────────────────────────

class PersistentQueue:
    """SQLite-backed job queue that survives process restarts.

    On startup, any queued jobs from the previous session are reloaded.
    Jobs are written when queued and deleted when dispatched to a worker.
    """

    def __init__(self, db_path: str = ""):
        if not db_path:
            data_dir = os.environ.get("REVOLUZEN_FLOW_DATA_DIR", ".")
            db_path = os.path.join(data_dir, ".revoluzen_flow_queue.db")
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_queue (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                workflow_name TEXT DEFAULT '',
                project_id TEXT DEFAULT 'default',
                workspace_id TEXT DEFAULT 'default',
                environment TEXT DEFAULT 'dev',
                priority INTEGER DEFAULT 3,
                triggered_by TEXT DEFAULT 'manual',
                schedule_id TEXT,
                queued_at TEXT NOT NULL,
                kwargs_json TEXT DEFAULT '{}'
            )
        """)
        conn.commit()
        conn.close()

    def enqueue(self, job: "QueuedJob", kwargs: dict) -> None:
        """Persist a queued job."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO job_queue "
                "(id, workflow_id, workflow_name, project_id, workspace_id, "
                "environment, priority, triggered_by, schedule_id, queued_at, kwargs_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.workflow_id, job.workflow_name, job.project_id,
                 job.workspace_id, job.environment, job.priority,
                 job.triggered_by, job.schedule_id,
                 job.queued_at.isoformat(),
                 json.dumps(kwargs, default=str)),
            )
            conn.commit()
        finally:
            conn.close()

    def dequeue(self, job_id: str) -> None:
        """Remove a job from the persistent queue (dispatched or cancelled)."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM job_queue WHERE id = ?", (job_id,))
            conn.commit()
        finally:
            conn.close()

    def load_pending(self) -> list[dict]:
        """Load all pending jobs from a previous session."""
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, workflow_id, workflow_name, project_id, workspace_id, "
                "environment, priority, triggered_by, schedule_id, queued_at "
                "FROM job_queue ORDER BY priority, queued_at"
            ).fetchall()
            return [
                {
                    "id": r[0], "workflow_id": r[1], "workflow_name": r[2],
                    "project_id": r[3], "workspace_id": r[4],
                    "environment": r[5], "priority": r[6],
                    "triggered_by": r[7], "schedule_id": r[8],
                    "queued_at": r[9],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def clear(self) -> None:
        """Clear all persisted jobs."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM job_queue")
            conn.commit()
        finally:
            conn.close()


# ── Resource Governor ─────────────────────────────────────────────────

class ResourceGovernor:
    """Per-job resource limits and concurrency control.

    Features:
      - Per-priority memory limits (DuckDB memory_limit per connection)
      - Max concurrent jobs per workflow (prevent one pipeline from hogging)
      - Dedup: reject if same workflow is already running (optional)
      - Backpressure: reject new jobs when queue depth > threshold
    """

    def __init__(
        self,
        memory_limits: dict[int, str] | None = None,
        max_per_workflow: int = 0,
        dedup_running: bool = False,
        max_queue_depth: int = 1000,
    ):
        # Default memory per priority: P1 gets more, P5 gets less
        self.memory_limits = memory_limits or {
            1: "2GB",    # P1 Critical
            2: "1GB",    # P2 High
            3: "512MB",  # P3 Normal
            4: "256MB",  # P4 Low
            5: "128MB",  # P5 Background
        }
        self.max_per_workflow = max_per_workflow  # 0 = unlimited
        self.dedup_running = dedup_running
        self.max_queue_depth = max_queue_depth

    def get_memory_limit(self, priority: int) -> str:
        """Get the DuckDB memory_limit for a given priority level."""
        return self.memory_limits.get(priority, "512MB")

    def check_admission(
        self,
        workflow_id: str,
        active_jobs: dict[str, "QueuedJob"],
        queue: list["QueuedJob"],
    ) -> tuple[bool, str]:
        """Check if a new job should be admitted.

        Returns (allowed, reason).
        """
        # Backpressure: queue depth limit
        if len(queue) >= self.max_queue_depth:
            return False, f"Queue full ({len(queue)} >= {self.max_queue_depth})"

        # Dedup: reject if same workflow already running
        if self.dedup_running:
            for job in active_jobs.values():
                if job.workflow_id == workflow_id:
                    return False, f"Workflow '{workflow_id}' already running (job {job.id})"

        # Max per workflow: count active + queued for this workflow
        if self.max_per_workflow > 0:
            count = sum(
                1 for j in list(active_jobs.values()) + queue
                if j.workflow_id == workflow_id
            )
            if count >= self.max_per_workflow:
                return False, (
                    f"Workflow '{workflow_id}' at concurrency limit "
                    f"({count} >= {self.max_per_workflow})"
                )

        return True, ""


# ── Worker Pool ────────────────────────────────────────────────────────

class WorkerPool:
    """Priority-aware worker pool for pipeline execution.

    Usage:
        pool = WorkerPool(max_workers=8)
        pool.start()

        # Submit a pipeline job
        job_id = pool.submit(
            workflow_id="abc",
            workflow_name="Daily ETL",
            priority=Priority.P2_HIGH,
            fn=executor.execute_workflow,
            kwargs={"wf": wf, "preview_limit": 50},
        )

        # Check status
        status = pool.get_status()

        pool.stop()
    """

    def __init__(
        self,
        max_workers: int = 0,
        governor: ResourceGovernor | None = None,
        persistent_queue: bool = False,
        job_queue: "JobQueue | None" = None,
    ):
        # Auto-detect worker count from CPU cores if not specified
        cpu_count = multiprocessing.cpu_count() or 4
        # Whether a real concurrency limit exists, as opposed to a slot
        # count we invented from the CPU count. The distinction was lost
        # here: both paths produced an int, so `max_workers` looked like a
        # cap even when nothing enforced one, and the Pool page divided by
        # it to show a utilization percentage against a denominator that
        # did not exist. API runs are gated by the semaphore in
        # api/execution.py, which is a NO-OP when MAX_CONCURRENT_RUNS is 0.
        capped = max_workers > 0
        if max_workers <= 0:
            from revoluzen_flow import runtime_config
            max_workers = runtime_config.MAX_CONCURRENT_RUNS
            capped = max_workers > 0
            if max_workers <= 0:
                max_workers = max(4, cpu_count)

        self._max_workers = max_workers
        self._capped = capped

        # Re-entrancy depth, per thread. A pipeline that runs a
        # sub-pipeline (exec_pipeline, or the executor's own dependency
        # resolution) must NOT take a second slot: with a cap of 1 that
        # deadlocks instantly, and at any cap it double-counts one run.
        self._admission = threading.local()
        # Runs blocked in admission: job_id -> (job, waiting_since).
        # Distinct from `_queue`, which holds jobs submitted through
        # submit() and not yet dispatched. These are live callers parked
        # on a thread, waiting for a slot.
        self._waiting: dict[str, tuple[QueuedJob, datetime]] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._slot_free = threading.Condition(self._lock)

        # Resource governor — controls admission, memory limits, concurrency
        self._governor = governor or ResourceGovernor()

        # Persistent queue — survives restarts
        self._persistent_queue: PersistentQueue | None = None
        if persistent_queue:
            self._persistent_queue = PersistentQueue()

        # Worker tracking
        self._workers: list[WorkerSlot] = [
            WorkerSlot(id=i) for i in range(max_workers)
        ]

        # Stage 5 Phase 1: priority queue is now pluggable. Default is
        # InProcQueue which preserves the old in-memory sorted-list
        # behaviour exactly. Phase 2 will swap in RedisQueue for the
        # revoluzen_flow-worker container path — same protocol, different
        # persistence. Tests can inject a mock queue.
        if job_queue is None:
            from revoluzen_flow.engine.queue import InProcQueue
            job_queue = InProcQueue()
        self._queue: "JobQueue" = job_queue

        # Active jobs — job_id → QueuedJob
        self._active: dict[str, QueuedJob] = {}

        # History — rolling buffer of last 500 completed jobs
        self._history: deque[CompletedJob] = deque(maxlen=500)

        # Counters
        self._total_submitted = 0
        self._total_completed = 0
        self._total_errors = 0
        self._total_rejected = 0
        self._started_at: datetime | None = None

        # Step 6: pipeline-timeout watchdog. Background thread that
        # polls _active every `_timeout_check_interval_s` and flags
        # jobs past their deadline. Lifecycle tied to start()/stop().
        # PR5 follow-up: if ExecutionManager is initialized, the
        # watchdog registers via spawn_thread so it's counted against
        # the thread cap. When not (tests that construct the pool
        # standalone), falls back to raw threading.Thread.
        self._timeout_check_interval_s: float = 5.0
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_handle: Any = None  # ExecutionManager TaskHandle
        self._watchdog_stop = threading.Event()

        # Long-running notifier (May 3 2026). Optional — set via
        # set_long_running_notifier() during lifespan startup. When
        # None, long-running detection is disabled.
        self._long_running_notifier: Any = None

    def start(self) -> None:
        """Start the thread pool."""
        if self._executor is not None:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="revoluzen_flow-worker",
        )
        self._started_at = datetime.now(timezone.utc)
        logger.info(
            "Worker pool started: %d workers, priority P1-P5",
            self._max_workers,
        )

        # Launch the timeout watchdog. Register with ExecutionManager
        # when available so the thread is counted against the thread
        # cap and visible to /api/admin/execution/stats. Falls back to
        # raw threading.Thread for test fixtures that construct the
        # pool standalone (without initializing the manager).
        self._watchdog_stop.clear()
        try:
            from revoluzen_flow.engine.execution_manager import ExecutionManager
            mgr = ExecutionManager.get()
        except RuntimeError:
            mgr = None

        if mgr is not None:
            self._watchdog_handle = mgr.spawn_thread(
                name="pool-timeout-watchdog",
                fn=self._timeout_watchdog_loop,
                owner="worker_pool",
                stop_event_kwarg="stop_event",
            )
        else:
            self._watchdog_thread = threading.Thread(
                target=self._timeout_watchdog_loop,
                name="revoluzen_flow-pool-timeout-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    def stop(self) -> None:
        """Gracefully shut down the pool."""
        # Signal the watchdog to exit before shutting down the
        # executor — a stale watchdog iteration against a torn-down
        # _active dict is harmless but noisy. Also set the legacy
        # fallback event so the raw-thread path exits when
        # ExecutionManager isn't mediating.
        self._watchdog_stop.set()

        if self._watchdog_handle is not None:
            try:
                from revoluzen_flow.engine.execution_manager import ExecutionManager
                ExecutionManager.get().cancel(self._watchdog_handle)
            except RuntimeError:
                # Manager gone already — shutdown-ordering edge case.
                # The spawn_thread wrapper is cooperative; when the
                # stop_event (the spawn_thread-internal one) is set
                # via cancel, the loop exits. If cancel couldn't run,
                # the watchdog thread just keeps going until the
                # daemon-thread interpreter-exit cleanup fires.
                pass
            self._watchdog_handle = None
        elif self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=self._timeout_check_interval_s + 1)
            self._watchdog_thread = None

        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor = None
        # Stage 5 Phase 1: release queue resources. InProcQueue is a
        # no-op; Phase 2's RedisQueue will close its connection pool.
        try:
            self._queue.close()
        except Exception as exc:
            logger.debug("Queue close failed (non-fatal): %s", exc)
        logger.info("Worker pool stopped")

    def _timeout_watchdog_loop(self, stop_event: threading.Event | None = None) -> None:
        """Check each active job's deadline; flag those past it.
        Also detects long-running pipelines (elapsed > admin-configured
        threshold) and fires a one-shot notification."""
        if stop_event is None:
            stop_event = self._watchdog_stop

        while not stop_event.is_set():
            now = datetime.now(timezone.utc)
            flagged: list[str] = []
            long_running: list[tuple[QueuedJob, int]] = []

            threshold_min = self._long_running_threshold_min()

            with self._lock:
                for job in list(self._active.values()):
                    if not job._timeout_flagged:
                        deadline = job.deadline()
                        if deadline is not None and now > deadline:
                            job._timeout_flagged = True
                            flagged.append(
                                f"{job.id} ({job.workflow_name}): "
                                f"ran {(now - job.started_at).total_seconds():.1f}s "
                                f"past deadline of {job.timeout_s}s"
                            )

                    # Long-running detection (May 3 2026)
                    if (
                        threshold_min > 0
                        and not getattr(job, "_long_running_notified", False)
                        and job.started_at is not None
                    ):
                        elapsed_min = int((now - job.started_at).total_seconds() / 60)
                        if elapsed_min >= threshold_min:
                            job._long_running_notified = True
                            long_running.append((job, elapsed_min))

            for msg in flagged:
                logger.warning("pipeline timeout flagged: %s", msg)

            for job, elapsed_min in long_running:
                self._fire_long_running(job, elapsed_min, threshold_min)

            if stop_event.wait(timeout=self._timeout_check_interval_s):
                return

    def _long_running_threshold_min(self) -> int:
        """Resolve threshold (minutes) from notifier config, env-var
        fallback, or 0 to disable."""
        notifier = self._long_running_notifier
        if notifier is None:
            return 0
        try:
            cfg = notifier._get_config()
            if not cfg.get("notify_on_long_running", True):
                return 0
            return int(cfg.get("long_running_threshold_min", 30))
        except Exception:
            try:
                return int(os.environ.get("REVOLUZEN_FLOW_LONG_RUNNING_THRESHOLD_MIN", "30"))
            except (TypeError, ValueError):
                return 30

    def _fire_long_running(self, job: QueuedJob, elapsed_min: int, threshold_min: int) -> None:
        """Best-effort notification dispatch. Never raises."""
        notifier = self._long_running_notifier
        if notifier is None:
            return
        try:
            notifier.on_long_running(
                workflow_id=job.workflow_id,
                workflow_name=job.workflow_name,
                execution_id=job.id,
                elapsed_minutes=elapsed_min,
                threshold_minutes=threshold_min,
                triggered_by_user_id=None,
            )
            logger.info(
                "long-running alert fired for %s (%dm, threshold %dm)",
                job.workflow_name, elapsed_min, threshold_min,
            )
        except Exception as exc:
            logger.warning("long-running notify failed (non-fatal): %s", exc)

    def set_long_running_notifier(self, notifier) -> None:
        """Wire an ApprovalNotifier (or compatible) to receive long-running
        alerts. Called from main.py during lifespan startup."""
        self._long_running_notifier = notifier

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def submit(
        self,
        workflow_id: str,
        workflow_name: str,
        project_id: str = "default",
        workspace_id: str = "default",
        environment: str = "dev",
        priority: int = 3,
        triggered_by: str = "manual",
        schedule_id: str | None = None,
        fn: Callable | None = None,
        kwargs: dict | None = None,
        timeout_s: int | None = None,
    ) -> QueuedJob:
        """Submit a pipeline for execution.

        Returns the QueuedJob immediately. The job runs when a worker is
        free, ordered by priority.

        timeout_s is an optional wall-clock deadline. When set, the
        pool's watchdog flags the job for cooperative abort once
        started_at + timeout_s has passed. Default None = unlimited.
        """
        job = QueuedJob(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            project_id=project_id,
            workspace_id=workspace_id,
            environment=environment,
            priority=min(max(priority, 1), 5),
            triggered_by=triggered_by,
            schedule_id=schedule_id,
            timeout_s=timeout_s,
            _fn=fn,
            _kwargs=kwargs or {},
        )

        with self._lock:
            # Admission control via resource governor. Pass a snapshot
            # of the queue since check_admission iterates it; the
            # snapshot protects against concurrent mutation.
            allowed, reason = self._governor.check_admission(
                workflow_id, self._active, self._queue.snapshot(),
            )
            if not allowed:
                self._total_rejected += 1
                logger.warning(
                    "Rejected job for %s: %s", workflow_name, reason,
                )
                raise ValueError(f"Job rejected: {reason}")

            self._total_submitted += 1

            # Try to dispatch immediately if a worker is free
            worker = self._find_free_worker()
            if worker is not None:
                self._dispatch(job, worker)
            else:
                # Queue by priority (lower number = higher priority).
                # Stage 5 Phase 1: delegate ordering to the JobQueue
                # implementation — InProcQueue sorts on enqueue, just
                # as the old inline code did.
                self._queue.enqueue(job)

                # Persist to SQLite for restart recovery
                if self._persistent_queue:
                    self._persistent_queue.enqueue(job, kwargs or {})

                logger.info(
                    "Queued job %s (%s) at P%d — all %d workers busy, queue depth: %d",
                    job.id, workflow_name, job.priority,
                    self._max_workers, self._queue.depth(),
                )

        return job

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job."""
        with self._lock:
            # Remove from queue — JobQueue.cancel returns True if the
            # job was queued and removed, False otherwise.
            if self._queue.cancel(job_id):
                logger.info("Cancelled queued job %s", job_id)
                return True

            # Cancel active job future
            if job_id in self._active:
                job = self._active[job_id]
                if job._future and not job._future.done():
                    job._future.cancel()
                    logger.info("Cancelled active job %s", job_id)
                    return True

        return False

    def _find_free_worker(self) -> WorkerSlot | None:
        """Find an idle worker slot (must hold self._lock)."""
        for w in self._workers:
            if w.status == "idle":
                return w
        return None

    # ── Display-only tracking for runs this pool does not dispatch ──────
    #
    # API runs do not go through submit()/the queue — they are gated by
    # the semaphore in api/execution.py and executed with anyio. The pool
    # still tracks them so the Execution Pool page can show what is
    # running. That tracking used to be done by the endpoint reaching
    # into `_workers` / `_active` directly, which had two bugs:
    #
    #   * when every slot was busy the run was simply not recorded, so
    #     the page under-reported real concurrency — guaranteed whenever
    #     MAX_CONCURRENT_RUNS is 0, because then nothing caps the number
    #     of runs at the slot count;
    #   * on completion it fell back to releasing `_workers[0]`, which
    #     belongs to a DIFFERENT job and was marked idle while still busy.
    #
    # These two methods own that lifecycle instead.

    def claim_tracking_slot(self, job: QueuedJob) -> WorkerSlot:
        """Mark a slot busy for an externally-executed run.

        Returns an idle slot when one exists, otherwise APPENDS a slot.
        Growing is the honest option: the run IS happening, so a report
        that cannot represent it is simply wrong. On an uncapped pool the
        slot list is a picture of concurrency, not a limit on it.
        """
        with self._lock:
            self._total_submitted += 1
            worker = self._find_free_worker()
            if worker is None:
                worker = WorkerSlot(id=len(self._workers))
                self._workers.append(worker)
            worker.status = "busy"
            worker.current_job_id = job.id
            worker.current_workflow_id = job.workflow_id
            worker.current_workflow_name = job.workflow_name
            worker.current_priority = job.priority
            worker.current_environment = job.environment
            worker.started_at = datetime.now(timezone.utc)
            self._active[job.id] = job
            return worker

    def release_tracking_slot(
        self,
        job: QueuedJob,
        worker: WorkerSlot | None,
        status: str,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        """Release a slot claimed by `claim_tracking_slot`.

        A missing worker means the claim never happened, so there is
        nothing to free — previously this released slot 0 and reported
        an unrelated running job as idle.
        """
        if worker is None:
            with self._lock:
                self._active.pop(job.id, None)
            return
        self._on_job_complete(job, worker, status, duration_ms, error)
        self._prune_grown_slots()

    # ── Single admission point ─────────────────────────────────────────
    #
    # `run_admitted` gates a run that the CALLER executes on its own
    # thread, rather than handing the work to the pool's executor. That
    # shape matters: every run path in Revoluzen Flow is already
    # synchronous and needs the result (the API awaits it through
    # anyio.to_thread, the scheduler runs it on its schedule thread, a
    # backfill runs each window on its own pool thread). Making them all
    # submit-and-wait would double the threads per run and leave the
    # result-plumbing untouched anyway.
    #
    # Before this existed, each path invented its own control: the API
    # had an asyncio.Semaphore, backfills a private ThreadPoolExecutor,
    # and the scheduler nothing at all — so `MAX_CONCURRENT_RUNS=4`
    # capped API runs at 4 while schedules and backfills ran unbounded
    # alongside, invisible to the Pool page.

    def run_admitted(
        self,
        job: QueuedJob,
        fn: Callable[..., Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Run `fn` on the calling thread, holding a pool slot.

        Blocks before starting when the pool is capped and full; returns
        or raises exactly what `fn` does. The slot is always released,
        and the run appears on the Execution Pool page for its duration.

        Nested calls on the same thread run inline without taking a
        second slot — see `_admission`.
        """
        depth = getattr(self._admission, "depth", 0)
        if depth > 0:
            # Already inside an admitted run: this is a sub-pipeline, and
            # it is covered by the parent's slot.
            return fn(**(kwargs or {}))

        try:
            worker = self._acquire_admission(job)
        except BaseException:
            # A cancelled or interrupted wait must not leave the run
            # showing as waiting forever.
            with self._lock:
                self._waiting.pop(job.id, None)
            raise
        self._admission.depth = depth + 1
        start = time.time()
        status = "error"
        error_msg: str | None = None
        try:
            result = fn(**(kwargs or {}))
            status = _status_of(result)
            return result
        except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised
            error_msg = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._admission.depth = depth
            try:
                self._on_job_complete(
                    job, worker, status, (time.time() - start) * 1000, error_msg,
                )
                self._prune_grown_slots()
            except Exception:  # noqa: BLE001 — never mask the run's own outcome
                logger.warning("pool accounting failed after run", exc_info=True)

    def _may_start(self, job: QueuedJob, waiting_since: datetime | None) -> bool:
        """Whether `job` may take a free slot (must hold self._lock).

        Free slots go to the highest priority waiting, then to whoever has
        waited longest. A condition variable wakes its waiters in no useful
        order, so without this a P5 archive job could beat a P1 that had been
        waiting for a minute.
        """
        if not self._capped:
            return True
        mine = (job.priority, waiting_since or datetime.now(timezone.utc))
        ahead = sum(
            1 for other, since in self._waiting.values()
            if other.id != job.id and (other.priority, since) < mine
        )
        idle = sum(1 for w in self._workers if w.status == "idle")
        return ahead < idle

    def _acquire_admission(self, job: QueuedJob) -> WorkerSlot:
        """Take a slot, waiting when capped and full. Returns the slot."""
        waiting_since: datetime | None = None
        with self._slot_free:
            self._total_submitted += 1
            while True:
                worker = self._find_free_worker()
                if worker is None and not self._capped:
                    # Uncapped means uncapped: `MAX_CONCURRENT_RUNS=0` is
                    # documented as "no limit", so admission must not
                    # invent one out of the CPU-count slot guess. Grow
                    # instead, which also keeps the run visible.
                    worker = WorkerSlot(id=len(self._workers))
                    self._workers.append(worker)
                if worker is not None and self._may_start(job, waiting_since):
                    worker.status = "busy"
                    worker.current_job_id = job.id
                    worker.current_workflow_id = job.workflow_id
                    worker.current_workflow_name = job.workflow_name
                    worker.current_priority = job.priority
                    worker.current_environment = job.environment
                    worker.started_at = datetime.now(timezone.utc)
                    job.started_at = worker.started_at
                    self._active[job.id] = job
                    self._waiting.pop(job.id, None)
                    # Whoever is behind this job may now be next for another free slot.
                    self._slot_free.notify_all()
                    return worker
                # Capped and full — wait for a slot to free.
                #
                # Recorded so the Execution Pool page can show it. A run
                # stuck here is invisible otherwise: it holds no slot, so
                # it looks idle, while a scheduled one is ALSO holding its
                # schedule's overlap lock and silently suppressing that
                # schedule's next fire. "Nothing is running and nothing is
                # queued" was the worst possible thing to show an operator
                # whose schedules had stopped firing.
                if waiting_since is None:
                    waiting_since = datetime.now(timezone.utc)
                    self._waiting[job.id] = (job, waiting_since)
                self._slot_free.wait(timeout=1.0)

    def _prune_grown_slots(self) -> None:
        """Drop trailing idle slots added by growth, back to the base size.

        Without this the list only ever grows: one burst of 30 concurrent
        runs would leave 30 slots showing "idle" forever, which overstates
        capacity exactly as badly as the old code understated load.
        """
        with self._lock:
            while (
                len(self._workers) > self._max_workers
                and self._workers[-1].status == "idle"
            ):
                self._workers.pop()

    def _dispatch(self, job: QueuedJob, worker: WorkerSlot) -> None:
        """Dispatch a job to a worker (must hold self._lock)."""
        worker.status = "busy"
        worker.current_job_id = job.id
        worker.current_workflow_id = job.workflow_id
        worker.current_workflow_name = job.workflow_name
        worker.current_priority = job.priority
        worker.current_environment = job.environment
        worker.started_at = datetime.now(timezone.utc)
        # Record on the job itself so the timeout watchdog can compute
        # deadline without cross-referencing the worker slot.
        job.started_at = worker.started_at

        self._active[job.id] = job

        # Remove from persistent queue (now dispatched)
        if self._persistent_queue:
            self._persistent_queue.dequeue(job.id)

        if self._executor and job._fn:
            future = self._executor.submit(
                self._run_job, job, worker,
            )
            job._future = future
        else:
            # No executor or no function — mark done immediately
            self._on_job_complete(job, worker, "error", 0, "No executor or function")

    def _run_job(self, job: QueuedJob, worker: WorkerSlot) -> Any:
        """Execute the job function and handle completion."""
        start = time.time()
        status = "error"
        error_msg = None
        result = None

        try:
            if job._fn:
                result = job._fn(**job._kwargs)
            # Determine status from result
            if result is not None:
                if hasattr(result, "status"):
                    status = result.status
                elif isinstance(result, dict) and "status" in result:
                    status = result["status"]
                else:
                    status = "success"
            else:
                status = "success"
        except Exception as e:
            status = "error"
            error_msg = str(e)
            logger.error("Job %s failed: %s", job.id, e)
        finally:
            duration_ms = (time.time() - start) * 1000
            self._on_job_complete(job, worker, status, duration_ms, error_msg)

        return result

    def _on_job_complete(
        self,
        job: QueuedJob,
        worker: WorkerSlot,
        status: str,
        duration_ms: float,
        error: str | None,
    ) -> None:
        """Handle job completion — free worker, dispatch next from queue."""
        now = datetime.now(timezone.utc)

        with self._lock:
            # Record completion
            started_at = worker.started_at or now
            wait_ms = (started_at - job.queued_at).total_seconds() * 1000

            # Step 6: if the watchdog flagged this job, record
            # "timed_out" in history regardless of how the underlying
            # callable actually returned. Helps audit visibility when
            # the pipeline happened to finish naturally just after
            # the deadline.
            if job._timeout_flagged and status in ("success", "error"):
                status = "timed_out"

            completed = CompletedJob(
                id=job.id,
                workflow_id=job.workflow_id,
                workflow_name=job.workflow_name,
                priority=job.priority,
                environment=job.environment,
                status=status,
                queued_at=job.queued_at,
                started_at=started_at,
                completed_at=now,
                duration_ms=round(duration_ms, 1),
                wait_ms=round(max(0, wait_ms), 1),
                worker_id=worker.id,
                triggered_by=job.triggered_by,
                error=error,
            )
            self._history.append(completed)
            self._total_completed += 1
            if status == "error":
                self._total_errors += 1

            # Update worker stats
            worker.jobs_completed += 1
            worker.total_busy_ms += duration_ms

            # Free the worker
            worker.status = "idle"
            worker.current_job_id = None
            worker.current_workflow_id = None
            worker.current_workflow_name = None
            worker.current_priority = 3
            worker.current_environment = "dev"
            worker.started_at = None

            # Remove from active
            self._active.pop(job.id, None)

            # Dispatch next queued job if any. dequeue() returns None
            # when the queue is empty; the old ``if self._queue`` truthy
            # check on a list behaves the same way via the explicit
            # None check below.
            next_job = self._queue.dequeue()
            if next_job is not None:
                self._dispatch(next_job, worker)

            # Wake admission waiters. Unconditional: when the slot was
            # immediately re-dispatched above, waiters re-check and go
            # back to sleep, which is cheaper than reasoning about it
            # here and cannot miss a wakeup.
            self._slot_free.notify_all()

    # ── Status / Metrics ───────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return complete pool status for the admin UI."""
        with self._lock:
            now = datetime.now(timezone.utc)
            workers = []
            busy_count = 0
            for w in self._workers:
                elapsed_ms = 0
                if w.status == "busy" and w.started_at:
                    elapsed_ms = round(
                        (datetime.now(timezone.utc) - w.started_at).total_seconds() * 1000
                    )
                    busy_count += 1
                workers.append({
                    "id": w.id,
                    "status": w.status,
                    "current_job_id": w.current_job_id,
                    "current_workflow_id": w.current_workflow_id,
                    "current_workflow_name": w.current_workflow_name,
                    "current_priority": w.current_priority,
                    "current_priority_label": PRIORITY_LABELS.get(w.current_priority, ""),
                    "current_environment": w.current_environment,
                    "elapsed_ms": elapsed_ms,
                    "jobs_completed": w.jobs_completed,
                    "total_busy_ms": round(w.total_busy_ms, 1),
                })

            # Snapshot the queue while holding the outer lock; the
            # admin page reads attributes off the result, doesn't
            # mutate. Keeps the old payload shape identical.
            queue = [
                {
                    "id": j.id,
                    "workflow_id": j.workflow_id,
                    "workflow_name": j.workflow_name,
                    "priority": j.priority,
                    "priority_label": PRIORITY_LABELS.get(j.priority, ""),
                    "environment": j.environment,
                    "queued_at": j.queued_at.isoformat(),
                    "wait_ms": round(
                        (datetime.now(timezone.utc) - j.queued_at).total_seconds() * 1000
                    ),
                    "triggered_by": j.triggered_by,
                }
                for j in self._queue.snapshot()
            ]

            active = [
                {
                    "id": j.id,
                    "workflow_id": j.workflow_id,
                    "workflow_name": j.workflow_name,
                    "priority": j.priority,
                    "priority_label": PRIORITY_LABELS.get(j.priority, ""),
                    "environment": j.environment,
                    "queued_at": j.queued_at.isoformat(),
                    "triggered_by": j.triggered_by,
                }
                for j in self._active.values()
            ]

            # Throughput metrics
            now = datetime.now(timezone.utc)
            recent_1h = [
                c for c in self._history
                if (now - c.completed_at).total_seconds() < 3600
            ]
            recent_5m = [
                c for c in self._history
                if (now - c.completed_at).total_seconds() < 300
            ]

            # Average wait time (queue time)
            avg_wait = 0
            if recent_1h:
                avg_wait = round(sum(c.wait_ms for c in recent_1h) / len(recent_1h), 1)

            # Average duration
            avg_duration = 0
            p95_duration = 0
            if recent_1h:
                durations = sorted(c.duration_ms for c in recent_1h)
                avg_duration = round(sum(durations) / len(durations), 1)
                # P95 — index = ceil(0.95 * n) - 1, clamped to [0, n-1].
                # On tiny samples (n<5) p95 ≈ max, which is the right answer:
                # one slow run is the operator's signal.
                idx = max(0, min(len(durations) - 1, int(0.95 * len(durations))))
                p95_duration = round(durations[idx], 1)

            # Priority distribution in history
            priority_dist = {}
            for c in self._history:
                key = f"P{c.priority}"
                priority_dist[key] = priority_dist.get(key, 0) + 1

            # Environment distribution — how many DEV vs PROD runs
            env_dist = {"dev": 0, "prod": 0}
            for c in self._history:
                env_dist[c.environment] = env_dist.get(c.environment, 0) + 1

            # Currently active per environment
            active_dev = sum(1 for w in self._workers if w.status == "busy" and w.current_environment == "dev")
            active_prod = sum(1 for w in self._workers if w.status == "busy" and w.current_environment == "prod")

            # Utilization needs a real denominator. Uncapped, there isn't
            # one: `_max_workers` is a CPU-count guess that nothing
            # enforces, so "3 / 8 busy, 37%" would be a number we made
            # up. Report None and let the UI say so.
            if self._capped and self._max_workers > 0:
                utilization = round(busy_count / self._max_workers * 100, 1)
                idle = max(0, self._max_workers - busy_count)
            else:
                utilization = None
                idle = None

            return {
                "pool": {
                    # The concurrency LIMIT, or None when runs are uncapped.
                    "capped": self._capped,
                    "concurrency_limit": self._max_workers if self._capped else None,
                    # Slot count — with growth this tracks live concurrency
                    # rather than capping it, so it is not a limit.
                    "max_workers": len(self._workers),
                    "busy_workers": busy_count,
                    "idle_workers": idle,
                    "utilization_pct": utilization,
                    "queue_depth": self._queue.depth(),
                    "active_jobs": len(self._active),
                    # Live callers parked in admission waiting for a slot.
                    # Separate from `queue_depth`, which counts jobs
                    # submitted through submit() and not yet dispatched.
                    "waiting_for_capacity": len(self._waiting),
                    "waiting": sorted(
                        (
                            {
                                "job_id": j.id,
                                "workflow_id": j.workflow_id,
                                "workflow_name": j.workflow_name,
                                "environment": j.environment,
                                "triggered_by": j.triggered_by,
                                "schedule_id": j.schedule_id,
                                "waiting_ms": round(
                                    (now - since).total_seconds() * 1000, 1
                                ),
                            }
                            for j, since in self._waiting.values()
                        ),
                        key=lambda w: -w["waiting_ms"],
                    ),
                    "started_at": self._started_at.isoformat() if self._started_at else None,
                },
                "counters": {
                    "total_submitted": self._total_submitted,
                    "total_completed": self._total_completed,
                    "total_errors": self._total_errors,
                    "total_rejected": self._total_rejected,
                    "success_rate": round(
                        (self._total_completed - self._total_errors)
                        / max(self._total_completed, 1) * 100,
                        1,
                    ),
                },
                "governor": {
                    "memory_limits": self._governor.memory_limits,
                    "max_per_workflow": self._governor.max_per_workflow,
                    "dedup_running": self._governor.dedup_running,
                    "max_queue_depth": self._governor.max_queue_depth,
                },
                "environments": {
                    "active_dev": active_dev,
                    "active_prod": active_prod,
                    "history_dev": env_dist.get("dev", 0),
                    "history_prod": env_dist.get("prod", 0),
                },
                "throughput": {
                    "last_5m": len(recent_5m),
                    "last_1h": len(recent_1h),
                    "avg_wait_ms": avg_wait,
                    "avg_duration_ms": avg_duration,
                    "p95_duration_ms": p95_duration,
                    "priority_distribution": priority_dist,
                    "environment_distribution": env_dist,
                },
                "workers": workers,
                "queue": queue,
                "active": active,
            }

    def get_history(self, limit: int = 100) -> list[dict]:
        """Return recent completion history for timeline view."""
        with self._lock:
            items = list(self._history)[-limit:]
            items.reverse()  # Most recent first
            return [
                {
                    "id": c.id,
                    "workflow_id": c.workflow_id,
                    "workflow_name": c.workflow_name,
                    "priority": c.priority,
                    "priority_label": PRIORITY_LABELS.get(c.priority, ""),
                    "environment": c.environment,
                    "status": c.status,
                    "queued_at": c.queued_at.isoformat(),
                    "started_at": c.started_at.isoformat(),
                    "completed_at": c.completed_at.isoformat(),
                    "duration_ms": c.duration_ms,
                    "wait_ms": c.wait_ms,
                    "worker_id": c.worker_id,
                    "triggered_by": c.triggered_by,
                    "error": c.error,
                }
                for c in items
            ]
