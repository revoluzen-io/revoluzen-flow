"""Unit tests for PipelineScheduler — schedule evaluation, cron parsing."""

import pytest
import threading
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from revoluzen_flow.reports.cron_human import cron_to_human
from revoluzen_flow.scheduling.scheduler import PipelineScheduler
from revoluzen_flow.scheduling.models import ScheduleType


class TestSchedulerCronField:
    """Test _cron_field_matches static method."""

    def test_wildcard(self):
        assert PipelineScheduler._cron_field_matches(5, "*", 0, 59) is True

    def test_exact_match(self):
        assert PipelineScheduler._cron_field_matches(5, "5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(5, "10", 0, 59) is False

    def test_step(self):
        assert PipelineScheduler._cron_field_matches(0, "*/5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(15, "*/5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(7, "*/5", 0, 59) is False

    def test_comma_list(self):
        assert PipelineScheduler._cron_field_matches(3, "1,3,5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(4, "1,3,5", 0, 59) is False

    def test_range(self):
        assert PipelineScheduler._cron_field_matches(3, "1-5", 0, 59) is True
        assert PipelineScheduler._cron_field_matches(7, "1-5", 0, 59) is False

    def test_invalid_step(self):
        assert PipelineScheduler._cron_field_matches(5, "*/abc", 0, 59) is False

    def test_invalid_exact(self):
        assert PipelineScheduler._cron_field_matches(5, "abc", 0, 59) is False


class TestSchedulerIsDue:
    @pytest.fixture
    def scheduler(self):
        return PipelineScheduler(check_interval_seconds=30)

    def test_interval_never_run(self, scheduler):
        sched = {
            "schedule_type": "interval",
            "interval_minutes": 60,
            "last_run_at": None,
            "enabled": True,
        }
        now = datetime.now(timezone.utc)
        assert scheduler._is_due(sched, now) is True

    def test_interval_not_due(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {
            "schedule_type": "interval",
            "interval_minutes": 60,
            "last_run_at": (now - timedelta(minutes=30)).isoformat(),
            "enabled": True,
        }
        assert scheduler._is_due(sched, now) is False

    def test_interval_is_due(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {
            "schedule_type": "interval",
            "interval_minutes": 60,
            "last_run_at": (now - timedelta(minutes=65)).isoformat(),
            "enabled": True,
        }
        assert scheduler._is_due(sched, now) is True

    def test_daily_due(self, scheduler):
        now = datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is True

    def test_daily_wrong_hour(self, scheduler):
        now = datetime(2026, 4, 2, 14, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False

    def test_daily_already_ran_today(self, scheduler):
        now = datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "last_run_at": datetime(2026, 4, 2, 9, 0, 0, tzinfo=timezone.utc),
        }
        assert scheduler._is_due(sched, now) is False

    def test_weekly_correct_day(self, scheduler):
        # 2026-04-02 is a Thursday (weekday=3)
        now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "weekly",
            "weekly_days": [3],
            "weekly_time": "10:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is True

    def test_weekly_wrong_day(self, scheduler):
        now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "weekly",
            "weekly_days": [0, 1],  # Mon, Tue only
            "weekly_time": "10:00",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False

    @pytest.mark.parametrize("kind", ["file_arrival", "after_pipeline", "event"])
    def test_event_triggers_are_never_due_on_the_clock(self, scheduler, kind):
        """They start when their event happens (triggers/), never by the clock.
        A leftover "event" row is not due either."""
        now = datetime.now(timezone.utc)
        sched = {"schedule_type": kind, "last_run_at": None}
        assert scheduler._is_due(sched, now) is False

    def test_cron_matching(self, scheduler):
        now = datetime(2026, 4, 2, 10, 30, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "cron",
            "cron_expression": "30 10 * * *",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is True

    def test_cron_not_matching(self, scheduler):
        now = datetime(2026, 4, 2, 10, 15, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "cron",
            "cron_expression": "30 10 * * *",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False

    def test_cron_empty_expression(self, scheduler):
        now = datetime.now(timezone.utc)
        sched = {
            "schedule_type": "cron",
            "cron_expression": "",
            "last_run_at": None,
        }
        assert scheduler._is_due(sched, now) is False


class TestCronWeekdays:
    """Day of week as in standard cron, and as every screen writes and shows
    it: 0 and 7 are Sunday, 1 is Monday. It used to count from Monday, so a
    schedule set for Monday ran on Tuesday."""

    # Monday 2 Nov 2026 to Sunday 8 Nov 2026, at 09:00.
    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    WEEK = {name: datetime(2026, 11, 2 + i, 9, 0, tzinfo=timezone.utc) for i, name in enumerate(DAYS)}

    @pytest.fixture
    def scheduler(self):
        return PipelineScheduler(check_interval_seconds=30)

    def _due_on(self, scheduler, field):
        return [name for name, at in self.WEEK.items() if scheduler._check_cron(at, None, f"0 9 * * {field}")]

    @pytest.mark.parametrize("number, day", [
        ("0", "Sunday"), ("1", "Monday"), ("2", "Tuesday"), ("3", "Wednesday"),
        ("4", "Thursday"), ("5", "Friday"), ("6", "Saturday"), ("7", "Sunday"),
    ])
    def test_each_number_means_its_day(self, scheduler, number, day):
        assert self._due_on(scheduler, number) == [day]

    @pytest.mark.parametrize("number", list("01234567"))
    def test_it_runs_on_the_day_it_says(self, scheduler, number):
        """The words the inventory and scheduled reports print."""
        due = self._due_on(scheduler, number)
        assert cron_to_human(f"0 9 * * {number}") == f"Every {due[0]} at 09:00"

    @pytest.mark.parametrize("field, days", [
        ("1-5", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]),
        ("0,6", ["Saturday", "Sunday"]),
        ("6,7", ["Saturday", "Sunday"]),
        ("5-7", ["Friday", "Saturday", "Sunday"]),
        ("1,3,5", ["Monday", "Wednesday", "Friday"]),
        ("*", DAYS),
    ])
    def test_ranges_and_lists(self, scheduler, field, days):
        assert self._due_on(scheduler, field) == days

    def test_the_weekday_is_the_one_in_the_schedules_own_time_zone(self, scheduler):
        # 20:00 UTC on Sunday is 01:30 on Monday in Kolkata.
        sunday_evening = datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc)
        sched = {
            "schedule_type": "cron", "cron_expression": "30 1 * * 1",
            "timezone": "Asia/Kolkata", "last_run_at": None,
        }
        assert scheduler._is_due(sched, sunday_evening) is True
        assert scheduler._is_due({**sched, "cron_expression": "30 1 * * 0"}, sunday_evening) is False


class TestCronOncePerMinute:
    """Two ticks land in most minutes; only the first may fire."""

    @pytest.fixture
    def scheduler(self):
        return PipelineScheduler(check_interval_seconds=30)

    FIRST = datetime(2026, 11, 10, 9, 0, 5, tzinfo=timezone.utc)

    def test_the_next_tick_in_the_same_minute_does_not_fire(self, scheduler):
        # A tick is 30 seconds and a little after the last, so a check on
        # "fewer than 30 seconds ago" let this through.
        next_tick = self.FIRST + timedelta(seconds=30, milliseconds=5)
        assert scheduler._check_cron(next_tick, self.FIRST, "0 9 * * *") is False

    def test_the_next_matching_minute_fires(self, scheduler):
        assert scheduler._check_cron(self.FIRST + timedelta(minutes=1), self.FIRST, "* * * * *") is True

    def test_the_same_time_tomorrow_fires(self, scheduler):
        assert scheduler._check_cron(self.FIRST + timedelta(days=1), self.FIRST, "0 9 * * *") is True

    def test_a_stored_time_with_no_zone_is_utc(self, scheduler):
        naive = self.FIRST.replace(tzinfo=None)
        assert scheduler._check_cron(self.FIRST + timedelta(seconds=30), naive, "0 9 * * *") is False

    def test_a_run_finishing_in_the_next_matching_minute_keeps_that_minutes_turn(self, scheduler):
        sched = {
            "schedule_type": "cron", "cron_expression": "* * * * *",
            "last_fired_at": "2026-11-10T12:00:25+00:00",
            "last_run_at": "2026-11-10T12:01:05+00:00",  # 40 seconds later
        }
        tick = datetime(2026, 11, 10, 12, 1, 25, tzinfo=timezone.utc)
        assert scheduler._is_due(sched, tick) is True

    def test_a_run_started_this_minute_is_not_repeated(self, scheduler):
        sched = {
            "schedule_type": "cron", "cron_expression": "0 9 * * *",
            "last_fired_at": self.FIRST.isoformat(),
            "last_run_at": (self.FIRST + timedelta(seconds=2)).isoformat(),
        }
        assert scheduler._is_due(sched, self.FIRST + timedelta(seconds=30)) is False

    def test_rows_from_before_the_start_time_was_kept_use_the_finish_time(self, scheduler):
        sched = {
            "schedule_type": "cron", "cron_expression": "0 9 * * *",
            "last_run_at": (self.FIRST + timedelta(seconds=2)).isoformat(),
        }
        assert scheduler._is_due(sched, self.FIRST + timedelta(seconds=30)) is False


class TestDeploymentsFireOnce:
    @pytest.fixture
    def deployment(self, monkeypatch):
        from revoluzen_flow.api import deployments as module

        d = SimpleNamespace(
            id="d1", workflow_id="wf", workspace_id="default", name="Nightly",
            parameters={}, schedule={"cron": "0 9 * * *"}, last_run_at=None,
        )
        monkeypatch.setattr(module, "list_enabled_with_schedule", lambda: [d])
        monkeypatch.setattr(module, "mark_fired", lambda _id, when: setattr(d, "last_run_at", when))
        return d

    FIRST = datetime(2026, 11, 10, 9, 0, 5, tzinfo=timezone.utc)

    def test_two_ticks_in_a_minute_run_it_once(self, deployment):
        scheduler = PipelineScheduler(check_interval_seconds=30)
        fired = []

        def started(key, _sched):
            fired.append(key)
            return True

        scheduler._execute_schedule = started
        scheduler._check_deployments(self.FIRST, None)
        scheduler._check_deployments(self.FIRST + timedelta(seconds=30, milliseconds=5), None)
        assert fired == ["dep:d1"]
        assert deployment.last_run_at == self.FIRST

    def test_turned_away_at_capacity_it_tries_again_on_the_next_tick(self, deployment):
        scheduler = PipelineScheduler(check_interval_seconds=30)
        answers = [False, True]
        tries = []

        def maybe(key, _sched):
            tries.append(key)
            return answers.pop(0)

        scheduler._execute_schedule = maybe
        scheduler._check_deployments(self.FIRST, None)
        assert deployment.last_run_at is None
        next_tick = self.FIRST + timedelta(seconds=30)
        scheduler._check_deployments(next_tick, None)
        assert tries == ["dep:d1", "dep:d1"]
        assert deployment.last_run_at == next_tick


class TestExecuteScheduleSaysIfItStarted:
    SCHED = {"workflow_id": "wf", "workspace_id": "default", "name": "n"}

    def test_started(self, monkeypatch):
        from revoluzen_flow.engine import execution_manager as module

        manager = SimpleNamespace(spawn_thread=lambda **kwargs: None)
        monkeypatch.setattr(module.ExecutionManager, "get", classmethod(lambda cls: manager))
        scheduler = PipelineScheduler()
        assert scheduler._execute_schedule("s1", self.SCHED) is True
        assert "s1" in scheduler._running_jobs

    def test_turned_away(self, monkeypatch):
        from revoluzen_flow.engine import execution_manager as module

        def refuse(**kwargs):
            raise ValueError("at capacity")

        manager = SimpleNamespace(spawn_thread=refuse)
        monkeypatch.setattr(module.ExecutionManager, "get", classmethod(lambda cls: manager))
        scheduler = PipelineScheduler()
        assert scheduler._execute_schedule("s1", self.SCHED) is False
        assert "s1" not in scheduler._running_jobs

    def test_nothing_to_run(self):
        assert PipelineScheduler()._execute_schedule("s1", {}) is False


def test_a_finished_run_records_when_it_started(monkeypatch):
    """So the next tick compares with the start, not the finish."""
    from revoluzen_flow import main

    calls = []
    store = SimpleNamespace(record_run=lambda sid, status, fired_at=None: calls.append((sid, status, fired_at)))
    for key, value in {
        "store": None, "schedule_store": store, "execution_store": None,
        "alert_store": None, "notification_store": None, "user_store": None,
    }.items():
        monkeypatch.setitem(main.app_state, key, value)
    before = datetime.now(timezone.utc)
    scheduler = PipelineScheduler()
    scheduler._running_jobs["s1"] = before
    scheduler._run_pipeline("s1", "wf", "name")
    assert len(calls) == 1
    sid, status, fired_at = calls[0]
    assert (sid, status) == ("s1", "error")
    assert before - timedelta(seconds=1) <= fired_at <= datetime.now(timezone.utc)


class TestSchedulerStuckGuard:
    """Liveness-based overlap guard (2026-06-16). The watchdog must NOT release
    a run-lock just because a timer elapsed — that let short-interval schedules
    start an overlapping second run mid-flight. It releases only when the run
    thread is actually gone, or past a generous hard cap."""

    @pytest.fixture
    def scheduler(self):
        return PipelineScheduler(check_interval_seconds=30)

    def test_honest_long_run_keeps_lock(self, scheduler):
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        try:
            sid = "sched-long"
            # 30 min in — well past the 5-min grace — but the thread is alive.
            scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=30)
            scheduler._run_threads[sid] = t
            assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == []
        finally:
            stop.set()
            t.join(timeout=2)

    def test_dead_run_lock_is_released(self, scheduler):
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(timeout=2)  # thread has finished → dead
        sid = "sched-dead"
        scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=30)
        scheduler._run_threads[sid] = t
        assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == [sid]

    def test_within_grace_never_flagged(self, scheduler):
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(timeout=2)  # dead, but the lock is only 10s old
        sid = "sched-fresh"
        scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(seconds=10)
        scheduler._run_threads[sid] = t
        assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == []

    def test_hard_cap_releases_even_when_alive(self, scheduler):
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        try:
            scheduler.STUCK_JOB_HARD_CAP_S = 60  # shrink for the test
            sid = "sched-hung"
            scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=10)
            scheduler._run_threads[sid] = t
            assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == [sid]
        finally:
            stop.set()
            t.join(timeout=2)

    def test_missing_handle_evicted_after_grace(self, scheduler):
        sid = "sched-nohandle"
        scheduler._running_jobs[sid] = datetime.now(timezone.utc) - timedelta(minutes=30)
        # no _run_threads entry → can't prove liveness → evict (leaked lock)
        assert scheduler._stuck_schedule_ids(datetime.now(timezone.utc)) == [sid]


class TestSchedulerStartStop:
    """Scheduler start/stop now routes through ExecutionManager
    (Apr 22 2026 migration — PR5 step 9 follow-up). Tests initialize
    a real manager + worker pool so the spawn_scheduled call path is
    exercised end-to-end."""

    @pytest.fixture
    def manager(self):
        from revoluzen_flow.engine.execution_manager import ExecutionManager
        from revoluzen_flow.engine.worker_pool import WorkerPool
        ExecutionManager._reset_for_tests()
        pool = WorkerPool(max_workers=1)

        # Governor that never rejects — we're testing scheduler
        # lifecycle, not admission control.
        from revoluzen_flow.engine.global_governor import GlobalResourceGovernor
        gov = GlobalResourceGovernor(
            mem_defer_pct=99.5, mem_slow_pct=99.7, mem_red_pct=99.9,
            cpu_defer_pct=99.5, sample_cache_s=0,
        )
        mgr = ExecutionManager.initialize(
            worker_pool=pool, governor=gov, register_atexit=False,
        )
        pool.start()  # after manager init so the pool's watchdog registers via spawn_thread
        try:
            yield mgr
        finally:
            mgr.shutdown(timeout_s=2)
            ExecutionManager._reset_for_tests()

    def test_start_stop(self, manager):
        scheduler = PipelineScheduler(check_interval_seconds=1)
        assert scheduler.is_running is False
        scheduler.start()
        assert scheduler.is_running is True
        scheduler.stop()
        assert scheduler.is_running is False

    def test_active_jobs_empty(self):
        scheduler = PipelineScheduler()
        assert scheduler.active_jobs == {}

    def test_double_start_no_crash(self, manager):
        scheduler = PipelineScheduler(check_interval_seconds=1)
        scheduler.start()
        scheduler.start()  # Should log warning, not crash
        scheduler.stop()

    def test_scheduler_registered_in_manager_stats(self, manager):
        """Post-migration verification: the scheduler shows up in
        `mgr.stats().by_kind["scheduled"]` while running. Confirms
        the spawn_scheduled call path works end-to-end."""
        scheduler = PipelineScheduler(check_interval_seconds=5)
        assert manager.stats().by_kind.get("scheduled", 0) == 0
        scheduler.start()
        try:
            assert manager.stats().by_kind["scheduled"] == 1
            # And the handle owner is "scheduler" (set in spawn_scheduled call).
            owners = [r.handle.owner for r in manager.inspect()]
            assert "scheduler" in owners
        finally:
            scheduler.stop()

    def test_manager_shutdown_stops_scheduler(self, manager):
        """If the operator shuts down the manager before calling
        scheduler.stop(), the scheduler's loop still exits (via the
        stop_event set by manager.shutdown's distributed cancel)."""
        scheduler = PipelineScheduler(check_interval_seconds=5)
        scheduler.start()
        handle = scheduler._handle
        assert handle is not None

        # Manager shutdown cancels every scheduled primitive.
        manager.shutdown(timeout_s=3)

        # After shutdown, the handle's registry entry is gone.
        assert not [r for r in manager.inspect() if r.handle.id == handle.id]
        # scheduler.stop() tolerates the stale handle.
        scheduler.stop()
        assert scheduler.is_running is False
