"""Durable scheduler lease, and the API-only instance flag.

What was broken
---------------
`PipelineScheduler` tracked in-flight work in process-local dicts, and the
scheduler started unconditionally on every instance. Those dicts stop one
process double-running a schedule; they say nothing about a second process.
So two instances behind a load balancer fired every schedule twice, silently,
with both runs looking legitimate — which is why Flow could not be
load-balanced at all.

Why these tests use a second `Database`
---------------------------------------
`Database.execute()` returns a cursor and does **not** commit, on thread-local
connections. A lease written without an explicit commit sits in an
uncommitted transaction on the writing thread's own connection: that process
reads its own write and believes it holds the lease, every other instance sees
nothing, and all of them schedule — the exact failure the lease exists to
prevent.

Reading back through the writing connection passes even against that bug. So
every cross-instance assertion here goes through a *separate* `Database` on
the same file, which is what another instance actually is. Same trap, same
remedy as `test_admin_settings_persistence.py`.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from revoluzen_flow.scheduling import lease as lease_mod
from revoluzen_flow.scheduling.lease import SchedulerLease
from revoluzen_flow.storage.database import Database


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "lease.db")


@pytest.fixture
def db(db_path):
    d = Database(db_path)
    yield d
    try:
        d.close()
    except Exception:
        pass


def other_instance(db_path: str) -> Database:
    """A second Database on the same file — what another instance really is."""
    return Database(db_path)


def as_holder(name: str, monkeypatch):
    """Pin the process holder id, so one test can play several instances."""
    monkeypatch.setattr(lease_mod, "_HOLDER_ID", name, raising=False)


# ── Basic acquisition ────────────────────────────────────────────────────────


def test_an_unheld_lease_is_acquired(db, monkeypatch):
    as_holder("instance-a", monkeypatch)
    assert SchedulerLease(db).acquire_or_renew() is True


def test_the_holder_can_renew(db, monkeypatch):
    as_holder("instance-a", monkeypatch)
    l = SchedulerLease(db)
    assert l.acquire_or_renew() is True
    assert l.acquire_or_renew() is True


def test_renewal_does_not_increment_the_term(db, monkeypatch):
    """Term tracks takeovers, not renewals — that is what makes it useful.

    If renewing bumped it, a holder could not distinguish "I kept the lease"
    from "I lost it and took it back", which is the one question term exists
    to answer.
    """
    as_holder("instance-a", monkeypatch)
    l = SchedulerLease(db)
    l.acquire_or_renew()
    first = l.held_term
    l.acquire_or_renew()
    l.acquire_or_renew()
    assert l.held_term == first


# ── Cross-instance exclusion — the whole point ───────────────────────────────


def test_a_second_instance_is_refused_while_the_lease_is_live(db, db_path, monkeypatch):
    """Two instances, one lease. This is the double-firing bug, pinned.

    The second instance reads through its own `Database`, so this also proves
    the first instance's write was committed — an uncommitted lease would be
    invisible here and the second instance would win too.
    """
    as_holder("instance-a", monkeypatch)
    assert SchedulerLease(db).acquire_or_renew() is True

    b_db = other_instance(db_path)
    try:
        as_holder("instance-b", monkeypatch)
        assert SchedulerLease(b_db).acquire_or_renew() is False, (
            "a second instance took the lease while it was live — every "
            "schedule would fire twice"
        )
    finally:
        b_db.close()


def test_the_lease_is_visible_from_another_connection(db, db_path, monkeypatch):
    """REGRESSION: the commit is load-bearing.

    `Database.execute()` does not commit. Without the explicit commit the row
    would be visible only on the writing thread's connection, so this read
    through a fresh `Database` is what proves it reached disk.
    """
    as_holder("instance-a", monkeypatch)
    SchedulerLease(db).acquire_or_renew()

    reader = other_instance(db_path)
    try:
        row = reader.execute(
            "SELECT holder FROM scheduler_lease WHERE id = 1"
        ).fetchone()
        holder = row[0] if not isinstance(row, dict) else row["holder"]
        assert holder == "instance-a", (
            "the lease was not committed — invisible to every other instance"
        )
    finally:
        reader.close()


def test_the_lease_survives_in_a_separate_thread(db, db_path, monkeypatch):
    """Connections are thread-local, so a thread is its own reader."""
    as_holder("instance-a", monkeypatch)
    SchedulerLease(db).acquire_or_renew()

    seen: list = []

    def read():
        d = other_instance(db_path)
        try:
            row = d.execute("SELECT holder FROM scheduler_lease WHERE id=1").fetchone()
            seen.append(row[0] if not isinstance(row, dict) else row["holder"])
        finally:
            d.close()

    t = threading.Thread(target=read)
    t.start()
    t.join(timeout=10)
    assert seen == ["instance-a"]


# ── Takeover ─────────────────────────────────────────────────────────────────


def test_an_expired_lease_is_taken_over_and_the_term_increments(db, db_path, monkeypatch):
    """A dead holder must not block scheduling forever."""
    as_holder("instance-a", monkeypatch)
    a = SchedulerLease(db)
    a.acquire_or_renew()
    term_a = a.held_term

    # Expire it in place, as the passage of time would.
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    db.execute("UPDATE scheduler_lease SET expires_at = ? WHERE id = 1", (past,))
    db.commit()

    b_db = other_instance(db_path)
    try:
        as_holder("instance-b", monkeypatch)
        b = SchedulerLease(b_db)
        assert b.acquire_or_renew() is True
        assert b.held_term == term_a + 1, "a takeover must bump the term"
    finally:
        b_db.close()


def test_release_frees_the_lease_immediately(db, db_path, monkeypatch):
    """Clean shutdown should not cost a standby a full TTL of delay."""
    as_holder("instance-a", monkeypatch)
    a = SchedulerLease(db)
    a.acquire_or_renew()
    a.release()

    b_db = other_instance(db_path)
    try:
        as_holder("instance-b", monkeypatch)
        assert SchedulerLease(b_db).acquire_or_renew() is True
    finally:
        b_db.close()


def test_release_by_a_non_holder_does_nothing(db, db_path, monkeypatch):
    """B must not be able to release A's lease."""
    as_holder("instance-a", monkeypatch)
    SchedulerLease(db).acquire_or_renew()

    b_db = other_instance(db_path)
    try:
        as_holder("instance-b", monkeypatch)
        SchedulerLease(b_db).release()
        as_holder("instance-c", monkeypatch)
        assert SchedulerLease(b_db).acquire_or_renew() is False, (
            "a non-holder released someone else's lease"
        )
    finally:
        b_db.close()


# ── Fail closed ──────────────────────────────────────────────────────────────


def test_a_broken_database_means_not_held(monkeypatch):
    """Fail closed: unable to tell must mean do not schedule.

    Missing a tick is recoverable and already alerted on. A duplicate run can
    double-charge a customer or duplicate rows in their warehouse.
    """
    as_holder("instance-a", monkeypatch)

    class Broken:
        def execute(self, *a, **kw):
            raise RuntimeError("database is locked")

        def commit(self):
            raise RuntimeError("database is locked")

    assert SchedulerLease(Broken()).acquire_or_renew() is False


def test_holder_id_is_stable_within_a_process(monkeypatch):
    monkeypatch.setattr(lease_mod, "_HOLDER_ID", None, raising=False)
    assert lease_mod.holder_id() == lease_mod.holder_id()


def test_holder_id_distinguishes_a_recycled_pid(monkeypatch):
    """host:pid alone would let a recycled pid inherit a live lease."""
    monkeypatch.setattr(lease_mod, "_HOLDER_ID", None, raising=False)
    first = lease_mod.holder_id()
    monkeypatch.setattr(lease_mod, "_HOLDER_ID", None, raising=False)
    assert lease_mod.holder_id() != first


# ── The scheduler tick ───────────────────────────────────────────────────────


def test_the_tick_is_skipped_without_the_lease(monkeypatch):
    """No lease, no scheduling — the behaviour that makes N instances safe."""
    from revoluzen_flow.scheduling.scheduler import PipelineScheduler

    s = PipelineScheduler(check_interval_seconds=30)
    ran: list[bool] = []
    monkeypatch.setattr(s, "_check_schedules", lambda: ran.append(True))

    class NotHeld:
        def acquire_or_renew(self):
            return False

        def current_holder(self):
            return {"holder": "instance-b"}

    monkeypatch.setattr(s, "_lease", lambda: NotHeld())
    s._check_schedules_safe()
    assert ran == [], "scheduled while another instance held the lease"


def test_the_tick_runs_when_the_lease_is_held(monkeypatch):
    from revoluzen_flow.scheduling.scheduler import PipelineScheduler

    s = PipelineScheduler(check_interval_seconds=30)
    ran: list[bool] = []
    monkeypatch.setattr(s, "_check_schedules", lambda: ran.append(True))

    class Held:
        def acquire_or_renew(self):
            return True

        def current_holder(self):
            return None

    monkeypatch.setattr(s, "_lease", lambda: Held())
    s._check_schedules_safe()
    assert ran == [True]


def test_the_tick_is_skipped_when_no_database_is_available(monkeypatch):
    """Before the db exists, or if it is gone, do not schedule."""
    from revoluzen_flow.scheduling.scheduler import PipelineScheduler

    s = PipelineScheduler(check_interval_seconds=30)
    ran: list[bool] = []
    monkeypatch.setattr(s, "_check_schedules", lambda: ran.append(True))
    monkeypatch.setattr(s, "_lease", lambda: None)

    s._check_schedules_safe()
    assert ran == []


def test_ttl_is_comfortably_above_the_tick_interval(monkeypatch):
    """A TTL near the interval would hand the lease away on one slow tick."""
    from revoluzen_flow.scheduling.scheduler import PipelineScheduler

    for interval in (30, 60, 120):
        s = PipelineScheduler(check_interval_seconds=interval)
        s._lease_obj = None
        monkeypatch.setattr(
            "revoluzen_flow.main.app_state", {"db": object()}, raising=False
        )
        built = s._lease()
        assert built is not None
        assert built._ttl >= interval * 3, (
            f"TTL {built._ttl}s leaves too little room at a {interval}s tick"
        )


# ── The API-only flag ────────────────────────────────────────────────────────


def test_the_scheduler_flag_exists_and_defaults_on():
    """A single-node install must be unaffected by the flag's existence."""
    from revoluzen_flow import feature_flags

    assert "scheduler" in feature_flags._FLAG_KEYS
    assert feature_flags.is_enabled("scheduler") is True


@pytest.mark.parametrize("value,expected", [("0", False), ("1", True),
                                            ("false", False), ("on", True)])
def test_the_flag_reads_the_env_var(monkeypatch, value, expected):
    """`REVOLUZEN_FLOW_ENABLE_SCHEDULER=0` is how a node is made API-only."""
    from revoluzen_flow import feature_flags

    monkeypatch.setenv("REVOLUZEN_FLOW_ENABLE_SCHEDULER", value)
    assert feature_flags._read_env("scheduler") is expected
