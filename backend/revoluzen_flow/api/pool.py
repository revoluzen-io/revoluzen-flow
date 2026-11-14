"""Execution Pool API — Spark-style worker pool monitoring.

Exposes real-time pool status, worker utilization, queue depth,
and execution history for the admin Execution Pool page.

Note (2026-05-21): /pool/history now reads from the persistent
ExecutionStore (SQLite), not the in-memory WorkerPool._history. The
in-memory list was being wiped on every backend restart, which is the
opposite of what users expect from a "Run History" tab. Overview and
status endpoints still read from the live pool — they describe the
present (workers, queue), not the past (runs that finished).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from revoluzen_flow.auth.deps import current_workspace_id

# 2026-10-23 — router-level posture gate. `POST /api/pool/cancel/{job_id}`
# reached its handler unauthenticated (404 only because the id was made up),
# so anyone who could reach the port could cancel a running job.
router = APIRouter(
    prefix="/api/pool",
    tags=["execution-pool"],
    dependencies=[Depends(current_workspace_id)],
)


def _safe_workspace_id(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception:
        return "default"


def _get_pool():
    from revoluzen_flow.main import app_state
    pool = app_state.get("worker_pool")
    if pool is None:
        raise HTTPException(503, "Worker pool not initialized")
    return pool


@router.get("/status")
async def pool_status():
    """Return full worker pool status — workers, queue, counters, throughput.

    This is the primary endpoint for the Execution Pool admin page.
    Auto-refreshes every 2 seconds on the frontend.
    """
    pool = _get_pool()
    return pool.get_status()


# In-process cache for the pool→executions join (Pass 2, May 10 2026).
# Completed runs are immutable; once we've fetched a run's metadata
# (peak_memory_mb, cpu_seconds, error_message) we never need to re-query
# the executions store. Bounded at the same 500 limit as pool._history
# so the cache size tracks the data set it joins against.
_RUN_META_CACHE: dict[str, dict] = {}
_RUN_META_CACHE_MAX = 500


def _enrich_history_row(row: dict) -> dict:
    """Look up execution-store metadata for a pool history row and
    merge in the per-run resource fields. Cached after first lookup;
    safe to call on every poll because completed runs never change.
    """
    run_id = row.get("id")
    if not run_id:
        return row
    cached = _RUN_META_CACHE.get(run_id)
    if cached is not None:
        return {**row, **cached}
    extra: dict = {}
    try:
        from revoluzen_flow.main import app_state as _as
        store = _as.get("execution_store")
        if store is None:
            return row
        # ExecutionStore exposes get(id) for a stored execution; we
        # only need .metadata + .error_message off the result.
        exe = None
        try:
            exe = store.get(run_id)
        except Exception:
            exe = None
        if exe is not None:
            md = getattr(exe, "metadata", None) or {}
            if isinstance(md, dict):
                if "peak_memory_mb" in md:
                    extra["peak_memory_mb"] = md.get("peak_memory_mb")
                if "cpu_seconds" in md:
                    extra["cpu_seconds"] = md.get("cpu_seconds")
            err = getattr(exe, "error_message", None)
            # Pool history already carries `error` (set by worker_pool on
            # exception), but the executions store has the cleaned
            # error_message produced by the executor — prefer that when
            # present.
            if err and not row.get("error"):
                extra["error"] = err
    except Exception:
        pass
    # Cache even an empty result so we don't hammer the store on rows
    # that have no joinable execution. Evict an arbitrary entry when
    # the cap is hit (FIFO via dict iteration).
    if len(_RUN_META_CACHE) >= _RUN_META_CACHE_MAX:
        try:
            _RUN_META_CACHE.pop(next(iter(_RUN_META_CACHE)))
        except StopIteration:
            pass
    _RUN_META_CACHE[run_id] = extra
    return {**row, **extra}


PRIORITY_LABELS = {1: "P1", 2: "P2", 3: "P3", 4: "P4", 5: "P5"}


def _execution_to_history_row(exe) -> dict:
    """Project an ExecutionRecord into the dict shape the Pool > Run
    History frontend expects. Fields the frontend reads:

      id, workflow_id, workflow_name, status, environment,
      priority, priority_label, triggered_by, worker_id,
      queued_at, started_at, completed_at,
      duration_ms, wait_ms, rows_processed, steps,
      peak_memory_mb, cpu_seconds, error

    Backward-compat: matches the keys the previous pool._history
    output used so the frontend column reads keep working unchanged.
    """
    metadata = getattr(exe, "metadata", None) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    started = getattr(exe, "started_at", None)
    completed = getattr(exe, "completed_at", None)
    started_iso = started.isoformat() if started else ""
    completed_iso = completed.isoformat() if completed else ""

    # Priority isn't on the ExecutionRecord today — fall back to P3
    # (Normal) so the column has a value. Metadata can override it
    # when set by the scheduler.
    priority = int(metadata.get("priority", 3) or 3)

    # Resource metrics live in TWO places depending on the execution path:
    #   subprocess_runner — top-level ExecutionRecord.memory_peak_mb +
    #                        runtime_ms (used when isolation is enabled).
    #   RealtimeExecutor  — metadata["peak_memory_mb"] + metadata["cpu_seconds"]
    #                        (the default in-process path the OSS UI hits).
    # We check top-level first, then metadata. Both are populated by the
    # ResourceMonitor context manager wrapped around the executor in
    # every execution path (api/execution.py for manual runs,
    # scheduling/scheduler.py for scheduled runs). When ResourceMonitor
    # has nothing to report (no psutil installed, or pre-2026-06-02
    # records where the scheduler didn't wrap), both fields stay None
    # and the UI shows "—" instead of a misleading derived value.
    # 2026-06-02 removed the `runtime_ms → cpu_seconds` fallback —
    # wall-clock duration ≠ CPU-seconds (different concept across cores)
    # and conflating them displayed wrong numbers.
    peak_mem = getattr(exe, "memory_peak_mb", None)
    if peak_mem is None:
        peak_mem = metadata.get("peak_memory_mb")
    cpu_sec = metadata.get("cpu_seconds")

    # Step logs are stored as a list on the record; surface the count
    # for the Steps column.
    step_logs = getattr(exe, "step_logs", None) or []
    rows_processed = sum(int(s.rows_processed or 0) for s in step_logs)
    steps_total = int(getattr(exe, "steps_total", 0) or len(step_logs))

    return {
        "id": exe.id,
        "workflow_id": exe.workflow_id,
        "workflow_name": getattr(exe, "workflow_name", "") or "",
        "priority": priority,
        "priority_label": PRIORITY_LABELS.get(priority, ""),
        "environment": metadata.get("environment", "dev"),
        "status": exe.status,
        # Queued isn't tracked — point at started so the column has a value.
        "queued_at": started_iso,
        "started_at": started_iso,
        "completed_at": completed_iso,
        "duration_ms": float(getattr(exe, "duration_ms", 0) or 0),
        "wait_ms": 0,
        "worker_id": metadata.get("worker_id", "") or "",
        "triggered_by": getattr(exe, "triggered_by", "manual") or "manual",
        "error": getattr(exe, "error_message", None) or "",
        "rows_processed": rows_processed,
        "steps": steps_total,
        "peak_memory_mb": peak_mem,
        "cpu_seconds": cpu_sec,
    }


@router.get("/history")
async def pool_history(limit: int = 100, workspace_id: str = Depends(_safe_workspace_id)):
    """Return recent execution history from the persistent execution
    store (NOT the in-memory pool._history, which was wiped on every
    backend restart).

    Scope: workspace-bound, matching every other run-history surface in
    the app (the Executions page). For pre-restart runs
    still in SQLite this is the only path that surfaces them.

    The peak_memory_mb + cpu_seconds columns come straight off the
    persisted ExecutionRecord — no in-process cache layer needed.
    """
    from revoluzen_flow.main import app_state
    exe_store = app_state.get("execution_store")
    if exe_store is None:
        raise HTTPException(503, "Execution store not initialized")

    # ExecutionStore.list_all returns raw dicts (JSON blobs). Rehydrate
    # via ExecutionRecord so the same projector handles every field
    # consistently with the persisted shape.
    from revoluzen_flow.monitoring.store import ExecutionRecord
    raw = exe_store.list_all(limit=limit, workspace_id=workspace_id)
    out = []
    for r in raw:
        try:
            exe = ExecutionRecord(**r)
            out.append(_execution_to_history_row(exe))
        except Exception:
            # Skip a malformed row rather than 500ing the whole page.
            continue
    return out


@router.get("/connections")
async def connection_pool_stats():
    """Return connection-pool stats — total cached driver connections,
    breakdown by connection_id and run_id.

    The pool is a per-run cache (Critical #5 / Phase 2-5) that amortises
    DB connection setup across steps. This endpoint surfaces its live
    state so operators can see whether the pool is helping (entries =
    long-lived runs reusing connections) or saturated (cap warnings in
    logs). When the pool isn't installed, returns a sentinel response
    rather than 503 — the page can still render its 'Connection pool
    not installed' card.
    """
    from revoluzen_flow.main import app_state
    pool = app_state.get("connection_pool") if isinstance(app_state, dict) else None
    if pool is None:
        return {
            "installed": False,
            "total_entries": 0,
            "by_connection": {},
            "by_run": {},
            "max_per_connection": 0,
        }
    s = pool.stats()
    return {
        "installed": True,
        "total_entries": s.total_entries,
        "by_connection": s.by_connection,
        "by_run": s.by_run,
        "max_per_connection": pool._max,
    }


@router.post("/cancel/{job_id}")
async def cancel_job(job_id: str, request: Request):
    """Stop a running job, or take a waiting one out of the queue.

    A job's id is its run id, so this cancels the run the same way as the
    editor's Stop: a query in flight is interrupted, the run is recorded as
    cancelled with who stopped it, and a waiting run never starts. It used to
    cancel a Python future the run did not use, so a running job carried on.
    Scoped to the caller's workspace."""
    pool = _get_pool()
    workspace_id = _safe_workspace_id(request)
    from revoluzen_flow.auth.deps import current_user_optional
    from revoluzen_flow.engine.cancellation import cancel_run, stopped_by_text
    mine = pool.live_run_ids(workspace_id=workspace_id)
    if job_id not in mine and job_id in pool.live_run_ids():
        # Another workspace's run: the same answer as an unknown id.
        raise HTTPException(404, "Job not found or already completed")
    live = job_id in mine
    if live:
        cancel_run(job_id, stopped_by_text(current_user_optional(request)), create=True)
    dequeued = pool.cancel(job_id)
    if not (live or dequeued):
        raise HTTPException(404, "Job not found or already completed")
    return {"cancelled": True, "job_id": job_id}


@router.get("/config")
async def pool_config(workspace_id: str = Depends(_safe_workspace_id)):
    """Return current pool configuration — workers, memory, threads,
    governor tier, and spill-disk health."""
    from revoluzen_flow import runtime_config
    from revoluzen_flow.main import app_state
    import multiprocessing

    pool = _get_pool()

    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu_pct = psutil.cpu_percent(interval=0)
        total_ram_gb = round(mem.total / 1073741824, 1)
        used_ram_gb = round(mem.used / 1073741824, 1)
        avail_ram_gb = round(mem.available / 1073741824, 1)
        try:
            io_wait_pct = float(getattr(psutil.cpu_times_percent(interval=0), "iowait", 0.0))
        except Exception:
            io_wait_pct = 0.0
    except ImportError:
        cpu_pct = 0
        total_ram_gb = 0
        used_ram_gb = 0
        avail_ram_gb = 0
        io_wait_pct = 0.0

    governor_block = None
    governor = app_state.get("global_governor") if isinstance(app_state, dict) else None
    if governor is not None:
        try:
            sample = governor.sample()
            governor_block = {
                "tier": getattr(sample.tier, "value", str(sample.tier)),
                "mem_pct": round(sample.mem_pct, 1),
                "cpu_pct": round(sample.cpu_pct, 1),
                "sampled_at": sample.sampled_at.isoformat(),
                "explanation": _governor_explanation(
                    getattr(sample.tier, "value", str(sample.tier)),
                    sample.mem_pct,
                    sample.cpu_pct,
                ),
            }
        except Exception as exc:
            governor_block = {"tier": "unknown", "error": str(exc)}

    spill_dir = runtime_config.DUCKDB_TEMP_DIRECTORY
    disk_type = _detect_disk_type(spill_dir)

    return {
        "max_workers": pool.max_workers,
        "cpu_cores": multiprocessing.cpu_count() or 4,
        "cpu_percent": cpu_pct,
        "io_wait_percent": round(io_wait_pct, 1),
        "duckdb_memory_limit": runtime_config.DUCKDB_MEMORY_LIMIT,
        "duckdb_threads": runtime_config.DUCKDB_THREADS,
        "duckdb_temp_dir": spill_dir,
        "mode": runtime_config.MODE,
        "ram": {
            "total_gb": total_ram_gb,
            "used_gb": used_ram_gb,
            "available_gb": avail_ram_gb,
        },
        "max_memory_per_worker": runtime_config.DUCKDB_MEMORY_LIMIT,
        "theoretical_max_ram": _worst_case_phrase(
            pool.max_workers, runtime_config.DUCKDB_MEMORY_LIMIT),
        # The sentence above has always been computed and then ignored. If
        # worst case exceeds physical RAM the box can be driven into swap
        # or the OOM killer by concurrency alone, and the operator is the
        # only one who can fix it — so say so, with the actual levers.
        "capacity_risk": _assess_capacity(
            max_workers=pool.max_workers,
            per_worker=runtime_config.DUCKDB_MEMORY_LIMIT,
            total_ram_gb=total_ram_gb,
            available_ram_gb=avail_ram_gb,
            # Cross-reference the theoretical worst case against what the
            # heaviest pipeline has actually peaked at, so the advice is
            # about this workload rather than about the config alone.
            **_observed_peak(workspace_id),
        ),
        "governor": governor_block,
        "spill": {
            "directory": spill_dir,
            "disk_type": disk_type,
            "io_wait_percent": round(io_wait_pct, 1),
            "io_wait_status": (
                "healthy" if io_wait_pct < 10
                else "elevated" if io_wait_pct < 25
                else "saturated"
            ),
        },
    }


@router.get("/footprint")
async def pipeline_footprint(
    limit: int = 50,
    runs_window: int = 200,
    workspace_id: str = Depends(_safe_workspace_id),
):
    """Per-pipeline compute footprint — what each pipeline actually costs."""
    return _compute_footprint(workspace_id, limit=limit, runs_window=runs_window)


def _compute_footprint(workspace_id: str, *, limit: int = 50,
                       runs_window: int = 200) -> dict:
    """Roll recorded runs up per pipeline — what each one actually costs.

    Per-RUN memory and runtime were already captured (the PEAK MEMORY and
    CPU chips on an execution). Nothing rolled them up, so the one
    question an operator needs to size a host — "what does this pipeline
    cost, typically and at its worst?" — had no answer short of reading
    executions one at a time.

    p95 rather than max as the planning number: a single pathological run
    should not dictate the size of a machine, but the average hides the
    runs that actually hurt.
    """
    from revoluzen_flow import runtime_config
    from revoluzen_flow.main import app_state

    exe_store = app_state.get("execution_store")
    if exe_store is None:
        raise HTTPException(503, "Execution store not initialized")

    # Deliberately the same read path as /pool/history rather than a
    # SELECT over `executions`. The memory figure lives in TWO places
    # (top-level column for subprocess runs, metadata for the in-process
    # executor the UI actually hits — see _execution_to_history_row), and
    # a direct query over the column alone reports NULL for every default
    # run. Going through the projector means one dual-source rule, not a
    # second one that drifts.
    from revoluzen_flow.monitoring.store import ExecutionRecord

    want = min(20000, max(1, runs_window) * max(1, limit))
    raw = exe_store.list_all(limit=want, workspace_id=workspace_id)

    buckets: dict[str, dict] = {}
    for r in raw:
        try:
            row = _execution_to_history_row(ExecutionRecord(**r))
        except Exception:  # noqa: BLE001 — one bad row must not 500 the page
            continue
        wid = row.get("workflow_id")
        if not wid:
            continue
        b = buckets.setdefault(wid, {
            "mem": [], "ms": [], "cpu": [], "rows": [],
            "runs": 0, "errors": 0, "name": "",
        })
        # Cap on RUNS, not on collected samples: a pipeline whose runs
        # mostly lack a memory figure would otherwise keep consuming the
        # window and reach back further in history than every other one,
        # making its percentiles incomparable with theirs.
        if b["runs"] >= runs_window:
            continue
        b["runs"] += 1
        b["name"] = b["name"] or (row.get("workflow_name") or "")
        if row.get("status") in ("error", "failed"):
            b["errors"] += 1
        for key, field in (("mem", "peak_memory_mb"), ("ms", "duration_ms"),
                           ("cpu", "cpu_seconds"), ("rows", "rows_processed")):
            v = row.get(field)
            if v:
                b[key].append(float(v))

    def pct(values: list[float], p: float) -> float:
        """Interpolated percentile.

        The nearest-rank form (``s[int(n * p)]``) returns the maximum for
        any n <= 20 at p95, which made the p95 and max columns print the
        same number and the percentile worth nothing at the sample sizes
        a self-hosted install actually has. Interpolating between the two
        neighbouring samples keeps the two columns distinct.
        """
        if not values:
            return 0.0
        s = sorted(values)
        if len(s) == 1:
            return round(s[0], 1)
        pos = p * (len(s) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(s) - 1)
        return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 1)

    def avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 1) if values else 0.0

    per_worker_gb = _parse_mem_gb(runtime_config.DUCKDB_MEMORY_LIMIT)
    total_ram_gb = 0.0
    try:
        import psutil
        total_ram_gb = round(psutil.virtual_memory().total / 1073741824, 1)
    except Exception:  # noqa: BLE001
        pass

    # Never plan into the last of the host's RAM: the OS, the backend
    # itself and the page cache all live there, and a "safe" number that
    # fills the machine to the brim is not safe.
    OS_RESERVE_GB = 2.0
    usable_mb = max(0.0, total_ram_gb - OS_RESERVE_GB) * 1024

    out = []
    for wid, b in buckets.items():
        p95_mb = pct(b["mem"], 0.95)
        # What the host can actually hold if this pipeline ran alongside
        # copies of itself — the number that answers "can I schedule
        # these together?" rather than a bare megabyte figure.
        concurrent = (int(usable_mb // p95_mb)
                      if p95_mb and usable_mb else None)
        out.append({
            "workflow_id": wid,
            "name": b["name"] or wid,
            "runs_sampled": b["runs"],
            # Distinct from runs_sampled: a run with no psutil figure is
            # still a run, and a footprint built on 2 of 50 runs should
            # not be read with the same confidence as one built on 50.
            "memory_samples": len(b["mem"]),
            "error_rate_pct": round(100 * b["errors"] / b["runs"], 1) if b["runs"] else 0,
            "memory_mb": {
                "avg": avg(b["mem"]),
                "p95": p95_mb,
                "max": round(max(b["mem"]), 1) if b["mem"] else 0,
            },
            "runtime_ms": {
                "avg": avg(b["ms"]),
                "p95": pct(b["ms"], 0.95),
                "max": round(max(b["ms"]), 1) if b["ms"] else 0,
            },
            "cpu_seconds": {"avg": avg(b["cpu"]), "p95": pct(b["cpu"], 0.95)},
            "rows_processed": {"avg": avg(b["rows"]), "max":
                               round(max(b["rows"]), 1) if b["rows"] else 0},
            # Louder than a number: this pipeline's p95 is already past
            # the ceiling a single worker is allowed, so it will spill to
            # disk or be killed rather than simply running slowly.
            "exceeds_worker_limit": bool(
                per_worker_gb and p95_mb > per_worker_gb * 1024),
            "safe_concurrent_copies": concurrent,
        })

    out.sort(key=lambda d: d["memory_mb"]["p95"], reverse=True)
    return {
        "host_ram_gb": total_ram_gb,
        "os_reserve_gb": OS_RESERVE_GB,
        "per_worker_limit": runtime_config.DUCKDB_MEMORY_LIMIT,
        "runs_window": runs_window,
        # The sum of every pipeline's p95 — what the host would need if
        # all of them ran at once. Read against host_ram_gb it answers
        # "can I schedule everything together, or must I stagger?"
        "all_pipelines_p95_mb": round(
            sum(p["memory_mb"]["p95"] for p in out), 1),
        "pipelines": out[:limit],
    }


def _parse_mem_gb(limit: str) -> float:
    """'4GB' / '512MB' / '2 GiB' -> gigabytes. 0.0 when unparseable.

    The previous worst-case line hardcoded `max_workers * 4`, so an
    operator who raised DUCKDB_MEMORY_LIMIT to 8GB was still told their
    worst case was 4GB per worker — the number most worth trusting was
    the one that ignored their setting.
    """
    import re

    m = re.match(r"\s*([\d.]+)\s*([kmgt]?i?b?)\s*$", str(limit or ""), re.I)
    if not m:
        return 0.0
    value = float(m.group(1))
    unit = (m.group(2) or "gb").lower().rstrip("b").rstrip("i")
    return value * {"k": 1 / 1048576, "m": 1 / 1024, "g": 1.0, "t": 1024.0}.get(unit, 1.0)


def _worst_case_phrase(max_workers: int, per_worker: str) -> str:
    gb = _parse_mem_gb(per_worker)
    total = round(max_workers * gb, 1)
    return f"{max_workers} workers x {per_worker} = {total} GB worst case"


def _observed_peak(workspace_id: str) -> dict:
    """The heaviest recorded pipeline footprint, for the capacity verdict.

    Returns the kwargs `_assess_capacity` expects, and an empty dict when
    nothing has been measured — an unmeasured host must fall back to the
    worst-case advice rather than to a confident wrong number.
    """
    try:
        fp = _compute_footprint(workspace_id, limit=200, runs_window=100)
    except Exception:  # noqa: BLE001 — the config page must still render
        return {}
    pipelines = fp.get("pipelines") or []
    measured = [p for p in pipelines if p.get("memory_samples")]
    if not measured:
        return {}
    heaviest = max(measured, key=lambda p: p["memory_mb"]["p95"])
    return {
        "observed_peak_mb": heaviest["memory_mb"]["p95"],
        # Runs behind the heaviest footprint, not runs in total: the
        # confidence that matters is the confidence in THAT number.
        "observed_runs": heaviest["memory_samples"],
    }


def _assess_capacity(*, max_workers: int, per_worker: str,
                     total_ram_gb: float, available_ram_gb: float,
                     observed_peak_mb: float = 0.0,
                     observed_runs: int = 0) -> dict:
    """Is this configuration safe on this machine, and if not, what to change?

    Worst case is every worker simultaneously holding its full DuckDB
    allowance. That is pessimistic — most pipelines never approach the
    limit, and DuckDB spills to disk rather than allocating blindly — so
    exceeding RAM is a risk to flag, not proof of breakage. What makes it
    worth flagging is that the failure mode is the whole machine, and the
    fix is three settings the operator already controls.

    ``observed_peak_mb`` is what the heaviest pipeline has ACTUALLY peaked
    at (p95 over the recorded runs). Without it the only honest advice is
    to cap concurrency, and on this machine that advice was wrong in a
    costly direction: 20 workers x 4GB reads as 80 GB against 15.7 GB of
    RAM, so the warning said "drop to 3 workers" — while measurement said
    the heaviest pipeline peaks at 205 MB and all of them together want
    628 MB. Serialising a box that could run everything at once is a real
    cost paid for a theoretical risk. With the measurement in hand the
    diagnosis inverts: the per-worker CEILING is the oversized setting,
    not the concurrency.
    """
    per_gb = _parse_mem_gb(per_worker)
    worst_case_gb = round(max_workers * per_gb, 1)

    if not total_ram_gb or not per_gb:
        return {"status": "unknown",
                "reason": "could not read host RAM or the per-worker limit"}

    ratio = worst_case_gb / total_ram_gb
    # Safe headroom for a workload that spills: the pessimistic total may
    # exceed RAM somewhat without trouble. Past ~2x it is no longer a
    # margin, it is a configuration that cannot be honoured.
    if ratio <= 1.0:
        status, severity = "ok", 0
    elif ratio <= 2.0:
        status, severity = "overcommitted", 1
    else:
        status, severity = "severely_overcommitted", 2

    # How many workers this host can actually honour at the current limit.
    safe_workers = max(1, int(total_ram_gb // per_gb)) if per_gb else max_workers

    out = {
        "status": status,
        "severity": severity,
        "worst_case_gb": worst_case_gb,
        "total_ram_gb": total_ram_gb,
        "available_ram_gb": available_ram_gb,
        "overcommit_ratio": round(ratio, 1),
        "safe_max_workers_at_current_limit": safe_workers,
    }
    if severity == 0:
        out["message"] = (
            f"Worst case {worst_case_gb} GB fits within {total_ram_gb} GB of RAM."
        )
        out["remediation"] = []
        return out

    out["message"] = (
        f"{max_workers} workers x {per_worker} = {worst_case_gb} GB worst case, "
        f"against {total_ram_gb} GB of RAM ({out['overcommit_ratio']}x). If enough "
        f"pipelines run at once the host can be driven into swap or the OOM killer."
    )
    out["remediation"] = [
        f"Cap concurrency: REVOLUZEN_FLOW_MAX_CONCURRENT_RUNS={safe_workers} "
        f"(fits {per_worker} per worker in {total_ram_gb} GB)",
        f"Or lower the per-worker ceiling: REVOLUZEN_FLOW_DUCKDB_MEMORY_LIMIT="
        f"{max(1, int(total_ram_gb // max(max_workers, 1)))}GB",
        "Or add RAM to the host.",
    ]

    # Measurement beats the worst case when we have it. A ceiling many
    # times larger than anything ever observed makes the worst case a
    # number about the CONFIG, not about this workload — and the cheapest
    # fix is the ceiling, so lead with that instead of with concurrency.
    MIN_RUNS = 5  # below this the p95 is an anecdote, not a footprint
    if observed_peak_mb > 0 and observed_runs >= MIN_RUNS:
        headroom = (per_gb * 1024) / observed_peak_mb
        out["observed_peak_mb"] = round(observed_peak_mb, 1)
        out["observed_runs"] = observed_runs
        out["ceiling_vs_observed"] = round(headroom, 1)
        if headroom >= 4:
            suggested = max(1, int((observed_peak_mb * 2) / 1024 + 0.999))
            out["status"] = "overprovisioned_ceiling"
            out["severity"] = 1
            out["message"] = (
                f"The {per_worker} per-worker ceiling is {headroom:.0f}x the "
                f"heaviest footprint actually recorded ({observed_peak_mb:.0f} MB "
                f"p95 over {observed_runs} runs). The {worst_case_gb} GB worst "
                f"case describes the setting, not this workload — so the fix is "
                f"the ceiling, not the concurrency."
            )
            out["remediation"] = [
                f"Lower the ceiling to roughly double the real peak: "
                f"REVOLUZEN_FLOW_DUCKDB_MEMORY_LIMIT={suggested}GB "
                f"— that keeps {max_workers} workers within "
                f"{round(max_workers * suggested, 1)} GB.",
                f"Leave concurrency where it is: observed demand does not "
                f"justify dropping to {safe_workers} workers.",
                "Re-check after a month of runs, or after a pipeline grows.",
            ]

    if available_ram_gb and available_ram_gb < 2:
        out["remediation"].insert(
            0, f"Only {available_ram_gb} GB is free right now — act before the "
               f"next concurrent run, not after.")
    return out


def _governor_explanation(tier: str, mem_pct: float, cpu_pct: float) -> str:
    """Return a one-line human explanation of the governor tier."""
    t = (tier or "").lower()
    if t == "green":
        return f"Healthy — accepting all jobs (memory {mem_pct:.0f}%, CPU {cpu_pct:.0f}%)."
    if t == "yellow":
        if cpu_pct >= 85:
            return f"Throttling — CPU at {cpu_pct:.0f}%. New jobs queued; non-queueable spawns rejected."
        return f"Throttling — memory at {mem_pct:.0f}%. New jobs queued; non-queueable spawns rejected."
    if t == "orange":
        return f"High pressure — memory at {mem_pct:.0f}%. Reducers active; rejecting non-queueable spawns."
    if t == "red":
        return f"Critical — memory at {mem_pct:.0f}%. Rejecting all spawns until pressure relieves."
    return "Status unknown."


def _detect_disk_type(path: str) -> str:
    """Best-effort SSD/HDD detection. Returns 'ssd', 'hdd', or 'unknown'."""
    import os
    if not os.path.exists(path):
        return "unknown"
    try:
        import platform
        system = platform.system().lower()
        if system == "linux":
            stat = os.stat(path)
            major = os.major(stat.st_dev)
            for blk in os.listdir("/sys/block"):
                try:
                    with open(f"/sys/block/{blk}/dev") as f:
                        bm = f.read().strip().split(":")
                        if int(bm[0]) == major:
                            with open(f"/sys/block/{blk}/queue/rotational") as r:
                                rotational = r.read().strip()
                            return "hdd" if rotational == "1" else "ssd"
                except Exception:
                    continue
            return "unknown"
        return "unknown"
    except Exception:
        return "unknown"
