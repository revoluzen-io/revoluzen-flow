"""
Background pipeline scheduler — runs scheduled pipelines automatically.

As of Sprint 2 PR5 step 9 (Apr 22 2026) this routes through
ExecutionManager.spawn_scheduled. The scheduler no longer owns its
own thread or stop_event — the manager's registry counts it, the
manager's shutdown cancels it, and the 70/80/90 tier governor can
defer new work when the host is under pressure.

Public API (start / stop / is_running / active_jobs) is unchanged so
existing callers (main.py lifespan, /health endpoints, drift_daemon
integration) see no difference.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, TYPE_CHECKING

from zoneinfo import ZoneInfo

from revoluzen_flow.ir.governance import is_deactivated
from revoluzen_flow.ir.versioning import PublishedVersionMissing, published_pin
from revoluzen_flow.scheduling.models import EVENT_SCHEDULE_TYPES, Schedule, ScheduleType

if TYPE_CHECKING:
    from revoluzen_flow.engine.execution_manager import TaskHandle

logger = logging.getLogger("revoluzen_flow.scheduler")

# Timezone names we already warned about — one log line per bad name,
# not one per 30-second tick.
_warned_timezones: set[str] = set()


def _in_schedule_tz(dt: datetime, tz_name: str | None) -> datetime:
    """Convert a UTC datetime into the schedule's own timezone.

    Wall-clock checks (daily_time, weekly_days, cron hour/minute fields)
    must be evaluated in the timezone the user wrote them in — the model
    has carried `timezone` since v1, but evaluation previously compared
    raw UTC fields, so a daily_time of "09:00" in US/Eastern fired at
    09:00 UTC. Unknown or unloadable names fall back to UTC (logged once).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    name = (tz_name or "UTC").strip() or "UTC"
    if name.upper() == "UTC":
        return dt.astimezone(timezone.utc)
    try:
        return dt.astimezone(ZoneInfo(name))
    except Exception:
        if name not in _warned_timezones:
            _warned_timezones.add(name)
            logger.warning(
                "Unknown schedule timezone %r — evaluating in UTC. "
                "On Windows, IANA names need the `tzdata` package.", name,
            )
        return dt.astimezone(timezone.utc)


def _as_datetime(value: Any) -> datetime | None:
    """A stored time, which is a datetime or an ISO string, or None."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return value or None


class PipelineScheduler:
    """Manager-backed scheduler that checks and runs scheduled pipelines."""

    def __init__(self, check_interval_seconds: int = 30):
        self._check_interval = check_interval_seconds
        self._running_jobs: dict[str, datetime] = {}  # schedule_id -> started_at
        # schedule_id -> the worker thread running it. Lets the watchdog tell an
        # HONEST long run (thread still alive) from a dead/leaked lock, instead
        # of blindly releasing the lock on a timer — which used to let a
        # short-interval schedule start an overlapping second run mid-flight.
        self._run_threads: dict[str, "threading.Thread"] = {}
        self._handle: "TaskHandle | None" = None
        # Schedule-miss notifier (May 3 2026). Optional. Fires once when
        # an interval schedule is severely overdue (>= 2x interval).
        self._miss_notifier: Any = None
        self._miss_alerted: dict[str, datetime] = {}
        self._credentials_checked_at: datetime | None = None
        # Durable cross-instance lease. Built lazily on first tick because
        # app_state["db"] does not exist yet when main.py constructs this.
        self._lease_obj: "SchedulerLease | None" = None
        # Event triggers (triggers/). The file watcher keeps what it has seen
        # of each folder between looks; after-pipeline events are started by
        # one thread at a time.
        self._file_watcher_obj: Any = None
        self._after_pipeline_lock = threading.Lock()

    # ── Event triggers ───────────────────────────────────────────────────

    @property
    def file_watcher(self):
        if self._file_watcher_obj is None:
            from revoluzen_flow.triggers.file_watch import FileTriggerWatcher

            self._file_watcher_obj = FileTriggerWatcher(self)
        return self._file_watcher_obj

    def is_trigger_busy(self, trigger_id: str) -> bool:
        """True while a run this trigger started is still going. A trigger runs
        one at a time; what arrives meanwhile waits for it."""
        return trigger_id in self._running_jobs

    def start_trigger_run(self, row: dict, **run_options: Any) -> bool:
        """Start one run for an event trigger. False if it could not start."""
        return self._execute_schedule(row["id"], row, **run_options)

    def _spawn_side_job(self, name: str, fn: Callable[..., None], **kwargs: Any) -> bool:
        """Run checking work off the scheduler's own thread."""
        try:
            from revoluzen_flow.engine.execution_manager import ExecutionManager
            ExecutionManager.get().spawn_thread(name=name, fn=fn, owner="scheduler", kwargs=kwargs)
        except RuntimeError:
            threading.Thread(target=fn, kwargs=kwargs, daemon=True, name=name).start()
        except Exception as exc:  # noqa: BLE001 — at capacity: the next tick tries again
            logger.debug("Scheduler could not start %s: %s", name, exc)
            return False
        return True

    def _holds_lease(self) -> bool:
        """Only a running scheduler holding the lease starts anything. A node
        that serves the API only must not take the lease from the one that
        schedules."""
        if not self.is_running:
            return False
        lease = self._lease()
        return lease is not None and lease.acquire_or_renew()

    def _check_file_triggers(self, now: datetime, schedules: list[dict]) -> None:
        for row in schedules:
            if row.get("schedule_type") != ScheduleType.FILE_ARRIVAL.value:
                continue
            if self.file_watcher.due(row, now):
                self._spawn_file_check(row)

    def _spawn_file_check(self, row: dict) -> bool:
        watcher = self.file_watcher
        if not watcher.begin(row["id"]):
            return False
        if not self._spawn_side_job(f"revoluzen_flow-files-{row['id'][:8]}", self._file_check, row=row):
            watcher.end(row["id"])
            return False
        return True

    def _file_check(self, row: dict) -> None:
        try:
            self.file_watcher.check(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("File trigger %s check failed: %s", row.get("id"), exc)
        finally:
            self.file_watcher.end(row["id"])

    def check_file_trigger_now(self, row: dict) -> bool:
        """Look at the folder now rather than at the next poll ("Check now")."""
        if not self._holds_lease():
            return False
        return self._spawn_file_check(row)

    def _recheck_file_trigger(self, trigger_id: str) -> None:
        """After a file's run, start the next waiting file without waiting for
        the poll. The folder is listed again first: the run may have moved files."""
        try:
            from revoluzen_flow.main import app_state

            schedule = app_state["schedule_store"].get(trigger_id)
            if schedule is None or not schedule.enabled or not self._holds_lease():
                return
            self._spawn_file_check(schedule.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not look again for trigger %s: %s", trigger_id, exc)

    def wake_after_pipeline_triggers(self) -> None:
        """Start after-pipeline triggers whose pipeline just finished, now rather
        than at the next tick. On a server that does not schedule, the event
        waits for the one that does."""
        if self.is_running:
            self._spawn_side_job("revoluzen_flow-after-pipeline", self._dispatch_after_pipeline, check_lease=True)

    def _dispatch_after_pipeline(self, check_lease: bool = False) -> None:
        if check_lease and not self._holds_lease():
            return
        if not self._after_pipeline_lock.acquire(blocking=False):
            return  # another pass is under way and will see this event too
        try:
            from revoluzen_flow.main import app_state
            from revoluzen_flow.triggers.chain import dispatch_events

            dispatch_events(self, app_state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("After-pipeline triggers could not be checked: %s", exc)
        finally:
            self._after_pipeline_lock.release()

    def set_miss_notifier(self, notifier: Any) -> None:
        """Wire an ApprovalNotifier (or compatible) for schedule-miss alerts."""
        self._miss_notifier = notifier

    def start(self) -> None:
        """Register the scheduler loop with ExecutionManager.

        Raises RuntimeError if the manager singleton isn't initialized
        yet — startup ordering in main.py puts ExecutionManager first,
        so this should never happen in production. Tests that don't
        initialize the manager must call it first or stub this out.
        """
        if self._handle is not None:
            logger.warning("Scheduler already running")
            return

        from revoluzen_flow.engine.execution_manager import ExecutionManager
        mgr = ExecutionManager.get()
        self._handle = mgr.spawn_scheduled(
            name="pipeline-scheduler",
            fn=self._check_schedules_safe,
            owner="scheduler",
            interval_seconds=self._check_interval,
        )
        logger.info(
            "Scheduler started via ExecutionManager (interval=%ds handle=%s)",
            self._check_interval, self._handle.id,
        )

    def stop(self) -> None:
        """Cancel the scheduler's manager handle. Tolerant of the
        manager being shut down before us (common during app teardown
        since lifespan calls execution_manager.shutdown() first)."""
        if self._handle is None:
            return
        try:
            from revoluzen_flow.engine.execution_manager import ExecutionManager
            mgr = ExecutionManager.get()
            mgr.cancel(self._handle)
        except RuntimeError:
            # Manager singleton gone — shutdown already fired or
            # _reset_for_tests ran. Nothing to cancel.
            pass
        finally:
            self._handle = None
        # Hand the lease back on a clean stop so a standby instance can take
        # over on its next tick instead of waiting out the full TTL. Purely an
        # availability optimisation — if this fails the lease expires and the
        # outcome is a delay, not a correctness problem.
        if self._lease_obj is not None:
            self._lease_obj.release()
        logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._handle is not None

    @property
    def active_jobs(self) -> dict[str, str]:
        """Return currently running schedule jobs."""
        return {sid: ts.isoformat() for sid, ts in self._running_jobs.items()}

    def _lease(self) -> "SchedulerLease | None":
        """Lazily build the lease against the live database handle.

        Lazy because `app_state["db"]` does not exist when the scheduler is
        constructed — main.py builds the scheduler inside the same dict
        literal. Cached per instance so the holder identity is stable.
        """
        if self._lease_obj is None:
            from revoluzen_flow.main import app_state

            db = app_state.get("db")
            if db is None:
                return None
            from revoluzen_flow.scheduling.lease import SchedulerLease

            # TTL generously above the tick interval: the holder renews every
            # tick and must miss three in a row before a takeover is allowed.
            self._lease_obj = SchedulerLease(
                db, ttl_seconds=max(90, self._check_interval * 3)
            )
        return self._lease_obj

    def _check_schedules_safe(self) -> None:
        """Wrapper that catches exceptions the way the old _run_loop
        did. spawn_scheduled already logs per-iteration failures and
        continues, but we keep the same log shape + message so
        existing log-based alerts don't break."""
        try:
            # Only the lease holder fires schedules. The in-process locks
            # below stop *this* process double-running a schedule; they say
            # nothing about a second process, so without this every instance
            # behind a load balancer fired every schedule.
            #
            # No lease available (no database yet, or the table is
            # unreachable) means do not schedule. Missing a tick is
            # recoverable and already alerted on by schedule-miss detection;
            # a duplicate run can double-charge a customer or duplicate rows
            # in their warehouse.
            lease = self._lease()
            if lease is None:
                logger.debug("Scheduler tick skipped: no database handle for the lease")
                return
            if not lease.acquire_or_renew():
                holder = lease.current_holder() or {}
                logger.debug(
                    "Scheduler tick skipped: lease held by %s",
                    holder.get("holder", "another instance"),
                )
                return

            self._check_schedules()
            self._check_credential_expiry()
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

    def _check_credential_expiry(self) -> None:
        """Hourly, and only on the lease holder, so each warning goes out once."""
        now = datetime.now(timezone.utc)
        if self._credentials_checked_at and (now - self._credentials_checked_at).total_seconds() < 3600:
            return
        self._credentials_checked_at = now
        from revoluzen_flow.credentials.expiry import check_in_background
        from revoluzen_flow.main import app_state
        check_in_background(app_state)

    # Grace period before the watchdog even *considers* a run-lock for
    # eviction. Below this, a held lock is always treated as a healthy run
    # (covers the thread-spawn window + every normal short run). Past it, the
    # watchdog checks whether the run thread is still ALIVE before evicting —
    # an honest long run keeps its lock (so the next tick is skipped instead of
    # starting an overlapping run). Override on the instance for tests.
    STUCK_JOB_TIMEOUT_S = 5 * 60
    # Hard backstop: even a still-alive run thread is force-evicted after this
    # long, so a genuinely hung executor (e.g. a network read with no timeout)
    # can't silence a schedule forever. Generous enough that no honest pipeline
    # should hit it. Override on the instance for tests.
    STUCK_JOB_HARD_CAP_S = 6 * 3600

    def _stuck_schedule_ids(self, now: datetime) -> list[str]:
        """Run-locks the watchdog should release.

        A lock is "stuck" only once it's past STUCK_JOB_TIMEOUT_S AND its run
        thread is gone — a normal finish clears its own lock in _run_pipeline's
        finally, so a lingering lock means the thread is still alive. Honest
        long runs are deliberately NOT flagged (their next tick skips instead of
        starting an overlapping run); only a genuinely-hung thread that blows
        past STUCK_JOB_HARD_CAP_S is force-released.
        """
        stuck: list[str] = []
        for sid, started_at in list(self._running_jobs.items()):
            try:
                elapsed = (now - started_at).total_seconds()
            except Exception:
                stuck.append(sid)  # bad timestamp — unrecoverable
                continue
            if elapsed <= self.STUCK_JOB_TIMEOUT_S:
                continue  # within grace — always treat as a healthy run
            th = self._run_threads.get(sid)
            if th is not None and th.is_alive() and elapsed < self.STUCK_JOB_HARD_CAP_S:
                continue  # honest long run — hold the lock, prevent overlap
            stuck.append(sid)
        return stuck

    def _check_schedules(self) -> None:
        """Check all enabled schedules and execute if due."""
        from revoluzen_flow.main import app_state

        schedule_store = app_state["schedule_store"]
        schedules = schedule_store.list_all()
        now = datetime.now(timezone.utc)

        # PR12 — pull workflow store once per tick to check is_active flags.
        wf_store = app_state.get("store")

        # Watchdog — release a schedule's run-lock only when the run is
        # genuinely gone or has blown past a generous hard cap. A normally
        # finishing run clears its own lock in _run_pipeline's finally, so a
        # lock still held past the grace means the run thread is still ALIVE —
        # an honest long run. Keeping its lock makes the next tick SKIP rather
        # than start an overlapping second run (the bug short-interval
        # schedules hit when the old blind timer freed the lock mid-run).
        stuck_ids = self._stuck_schedule_ids(now)
        for sid in stuck_ids:
            elapsed = (now - self._running_jobs[sid]).total_seconds() if sid in self._running_jobs else 0
            logger.warning(
                "Scheduler watchdog: evicting stuck job %s (running %.0fs) — "
                "the executor thread did not complete; future ticks were "
                "blocked until this eviction.",
                sid, elapsed,
            )
            self._running_jobs.pop(sid, None)
            self._run_threads.pop(sid, None)
            # A report is not a pipeline run: the next tick retries it, and it
            # has no schedule row or run record to mark.
            if sid.startswith("report:"):
                continue
            # Best-effort: mark the schedule as errored so /api/schedules
            # shows a non-null last_run_at the user can see in the UI.
            try:
                schedule_store.record_run(sid, "error")
            except Exception:
                pass
            # Also surface the eviction as a "cancelled" execution row
            # so the user finds evidence in the Executions UI instead of
            # silently wondering why their schedule never produces runs.
            try:
                exe_store = app_state.get("execution_store") if isinstance(app_state, dict) else None
                if exe_store is not None:
                    sched_obj = None
                    try:
                        sched_obj = schedule_store.get(sid)
                    except Exception:
                        pass
                    wf_id = (sched_obj.workflow_id if sched_obj else "") if sched_obj else ""
                    ws_id = (getattr(sched_obj, "workspace_id", "default") if sched_obj else "default")
                    from revoluzen_flow.monitoring.store import ExecutionRecord as _Exe
                    exe_store.record(_Exe(
                        workflow_id=wf_id or "(unknown)",
                        workflow_name=getattr(sched_obj, "name", "") or "(scheduled run)",
                        workspace_id=ws_id,
                        status="cancelled",
                        triggered_by="schedule",
                        completed_at=now,
                        duration_ms=int(elapsed * 1000),
                        error_message=(
                            f"Watchdog cancelled: scheduled run did not complete within "
                            f"{int(self.STUCK_JOB_TIMEOUT_S / 60)} minutes."
                        ),
                    ))
            except Exception as exc:
                logger.debug("Watchdog could not write cancelled execution record: %s", exc)

        for sched_dict in schedules:
            schedule_id = sched_dict["id"]

            # Skip disabled schedules
            if not sched_dict.get("enabled", False):
                continue

            # File-arrival and after-pipeline triggers fire on events, below.
            if sched_dict.get("schedule_type") in EVENT_SCHEDULE_TYPES:
                continue

            # Skip if already running
            if schedule_id in self._running_jobs:
                continue

            # Skip if the parent workflow is deactivated. The schedule model
            # carries no environment (see _run_pipeline), so a pause in either
            # environment stops it, the same rule published endpoints follow.
            # Best-effort: if we can't resolve the workflow, fall through and
            # let the executor decide (legacy behavior).
            if wf_store is not None:
                try:
                    workflow_id = sched_dict.get("workflow_id")
                    if workflow_id:
                        v = wf_store.get(workflow_id)
                        if v and is_deactivated(v.workflow):
                            logger.debug(
                                "Skipping schedule %s — workflow %s is deactivated",
                                schedule_id, workflow_id,
                            )
                            continue
                except Exception:
                    pass

            # Check if schedule is due
            if self._is_due(sched_dict, now):
                self._maybe_alert_miss(schedule_id, sched_dict, now)
                self._execute_schedule(schedule_id, sched_dict)

        # N10 round 4 — also check Deployments with cron schedules.
        # Same poll cadence, parallel loop. Failure to load / fire a
        # deployment is logged but never blocks the schedule loop above.
        self._check_deployments(now, wf_store)
        self._check_reports(now)

        # Event triggers: folders due a look, and pipelines that finished
        # while their after-pipeline triggers were busy or on another server.
        try:
            self._check_file_triggers(now, schedules)
        except Exception as exc:  # noqa: BLE001
            logger.warning("File triggers could not be checked (non-fatal): %s", exc)
        self._dispatch_after_pipeline()

    def _check_reports(self, now: datetime) -> None:
        """Send any saved report whose schedule has come round.

        Same tick, same lease, same worker threads as pipeline schedules. A
        report that cannot be read or sent never holds up the pipelines.
        """
        try:
            from revoluzen_flow.main import app_state
            from revoluzen_flow.reports.builder.deliver import due_reports

            due = due_reports(app_state, now)
        except Exception as exc:
            logger.warning("Could not check for scheduled reports (non-fatal): %s", exc)
            return

        for report, occurrence in due:
            job_key = f"report:{report.id}"
            if job_key in self._running_jobs:
                continue
            self._running_jobs[job_key] = datetime.now(timezone.utc)
            run_kwargs = {"job_key": job_key, "report_id": report.id, "occurrence": occurrence}
            try:
                from revoluzen_flow.engine.execution_manager import ExecutionManager
                ExecutionManager.get().spawn_thread(
                    name=f"revoluzen_flow-report-{report.id[:8]}",
                    fn=self._send_report,
                    owner="scheduler",
                    kwargs=run_kwargs,
                )
            except RuntimeError:
                threading.Thread(
                    target=self._send_report, kwargs=run_kwargs,
                    daemon=True, name=f"revoluzen_flow-report-{report.id[:8]}",
                ).start()
            except Exception as exc:
                # At capacity: free the slot so the next tick tries again.
                self._running_jobs.pop(job_key, None)
                logger.error("Scheduler could not start report %s: %s", report.id, exc)

    def _send_report(self, job_key: str, report_id: str, occurrence: datetime) -> None:
        """One scheduled report, always releasing its slot."""
        self._run_threads[job_key] = threading.current_thread()
        try:
            from revoluzen_flow.main import app_state
            from revoluzen_flow.reports.builder.deliver import send_scheduled

            send_scheduled(app_state, report_id, occurrence)
        except Exception as exc:
            logger.warning("Scheduled report %s failed (non-fatal): %s", report_id, exc)
        finally:
            self._running_jobs.pop(job_key, None)
            self._run_threads.pop(job_key, None)

    def _check_deployments(self, now: datetime, wf_store) -> None:
        """Iterate enabled deployments with a cron schedule and fire
        any that are due. Mirrors the schedule loop's shape so the
        debugging surface stays familiar.
        """
        try:
            from revoluzen_flow.api.deployments import list_enabled_with_schedule, mark_fired
        except Exception as exc:
            logger.debug("Deployments module not loadable, skipping: %s", exc)
            return

        try:
            deployments = list_enabled_with_schedule()
        except Exception as exc:
            logger.warning("list_enabled_with_schedule failed (non-fatal): %s", exc)
            return

        for d in deployments:
            # Dedup against the running-jobs map using a stable id key.
            # "dep:" prefix avoids collision with schedule ids.
            running_key = f"dep:{d.id}"
            if running_key in self._running_jobs:
                continue

            # Pause check — same rule as schedules: a deactivated workflow
            # is skipped silently.
            if wf_store is not None:
                try:
                    v = wf_store.get(d.workflow_id)
                    if v and is_deactivated(v.workflow):
                        logger.debug(
                            "Skipping deployment %s — workflow %s is deactivated",
                            d.id, d.workflow_id,
                        )
                        continue
                except Exception:
                    pass

            # Cron due-check — reuse the existing _check_cron helper.
            cron_expr = (d.schedule or {}).get("cron", "").strip()
            if not cron_expr:
                continue
            if not self._check_cron(now, d.last_run_at, cron_expr):
                continue

            # Synthesize a "schedule-shaped" dict so _execute_schedule
            # works unchanged. Deployment parameters flow into the
            # workflow via the params dict (the executor handles
            # `${param.<name>}` substitution in step params).
            sched_shaped = {
                "id": running_key,
                "workflow_id": d.workflow_id,
                "workspace_id": d.workspace_id,
                "name": d.name,
                "params": d.parameters or {},
                "schedule_type": "cron",
                "cron_expression": cron_expr,
                "enabled": True,
            }

            logger.info(
                "Scheduler firing deployment %s (%s) on cron %r",
                d.id, d.name, cron_expr,
            )
            try:
                # Stamped only once it has started: a run turned away at
                # capacity tries again on the next tick of this minute.
                if self._execute_schedule(running_key, sched_shaped):
                    mark_fired(d.id, now)
            except Exception as exc:
                logger.warning("Deployment %s fire failed: %s", d.id, exc)

    def _maybe_alert_miss(self, schedule_id: str, sched: dict, now: datetime) -> None:
        """One-shot miss alert when an interval schedule is severely overdue."""
        notifier = self._miss_notifier
        if notifier is None:
            return
        try:
            cfg = notifier._get_config()
            if not cfg.get("notify_on_schedule_miss", True):
                return
        except Exception:
            return

        stype = sched.get("schedule_type", "")
        if stype not in (ScheduleType.INTERVAL, "interval"):
            return  # Daily/weekly/cron miss detection deferred

        interval_min = int(sched.get("interval_minutes", 0))
        if interval_min <= 0:
            return

        last_run = sched.get("last_run_at")
        if not last_run:
            return
        try:
            if isinstance(last_run, str):
                last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            else:
                last_run_dt = last_run
        except (ValueError, TypeError):
            return

        elapsed_min = (now - last_run_dt).total_seconds() / 60
        if elapsed_min < interval_min * 2:
            return

        last_alert = self._miss_alerted.get(schedule_id)
        if last_alert and (now - last_alert).total_seconds() / 60 < interval_min:
            return

        self._miss_alerted[schedule_id] = now
        miss_min = int(elapsed_min - interval_min)
        expected = last_run_dt + timedelta(minutes=interval_min)
        try:
            notifier.on_schedule_miss(
                workflow_id=sched.get("workflow_id", ""),
                workflow_name=sched.get("name") or sched.get("workflow_id", "(unknown)"),
                schedule_id=schedule_id,
                expected_run_at=expected,
                miss_minutes=miss_min,
            )
            logger.info("schedule-miss alert fired for %s (overdue %dm)", schedule_id, miss_min)
        except Exception as exc:
            logger.warning("schedule-miss notify failed (non-fatal): %s", exc)

    def _is_due(self, sched: dict, now: datetime) -> bool:
        """Check if a schedule should run based on its type and timing."""
        stype = sched.get("schedule_type", "cron")
        last_run_dt = _as_datetime(sched.get("last_run_at"))

        if stype == ScheduleType.INTERVAL or stype == "interval":
            interval_min = sched.get("interval_minutes", 60)
            if last_run_dt is None:
                return True  # Never run — execute now
            elapsed = (now - last_run_dt).total_seconds() / 60
            return elapsed >= interval_min

        # Daily / weekly / cron compare wall-clock fields, so evaluate
        # them in the schedule's timezone. Interval (above) is pure
        # elapsed time and stays in UTC. zoneinfo also makes the
        # already-ran-today date comparison DST-correct.
        tz_name = sched.get("timezone")
        local_now = _in_schedule_tz(now, tz_name)
        local_last = _in_schedule_tz(last_run_dt, tz_name) if last_run_dt else None

        if stype == ScheduleType.DAILY or stype == "daily":
            daily_time = sched.get("daily_time", "00:00")
            return self._check_daily(local_now, local_last, daily_time)

        elif stype == ScheduleType.WEEKLY or stype == "weekly":
            weekly_days = sched.get("weekly_days", [])
            weekly_time = sched.get("weekly_time", "00:00")
            if local_now.weekday() not in weekly_days:
                return False
            return self._check_daily(local_now, local_last, weekly_time)

        elif stype == ScheduleType.CRON or stype == "cron":
            cron_expr = sched.get("cron_expression", "")
            if not cron_expr:
                return False
            # When the last run started, not when it finished: a long run can
            # finish inside the next matching minute, and that minute is still
            # owed its run. Rows written before last_fired_at existed have
            # only the finish time.
            fired = _as_datetime(sched.get("last_fired_at")) or last_run_dt
            local_fired = _in_schedule_tz(fired, tz_name) if fired else None
            return self._check_cron(local_now, local_fired, cron_expr)

        # File-arrival and after-pipeline triggers are never due on the clock;
        # triggers/ starts them when their event happens.
        return False

    def _check_daily(self, now: datetime, last_run: datetime | None, time_str: str) -> bool:
        """Check if a daily schedule is due at the given time."""
        try:
            parts = time_str.split(":")
            target_hour = int(parts[0])
            target_minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return False

        # Check if we're within the target time window (within check_interval)
        if now.hour != target_hour:
            return False
        if abs(now.minute - target_minute) > (self._check_interval // 60 + 1):
            return False

        # Ensure we haven't already run today
        if last_run:
            if last_run.date() == now.date():
                return False

        return True

    def _check_cron(self, now: datetime, last_run: datetime | None, cron_expr: str) -> bool:
        """
        Simple cron expression checker.
        Supports: minute hour day_of_month month day_of_week
        Supports: *, */N, specific numbers, comma lists

        ``last_run`` is when it last fired. It fires once in a matching
        minute, however many ticks land in that minute.
        """
        parts = cron_expr.strip().split()
        if len(parts) < 5:
            return False

        fields = [
            (now.minute, parts[0], 0, 59),     # minute
            (now.hour, parts[1], 0, 23),        # hour
            (now.day, parts[2], 1, 31),          # day of month
            (now.month, parts[3], 1, 12),        # month
        ]

        for current_val, pattern, min_val, max_val in fields:
            if not self._cron_field_matches(current_val, pattern, min_val, max_val):
                return False

        if not self._cron_weekday_matches(now, parts[4]):
            return False

        return last_run is None or not self._same_minute(now, last_run)

    @classmethod
    def _cron_weekday_matches(cls, now: datetime, pattern: str) -> bool:
        """The day-of-week field, numbered as in standard cron and everywhere
        Flow writes or shows one: 0 and 7 are Sunday, 1 is Monday, 6 is
        Saturday. (A weekly schedule's ``weekly_days`` is a different field
        and counts from Monday.)"""
        day = now.isoweekday()  # Monday is 1, Sunday is 7
        return any(
            cls._cron_field_matches(value, pattern, 0, 7)
            for value in ((0, 7) if day == 7 else (day,))
        )

    @staticmethod
    def _same_minute(a: datetime, b: datetime) -> bool:
        """Whether two times fall in one minute. A time with no zone is UTC."""
        a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
        b = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
        return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)

    @staticmethod
    def _cron_field_matches(value: int, pattern: str, min_val: int, max_val: int) -> bool:
        """Check if a cron field pattern matches the current value."""
        if pattern == "*":
            return True

        # */N — step
        if pattern.startswith("*/"):
            try:
                step = int(pattern[2:])
                return value % step == 0
            except ValueError:
                return False

        # Comma-separated list: 1,3,5
        if "," in pattern:
            try:
                values = [int(v.strip()) for v in pattern.split(",")]
                return value in values
            except ValueError:
                return False

        # Range: 1-5
        if "-" in pattern:
            try:
                low, high = pattern.split("-", 1)
                return int(low) <= value <= int(high)
            except ValueError:
                return False

        # Exact number
        try:
            return value == int(pattern)
        except ValueError:
            return False

    def _execute_schedule(self, schedule_id: str, sched: dict, **run_options: Any) -> bool:
        """Execute a scheduled pipeline in a separate thread. True if it started.

        ``run_options`` are passed on to :meth:`_run_pipeline`: an event trigger
        names itself and adds its own values (a file's path, say).
        """
        workflow_id = sched.get("workflow_id")
        if not workflow_id:
            return False

        # Pull the schedule's workspace_id — it was stamped at create
        # time and is the authoritative tenant for every run this
        # schedule produces. The scheduler daemon has no "caller" and
        # therefore no request-scoped workspace, so this is the only
        # place a background run can learn which tenant it belongs to.
        sched_workspace = sched.get("workspace_id") or "default"

        if schedule_id in self._running_jobs and run_options:
            return False  # an event trigger runs one at a time
        self._running_jobs[schedule_id] = datetime.now(timezone.utc)
        run_kwargs = {
            "schedule_id": schedule_id,
            "workflow_id": workflow_id,
            "schedule_name": sched.get("name", ""),
            "workspace_id": sched_workspace,
            **run_options,
        }
        if sched.get("schedule_type") in EVENT_SCHEDULE_TYPES:
            run_kwargs["published_only"] = True
        try:
            from revoluzen_flow.engine.execution_manager import ExecutionManager
            ExecutionManager.get().spawn_thread(
                name=f"revoluzen_flow-job-{schedule_id[:8]}",
                fn=self._run_pipeline,
                owner="scheduler",
                kwargs=run_kwargs,
            )
        except RuntimeError:
            # Manager singleton not initialized (unit tests / early boot)
            # — fall back to a plain thread so behaviour is unchanged.
            threading.Thread(
                target=self._run_pipeline,
                kwargs=run_kwargs,
                daemon=True,
                name=f"revoluzen_flow-job-{schedule_id[:8]}",
            ).start()
        except Exception as e:
            # Admission denied (governor at capacity) or spawn failure —
            # release the slot immediately so the next tick retries,
            # instead of waiting STUCK_JOB_TIMEOUT_S for the watchdog.
            self._running_jobs.pop(schedule_id, None)
            logger.error(
                "Scheduler could not spawn run for schedule %s: %s",
                schedule_id, e,
            )
            return False
        return True

    def _run_pipeline(
        self,
        schedule_id: str,
        workflow_id: str,
        schedule_name: str,
        workspace_id: str = "default",
        *,
        triggered_by: str = "schedule",
        parameter_overrides: dict | None = None,
        run_metadata: dict | None = None,
        published_only: bool = False,
        on_finish: Callable[[str, str], None] | None = None,
    ) -> None:
        """Run a pipeline and update schedule status.

        ``workspace_id`` is the tenant the schedule was created under.
        Every downstream write (execution record, execution log, alert
        log) inherits it so the background path can never silently
        cross a tenant boundary even if a workflow id collides.

        Event triggers (triggers/) run through here too. ``triggered_by``
        is what the run history shows; ``parameter_overrides`` go on top of
        the schedule's saved values (a file trigger's file path);
        ``run_metadata`` is kept on the run record; ``published_only`` refuses
        a pipeline that is not published rather than running its draft; and
        ``on_finish(status, execution_id)`` is told how the run ended.

        Defensive contract: this function GUARANTEES that
        `_running_jobs[schedule_id]` is popped before it returns. The
        previous version could leak the slot (e.g. KeyError on
        `app_state["alert_store"]`) leaving the schedule permanently
        skipped on every subsequent tick.
        """
        # Register this worker thread so the watchdog can check run liveness:
        # an honest long run keeps its lock (next tick skips, no overlap), a
        # dead/leaked one is evicted. Popped in the finally below.
        self._run_threads[schedule_id] = threading.current_thread()

        # Outer try/finally — pop the running-jobs slot no matter what
        # happens below, including module-import failures and KeyErrors
        # on app_state lookups.
        from revoluzen_flow.main import app_state

        # Best-effort store handles. None on missing keys instead of
        # raising — a partially-initialised app shouldn't permanently
        # silence the scheduler.
        store = app_state.get("store") if isinstance(app_state, dict) else None
        schedule_store = app_state.get("schedule_store") if isinstance(app_state, dict) else None
        execution_store = app_state.get("execution_store") if isinstance(app_state, dict) else None
        alert_store = app_state.get("alert_store") if isinstance(app_state, dict) else None

        logger.info(
            f"Scheduler executing pipeline {workflow_id} "
            f"(schedule: {schedule_name or schedule_id}, workspace: {workspace_id})"
        )

        start = time.time()
        status = "error"
        error_msg = ""
        # Persist a "running" execution record IMMEDIATELY so the user
        # sees the scheduled run in the Executions UI without waiting
        # for the executor to return. If anything below crashes or
        # hangs, the row is still there with whatever status the
        # finally block was able to set.
        from revoluzen_flow.monitoring.store import ExecutionRecord
        exe: ExecutionRecord | None = None

        try:
            # Pool registration used to be hand-rolled here, reaching into
            # `pool._lock` / `_workers` / `_active` and marking a slot busy
            # BEFORE the workflow was even loaded — so a schedule that
            # failed its lookup still showed as a running worker. It also
            # only tracked; nothing capped the run. `run_through_pool`
            # around the execute call below now does both, at the moment
            # work actually starts.
            #
            # The schedule model carries no environment field, so
            # scheduled runs are DEV. Named here rather than inside the
            # old `if pool:` branch, where it was left unbound on every
            # path that did not have a pool.
            sched_env = "dev"

            # Persist a stub execution record FIRST — before the
            # workflow lookup — so the user sees the scheduled fire in
            # the Executions UI even when the lookup fails (workspace
            # mismatch, deleted workflow, etc.). The finally block
            # updates it with the final status + error_message.
            exe = ExecutionRecord(
                workflow_id=workflow_id,
                workflow_name=schedule_name or "(scheduled run)",
                started_at=datetime.fromtimestamp(start, timezone.utc),
                workspace_id=workspace_id,
                steps_total=0,
                triggered_by=triggered_by,
                status="running",
            )
            if run_metadata or triggered_by != "schedule":
                # Which trigger started it, and on what (the file, the run
                # that finished before it), for whoever reads the run later.
                exe.metadata = {
                    **(exe.metadata or {}),
                    **(run_metadata or {}),
                    "schedule_id": schedule_id,
                }
            if execution_store is not None:
                try:
                    execution_store.record(exe)
                except Exception as exc:
                    logger.warning("Could not persist initial execution record: %s", exc)

            # Get the workflow — scoped to the schedule's workspace so
            # a cross-tenant workflow id collision is impossible.
            if store is None:
                error_msg = "Workflow store unavailable"
                logger.error(error_msg)
                return
            # A published pipeline runs its published version, whatever has
            # been saved since.
            try:
                versioned = store.live_version(workflow_id, workspace_id=workspace_id)
            except PublishedVersionMissing as exc:
                error_msg = str(exc)
                logger.error(error_msg + f" (schedule {schedule_id})")
                return
            if published_only and versioned is not None:
                # An event trigger never runs a draft: no published version,
                # no run, the same refusal as a published version gone missing.
                latest = store.get(workflow_id, workspace_id=workspace_id)
                if latest is None or published_pin(latest) is None:
                    error_msg = (
                        f'"{versioned.workflow.name or workflow_id}" is not published, so it was not run. '
                        "Publish it to let its triggers start it."
                    )
                    logger.error(error_msg + f" (trigger {schedule_id})")
                    return
            if not versioned:
                error_msg = (
                    f"Workflow {workflow_id} not found in workspace {workspace_id} — "
                    "the schedule may have been created under a different workspace, "
                    "or the workflow was deleted."
                )
                logger.error(error_msg + f" (schedule {schedule_id})")
                return

            wf = versioned.workflow

            # Update the stub record with workflow details now that we
            # have them — name, project, step count, IR snapshot.
            exe.workflow_name = wf.name
            exe.project_id = getattr(wf, "project_id", "default")
            exe.steps_total = len(wf.steps)
            exe.workflow_snapshot = wf.model_dump(mode="json")
            exe.workflow_version = getattr(versioned, "version", None)
            if execution_store is not None:
                try:
                    execution_store.record(exe)
                except Exception:
                    pass

            # Look up parameter_values from the schedule store rather
            # than the missing `sched` local that previous versions
            # referenced (silent NameError on every run).
            schedule_param_values: dict = {}
            if schedule_store is not None:
                try:
                    sched_obj = schedule_store.get(schedule_id, workspace_id=workspace_id)
                    if sched_obj is not None:
                        schedule_param_values = (
                            getattr(sched_obj, "parameter_values", None) or {}
                        )
                except Exception:
                    schedule_param_values = {}
            if parameter_overrides:
                # The trigger's own values, such as the path of the file that
                # arrived, win over the values saved on the schedule.
                schedule_param_values = {**schedule_param_values, **parameter_overrides}
            if schedule_param_values:
                # Kept on the record, encrypted where secret, so a Replay of
                # this run uses the same values (the same file, for a file
                # trigger) as the run did.
                try:
                    from revoluzen_flow.ir.step_secrets import seal_parameter_values
                    exe.metadata = {
                        **(exe.metadata or {}),
                        "parameter_values": seal_parameter_values(schedule_param_values, wf.parameters),
                    }
                except Exception:
                    pass

            # Execute — pass through any parameter_values declared on the
            # schedule. System placeholders (${utcnow:%Y-%m-%d}, ${run_id})
            # in step params resolve at fire time, so a daily schedule
            # actually gets today's date every run rather than the date
            # the schedule was created.
            #
            # 2026-06-02: wrap in ResourceMonitor so the Pool → Run History
            # Memory + CPU columns populate for scheduled runs too. The
            # HTTP-API execution paths (api/execution.py:289/574/1113)
            # already do this; the scheduler was the missing call site,
            # which is why every scheduled run in Pool → Run History showed
            # "—" in those columns even though shorter manual runs did.
            from revoluzen_flow.engine.admission import run_through_pool as _run_through_pool
            from revoluzen_flow.engine.executor import WorkflowExecutor
            from revoluzen_flow.engine.resource_monitor import ResourceMonitor
            data_dir = app_state["data_dir"]
            # Pass app_state so the executor's per-step output capture
            # (step_output_store) runs, and run_id=exe.id so those captures
            # key under the SAME id the Executions UI queries. Without both,
            # scheduled runs showed "No output captured for step ..." in the
            # lineage drawer while manual runs (which pass both) worked.
            executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state)
            from revoluzen_flow.security.execution_codes import mint_for_run

            def _execute():
                return executor.execute_workflow(
                    wf,
                    parameter_values=schedule_param_values or None,
                    run_id=exe.id,
                    execution_code=mint_for_run(wf, job_run_id=exe.id),
                )

            # Scheduled runs go through the pool's admission point like
            # every other path (2026-10-27). Before this they called the
            # executor straight from the schedule thread: nothing capped
            # them, so `MAX_CONCURRENT_RUNS=4` bounded API runs at 4
            # while any number of schedules fired alongside, and none of
            # them appeared on the Execution Pool page.
            #
            # `run_admitted` runs the work on THIS thread, so the
            # scheduler's own watchdog — which checks whether the run
            # thread is still alive — keeps working unchanged.
            with ResourceMonitor() as _resmon:
                result = _run_through_pool(
                    app_state,
                    run_id=exe.id,
                    workflow_id=getattr(wf, "id", "") or "",
                    workflow_name=getattr(wf, "name", "") or "scheduled run",
                    workspace_id=workspace_id,
                    environment=sched_env,
                    triggered_by=triggered_by,
                    schedule_id=schedule_id,
                    fn=_execute,
                )

            duration_ms = int((time.time() - start) * 1000)

            # Persist the captured resource metrics on the execution
            # record's metadata so /api/pool/history picks them up via
            # the existing metadata.get("peak_memory_mb") path in
            # api/pool.py. Round to 2 dp to match the manual-run paths.
            # No-op when psutil isn't installed (ResourceMonitor returns
            # 0.0 / 0.0); UI shows "0.0 MB" + "0.0s" in that case so the
            # operator can tell "monitor ran but psutil missing" from
            # "monitor never ran" (which still shows "—").
            try:
                if exe.metadata is None:
                    exe.metadata = {}
                exe.metadata["peak_memory_mb"] = round(_resmon.peak_memory_mb, 2)
                exe.metadata["cpu_seconds"] = round(_resmon.cpu_seconds, 2)
            except Exception:
                # Best-effort: never let metric capture break the run.
                pass

            # Update execution record
            result_status = result.status if hasattr(result, "status") else str(result)
            if result_status == "success":
                status = "success"
                exe.status = "success"
                exe.steps_completed = exe.steps_total
            elif result_status == "cancelled":
                # Someone pressed Stop. Not a failure: no failure alert, and
                # the record says who stopped it.
                status = "cancelled"
                exe.status = "cancelled"
                error_msg = getattr(result, "stop_message", None) or "Stopped before it finished."
                exe.error_message = error_msg
            else:
                status = "error"
                exe.status = "error"
                # Get error from step results
                if hasattr(result, "step_results"):
                    failed = [sr for sr in result.step_results.values() if sr.status == "error"]
                    error_msg = failed[0].error if failed else "Pipeline execution failed"
                exe.error_message = error_msg

            # Capture per-step metadata for the alert payload — lets the
            # email render a real lineage with each step coloured by its
            # outcome and the failed step highlighted. Step IR fields are
            # `id`, `type` (StepType enum), `label` — NOT `name` /
            # `step_type` (the prior version used wrong attr names, the
            # AttributeError was swallowed, and the email skipped the
            # lineage block). Best-effort: any extraction failure yields
            # an empty list and the email omits the lineage section.
            _alert_steps: list[dict] = []
            _alert_step_metrics: list[dict] = []  # for the Execution Timeline section
            _alert_failed = ""
            # 2026-05-28 — scheduled runs now populate exe.step_logs the
            # same way manual runs do (workflows.py:test_workflow +
            # execution.py:run_workflow). Without this, the Executions
            # UI's Lineage tab shows "No lineage data" for every
            # schedule-fired execution even though step_results are
            # available — the frontend reads from step_logs, not from
            # the alert payload. Reported in internal testing, 2026-05-28.
            from revoluzen_flow.monitoring.store import StepLog as _StepLog
            _step_logs: list = []
            try:
                step_results = getattr(result, "step_results", {}) or {}
                for s in wf.steps:
                    sr = step_results.get(s.id) if isinstance(step_results, dict) else None
                    s_status = (getattr(sr, "status", "") if sr else "") or ""
                    _step_type = getattr(s, "type", "")
                    _step_type_str = getattr(_step_type, "value", str(_step_type) if _step_type else "")
                    _step_label = getattr(s, "label", "") or s.id
                    if s_status == "error" and not _alert_failed:
                        _alert_failed = _step_label
                    _alert_steps.append({
                        "id": s.id,
                        "name": _step_label,
                        "type": _step_type_str,
                        "status": s_status,
                        # Per-step metrics — picked up by the alert
                        # email's lineage card rendering. Matches the
                        # `rows_processed` + `duration_ms` keys the
                        # notifier already reads. Manual-run path
                        # builds the same shape in execution.py.
                        "rows_processed": int(getattr(sr, "row_count", 0) or 0) if sr is not None else 0,
                        "duration_ms": float(getattr(sr, "duration_ms", 0) or 0) if sr is not None else 0,
                    })
                    # Richer metrics for the alert email's timeline table.
                    # Pulls duration + row_count from StepRunResult (both
                    # already tracked); rows_in remains None until the
                    # executor surfaces upstream-row counts. Depth is 0
                    # for now — true nesting needs the executor's
                    # parent-chain instrumentation (separate sprint).
                    _alert_step_metrics.append({
                        "name": _step_label,
                        "type": _step_type_str,
                        "status": s_status,
                        "duration_ms": (getattr(sr, "duration_ms", 0) if sr else 0) or 0,
                        "rows_in": None,  # not yet tracked
                        "rows_out": (getattr(sr, "row_count", 0) if sr else 0) or 0,
                        "depth": 0,  # flat IR — control-flow nesting tracked at runtime
                    })
                    # Build the per-step log entry the Executions UI reads.
                    # Same shape the manual-run path produces — keep field
                    # names + types aligned with StepLog in monitoring/store.py.
                    if sr is not None:
                        _step_logs.append(_StepLog(
                            step_id=s.id,
                            step_name=_step_label,
                            step_type=_step_type_str,
                            status=s_status or "unknown",
                            rows_processed=int(getattr(sr, "row_count", 0) or 0),
                            duration_ms=float(getattr(sr, "duration_ms", 0) or 0),
                            error_message=getattr(sr, "error", "") or "",
                        ))
            except Exception:
                _alert_steps, _alert_step_metrics, _alert_failed = [], [], ""
                _step_logs = []

            # Capture pipeline edges so the alert email can render a
            # real DAG (parallel branches + joins) instead of a flat
            # linear chain. Same {from,to} shape the notifier expects;
            # an empty list means the email falls back to the linear
            # layout. Wrapped in try/except so a malformed connections
            # list never blocks alert dispatch.
            _alert_connections: list[dict] = []
            try:
                for c in (getattr(wf, "connections", None) or []):
                    f = getattr(c, "from_step", "") or ""
                    t = getattr(c, "to_step", "") or ""
                    if f and t:
                        _alert_connections.append({"from": f, "to": t})
            except Exception:
                _alert_connections = []

            exe.duration_ms = duration_ms
            exe.completed_at = datetime.now(timezone.utc)
            # Attach the step logs so the Lineage tab + step-output
            # drawers in the Executions UI render properly for this run.
            try:
                exe.step_logs = _step_logs
                exe.steps_completed = sum(1 for sl in _step_logs if sl.status == "success")
                exe.steps_failed = sum(1 for sl in _step_logs if sl.status == "error")
                if not exe.steps_total or exe.steps_total == 0:
                    exe.steps_total = len(_step_logs)
            except Exception:
                pass
            if execution_store is not None:
                try:
                    execution_store.record(exe)
                except Exception as exc:
                    logger.warning("Could not persist final execution record: %s", exc)

            logger.info(f"Schedule {schedule_id}: pipeline {workflow_id} completed with status={status} in {duration_ms}ms")

        except Exception as e:
            status = "error"
            error_msg = str(e)
            logger.exception(f"Schedule {schedule_id}: pipeline execution failed: {e}")

        finally:
            # The pool slot is released by `run_through_pool` when the
            # run returns. The old release here passed
            # `_pool_worker or _pool._workers[0]`, so a schedule that
            # never got a slot freed slot 0 — belonging to a DIFFERENT
            # running job, which then showed as idle while still working.

            # Make sure the persisted execution record reflects the
            # final outcome — if the run errored before reaching the
            # success branch above, the up-front "running" row would
            # otherwise sit there forever.
            if exe is not None and execution_store is not None and exe.status == "running":
                try:
                    exe.status = status if status in ("success", "error", "cancelled") else "error"
                    exe.error_message = error_msg or exe.error_message
                    exe.duration_ms = int((time.time() - start) * 1000)
                    exe.completed_at = datetime.now(timezone.utc)
                    execution_store.record(exe)
                except Exception:
                    pass

            # Update schedule last_run_at — best-effort.
            if schedule_store is not None:
                try:
                    schedule_store.record_run(
                        schedule_id, status,
                        fired_at=datetime.fromtimestamp(start, timezone.utc),
                    )
                except Exception as exc:
                    logger.warning("Could not record schedule run: %s", exc)

            # Remove from running jobs — ALWAYS, even if every other
            # cleanup step above failed. This is the bug that left the
            # 2-minute schedule permanently silenced after one bad run.
            self._running_jobs.pop(schedule_id, None)
            self._run_threads.pop(schedule_id, None)

            # In-app notification for the scheduled run outcome —
            # writes to every user in the schedule's workspace so the
            # bell badge + Notifications page reflect scheduled runs
            # too, not just manual ones.
            try:
                from revoluzen_flow.notifications.run_events import emit_run_notification
                emit_run_notification(
                    notification_store=app_state.get("notification_store"),
                    user_store=app_state.get("user_store"),
                    workflow_id=workflow_id,
                    # The pipeline's name, read from it above, as the run's
                    # record has it: the bell and the incident opened from
                    # this used to read "Partner Price List schedule". The
                    # schedule's name only when the pipeline was not found.
                    workflow_name=(getattr(exe, "workflow_name", "") if exe is not None else "") or schedule_name or workflow_id,
                    execution_id=(exe.id if exe is not None else ""),
                    status=status,
                    workspace_id=workspace_id,
                    triggered_by=triggered_by,
                    error_message=error_msg,
                    failed_step=locals().get("_alert_failed") or "",
                    duration_ms=int((time.time() - start) * 1000),
                )
            except Exception as exc:
                logger.warning("In-app run notification failed (non-fatal): %s", exc)

            # Trigger alerts — also scoped to the schedule's workspace
            # so a rule attached to a same-id workflow in another
            # tenant is never fired on our behalf. Wrapped: a broken
            # alert rule must not bubble back into the scheduler loop.
            if alert_store is not None:
                try:
                    self._trigger_alerts(
                        alert_store=alert_store,
                        workflow_id=workflow_id,
                        status=status,
                        duration_ms=int((time.time() - start) * 1000),
                        error_message=error_msg,
                        schedule_name=schedule_name,
                        workspace_id=workspace_id,
                        workflow_steps=locals().get("_alert_steps") or [],
                        workflow_connections=locals().get("_alert_connections") or [],
                        first_failed_step=locals().get("_alert_failed") or "",
                        step_metrics=locals().get("_alert_step_metrics") or [],
                        # Run-details metadata for the alert email's
                        # Run Details section: lets operators jump to
                        # the right execution from a 16-line subject
                        # without having to grep workspaces.
                        execution_id=(exe.id if exe is not None else ""),
                        triggered_by=triggered_by,
                        started_at=(exe.started_at.isoformat() if exe is not None and exe.started_at else ""),
                        completed_at=(datetime.now(timezone.utc).isoformat()),
                        project_id=(getattr(exe, "project_id", "") if exe is not None else ""),
                    )
                except Exception as exc:
                    logger.warning("Alert trigger failed (non-fatal): %s", exc)

            if exe is not None and execution_store is not None:
                try:
                    exe.completed_at = datetime.now(timezone.utc)
                    execution_store.record(exe)
                except Exception as exc:
                    logger.warning("Could not persist final execution timing: %s", exc)

            self._after_run(
                schedule_id=schedule_id,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                workflow_name=(getattr(exe, "workflow_name", "") if exe is not None else "") or schedule_name,
                status=status,
                execution_id=(exe.id if exe is not None else ""),
                triggered_by=triggered_by,
                run_metadata=run_metadata,
                on_finish=on_finish,
            )

    def _after_run(
        self,
        *,
        schedule_id: str,
        workflow_id: str,
        workspace_id: str,
        workflow_name: str,
        status: str,
        execution_id: str,
        triggered_by: str,
        run_metadata: dict | None,
        on_finish: Callable[[str, str], None] | None,
    ) -> None:
        """What happens once a run this scheduler started has ended: the
        trigger that started it hears how it went, pipelines set to run after
        it are told, and a file trigger moves on to its next waiting file."""
        if on_finish is not None:
            try:
                on_finish(status, execution_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Trigger %s could not note how its run ended: %s", schedule_id, exc)
        try:
            from revoluzen_flow.main import app_state
            from revoluzen_flow.triggers.chain import on_run_finished

            on_run_finished(
                app_state,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
                workflow_name=workflow_name,
                status=status,
                execution_id=execution_id,
                triggered_by=triggered_by,
                run_metadata=run_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("After-pipeline triggers were not told run %s ended: %s", execution_id, exc)
        if triggered_by == ScheduleType.FILE_ARRIVAL.value:
            self._recheck_file_trigger(schedule_id)
        elif triggered_by == ScheduleType.AFTER_PIPELINE.value:
            # A pipeline that finished while this one was running is waiting.
            self.wake_after_pipeline_triggers()

    def _trigger_alerts(
        self,
        alert_store: Any,
        workflow_id: str,
        status: str,
        duration_ms: int,
        error_message: str,
        schedule_name: str,
        workspace_id: str = "default",
        workflow_steps: list[dict] | None = None,
        workflow_connections: list[dict] | None = None,
        first_failed_step: str = "",
        step_metrics: list[dict] | None = None,
        execution_id: str = "",
        triggered_by: str = "schedule",
        started_at: str = "",
        completed_at: str = "",
        project_id: str = "",
    ) -> None:
        """Check alert rules and send notifications.

        Only fires rules that belong to ``workspace_id`` — a cross-
        tenant rule collision on ``workflow_id`` is silently ignored
        rather than triggering a foreign tenant's notification chain.
        """
        from revoluzen_flow.alerts.models import AlertCondition

        # Prefer the tenant-scoped lookup if the store supports it.
        # Older alert stores only have the unscoped method; fall back
        # and filter in Python so this still works during a rolling
        # upgrade.
        try:
            rules = alert_store.list_rules_by_workflow(
                workflow_id, workspace_id=workspace_id,
            )
        except TypeError:
            rules = [
                r for r in alert_store.list_rules_by_workflow(workflow_id)
                if (r.get("workspace_id") or "default") == workspace_id
            ]
        if not rules:
            return

        for rule_dict in rules:
            # Same scoping contract as the list call above — fall back
            # to the unscoped API if an older store is in use.
            try:
                rule = alert_store.get_rule(
                    rule_dict["id"], workspace_id=workspace_id,
                )
            except TypeError:
                rule = alert_store.get_rule(rule_dict["id"])
            if not rule or not rule.enabled:
                continue

            conditions = rule.conditions or [rule.condition]
            should_fire = False
            triggered_condition = ""

            for cond in conditions:
                if cond == AlertCondition.ON_FAILURE and status == "error":
                    should_fire = True
                    triggered_condition = "on_failure"
                elif cond == AlertCondition.ON_SUCCESS and status == "success":
                    should_fire = True
                    triggered_condition = "on_success"
                elif cond == AlertCondition.ON_ANY:
                    should_fire = True
                    triggered_condition = "on_any"
                elif cond == AlertCondition.ON_LONG_RUNNING:
                    threshold_ms = rule.long_running_threshold_minutes * 60 * 1000
                    if duration_ms > threshold_ms:
                        should_fire = True
                        triggered_condition = "on_long_running"

                if should_fire:
                    break

            if should_fire:
                try:
                    # Enrich the alert payload with the same AI
                    # diagnosis that the manual-run path emits, so
                    # scheduled-run alerts arrive with the structured
                    # PIPELINE / ORIGINAL FAILURE / AI DIAGNOSIS body
                    # instead of a bare error string.
                    ai_diagnosis = ""
                    ai_suggestion = ""
                    ai_severity = ""
                    ai_powered = False
                    if status == "error" and error_message:
                        try:
                            # LLM-first analysis with rule-based fallback.
                            # Hard-capped at 12s so the scheduler thread
                            # can't be wedged by a slow provider.
                            from revoluzen_flow.ai.embedded import analyze_error as _diag
                            d = _diag(
                                error_message=error_message[:500],
                                node_type="",
                                workflow_steps=workflow_steps or [],
                                failed_step=first_failed_step or "",
                                workspace_id=workspace_id,
                            ) or {}
                            ai_diagnosis = (d.get("diagnosis") or "")[:500]
                            ai_suggestion = (d.get("suggestion") or "")[:500]
                            ai_severity = d.get("severity") or ""
                            ai_powered = bool(d.get("ai_powered"))
                        except Exception:
                            pass

                    import os as _os
                    app_root = _os.environ.get("REVOLUZEN_FLOW_APP_URL", "http://localhost:5174")
                    deep_link = f"{app_root}/#executions?workflow={workflow_id}"
                    # Per-resource deep-links so the email's action row
                    # can route operators directly to the right page
                    # rather than dumping them on a list view.
                    workflow_link = f"{app_root}/#editor?workflow={workflow_id}"
                    execution_link = (
                        f"{app_root}/#executions/{execution_id}"
                        if execution_id else deep_link
                    )

                    from revoluzen_flow.alerts.notifier import NotificationService
                    notifier = NotificationService()
                    log = notifier.send(rule, {
                        "workflow_name": schedule_name or workflow_id,
                        "workflow_id": workflow_id,
                        "status": status,
                        "duration_ms": duration_ms,
                        "duration_s": round(duration_ms / 1000.0, 2),
                        "error_message": error_message,
                        "triggered_condition": triggered_condition,
                        "ai_diagnosis": ai_diagnosis,
                        "ai_suggestion": ai_suggestion,
                        "ai_severity": ai_severity,
                        "ai_powered": ai_powered,
                        "deep_link": deep_link,
                        "workflow_link": workflow_link,
                        "execution_link": execution_link,
                        "workflow_steps": workflow_steps or [],
                        # Edges between steps — drives the layered DAG
                        # lineage renderer in the alert email so the
                        # message mirrors the in-app Execution Summary
                        # view instead of flattening parallel branches
                        # into a single misleading chain.
                        "workflow_connections": workflow_connections or [],
                        "first_failed_step": first_failed_step or "",
                        # Complex-pipeline enrichment — populated when
                        # the executor surfaces per-step duration + row
                        # counts. The alert template renders the
                        # Execution Timeline section when present;
                        # absent for ad-hoc / first-time runs and the
                        # email gracefully omits that section.
                        "step_metrics": step_metrics or [],
                        # Run-details metadata — drives the new Run
                        # Details key/value table in the alert email.
                        "execution_id": execution_id,
                        "workflow_id": workflow_id,
                        "schedule_name": schedule_name,
                        "triggered_by": triggered_by,
                        "started_at": started_at,
                        "completed_at": completed_at,
                        "workspace_id": workspace_id,
                        "project_id": project_id,
                        "environment": _os.environ.get("REVOLUZEN_FLOW_ENV", ""),
                    })
                    alert_store.add_log(log)
                except Exception as e:
                    logger.error(f"Failed to send alert for rule {rule.id}: {e}")
