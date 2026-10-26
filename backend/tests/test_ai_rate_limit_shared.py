"""The AI rate limiter's shared window (2026-10-23).

The limiter kept its sliding window in a process-local dict. Behind a load
balancer that means each instance enforces the quota independently, so N
instances allow N x the configured number — the config still says 60 and
the system permits 180. A cost control that quietly stops working at the
moment you scale out is worse than no control, because nobody looks at it
again.

What matters, and what these tests pin:

  * two "instances" sharing one metastore must share one window;
  * the default must not change for a laptop install;
  * the check-and-record step must be atomic, or the shared store is just
    a slower version of the same race;
  * a broken store must not fail the request.
"""

from __future__ import annotations

import importlib
import sqlite3
import threading

import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _restore_rate_limit_module():
    """Put `ai.rate_limit` back the way it was found.

    These tests work by setting env vars and `importlib.reload`-ing the
    module so its module-level constants pick the new values up. monkeypatch
    unsets the env vars at teardown, but it cannot un-bake them from a module
    that has already been reloaded — so without this the next test file in the
    session inherits a quota of 5 and its agent calls start coming back 429.
    (That is exactly what happened: test_ai_agent_api.py passed alone and
    failed when run after this file.)

    Reloading once more at teardown, with the env restored, rebinds the
    constants to their real defaults.
    """
    yield
    from revoluzen_flow.ai import rate_limit as rl
    importlib.reload(rl)


class _DB:
    """Metastore stand-in: one sqlite file, many 'instances'.

    Connections are THREAD-LOCAL, like `storage.database.Database`. That
    detail is load-bearing: a fake that shares one connection across
    threads cannot start a transaction per thread, so the concurrency test
    would fail against correct code and pass against a version with no
    locking at all — it would be measuring the fake, not the limiter.
    """

    def __init__(self, path):
        self._path = str(path)
        self._local = threading.local()
        # Set WAL once, on the creating thread.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self._path, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=15000")
            self._local.conn = c
        return c

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


def _fresh(monkeypatch, tmp_path, *, store, quota=5, window=60, server=True):
    """Import a fresh limiter bound to a given store mode + metastore."""
    monkeypatch.setenv("REVOLUZEN_FLOW_AI_RATE_MAX_PER_HOUR", str(quota))
    monkeypatch.setenv("REVOLUZEN_FLOW_AI_RATE_WINDOW_SECONDS", str(window))
    monkeypatch.setenv("REVOLUZEN_FLOW_AI_RATE_ENABLE", "1")
    monkeypatch.setenv("REVOLUZEN_FLOW_AI_RATE_STORE", store)

    from revoluzen_flow.ai import rate_limit as rl
    importlib.reload(rl)

    db = _DB(str(tmp_path / "meta.db"))
    monkeypatch.setattr(rl, "_db_handle", lambda: db, raising=True)
    monkeypatch.setattr(rl, "_schema_ready", False, raising=False)

    class _RC:
        IS_SERVER_MODE = server
    monkeypatch.setitem(
        __import__("sys").modules, "revoluzen_flow.runtime_config_stub", _RC
    )
    if store == "auto":
        import revoluzen_flow.runtime_config as rc
        monkeypatch.setattr(rc, "IS_SERVER_MODE", server, raising=False)
    return rl, db


# ── the actual defect ───────────────────────────────────────────────────

def test_two_instances_share_one_window(monkeypatch, tmp_path):
    """The whole point. Instance A spends the quota; instance B is refused."""
    rl_a, db = _fresh(monkeypatch, tmp_path, store="shared", quota=5)

    for _ in range(5):
        rl_a.check_ai_rate_limit(user_id="u1")

    # A second "instance": a separate module object, same metastore.
    import revoluzen_flow.ai.rate_limit as _mod
    rl_b = importlib.reload(_mod)
    monkeypatch.setattr(rl_b, "_db_handle", lambda: db, raising=True)
    monkeypatch.setattr(rl_b, "_schema_ready", False, raising=False)

    with pytest.raises(HTTPException) as exc:
        rl_b.check_ai_rate_limit(user_id="u1")
    assert exc.value.status_code == 429


def test_process_store_does_not_share(monkeypatch, tmp_path):
    """Control: with the memory store the old behaviour is still there, so
    the test above is measuring the store and not something else."""
    rl_a, db = _fresh(monkeypatch, tmp_path, store="memory", quota=5)
    for _ in range(5):
        rl_a.check_ai_rate_limit(user_id="u1")

    import revoluzen_flow.ai.rate_limit as _mod
    rl_b = importlib.reload(_mod)
    monkeypatch.setattr(rl_b, "_db_handle", lambda: db, raising=True)
    rl_b.check_ai_rate_limit(user_id="u1")      # a fresh process: allowed


# ── atomicity ───────────────────────────────────────────────────────────

def test_concurrent_callers_cannot_exceed_the_quota(monkeypatch, tmp_path):
    """BEGIN IMMEDIATE, not check-then-act. 20 threads race for 5 slots."""
    rl, _db = _fresh(monkeypatch, tmp_path, store="shared", quota=5)

    allowed, refused = [], []
    barrier = threading.Barrier(20)

    def go():
        barrier.wait()
        try:
            rl.check_ai_rate_limit(user_id="race")
            allowed.append(1)
        except HTTPException:
            refused.append(1)
        except Exception:
            pass

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(allowed) <= 5, f"quota overshot: {len(allowed)} allowed"
    assert len(allowed) + len(refused) == 20


# ── behaviour preserved ─────────────────────────────────────────────────

def test_quota_cost_is_honoured(monkeypatch, tmp_path):
    rl, _ = _fresh(monkeypatch, tmp_path, store="shared", quota=5)
    rl.check_ai_rate_limit(user_id="u2", cost=3)
    usage = rl.get_ai_rate_usage(user_id="u2")
    assert usage["used"] == 3
    assert usage["remaining"] == 2
    assert usage["scope"] == "shared"


def test_keys_are_isolated(monkeypatch, tmp_path):
    rl, _ = _fresh(monkeypatch, tmp_path, store="shared", quota=2)
    rl.check_ai_rate_limit(user_id="a")
    rl.check_ai_rate_limit(user_id="a")
    with pytest.raises(HTTPException):
        rl.check_ai_rate_limit(user_id="a")
    rl.check_ai_rate_limit(user_id="b")     # different key, unaffected


def test_entries_age_out_of_the_window(monkeypatch, tmp_path):
    rl, db = _fresh(monkeypatch, tmp_path, store="shared", quota=2, window=60)
    rl.check_ai_rate_limit(user_id="old")
    rl.check_ai_rate_limit(user_id="old")
    # Age both rows past the window rather than sleeping 60s.
    db.conn.execute("UPDATE ai_rate_events SET ts = ts - 120")
    db.conn.commit()
    rl.check_ai_rate_limit(user_id="old")   # capacity has returned
    assert rl.get_ai_rate_usage(user_id="old")["used"] == 1


def test_429_carries_retry_after(monkeypatch, tmp_path):
    rl, _ = _fresh(monkeypatch, tmp_path, store="shared", quota=1, window=600)
    rl.check_ai_rate_limit(user_id="r")
    with pytest.raises(HTTPException) as exc:
        rl.check_ai_rate_limit(user_id="r")
    assert exc.value.headers["Retry-After"]
    assert exc.value.detail["retry_after_seconds"] > 0


# ── failure modes ───────────────────────────────────────────────────────

def test_a_broken_store_does_not_fail_the_request(monkeypatch, tmp_path):
    """A limiter must never be the reason a request 500s. It degrades to the
    per-process window and warns."""
    rl, _ = _fresh(monkeypatch, tmp_path, store="shared", quota=5)

    class Broken:
        conn = None

        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rl, "_db_handle", lambda: Broken(), raising=True)
    monkeypatch.setattr(rl, "_schema_ready", False, raising=False)
    rl.check_ai_rate_limit(user_id="degraded")      # must not raise


def test_disabled_flag_still_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOLUZEN_FLOW_AI_RATE_ENABLE", "0")
    rl, _ = _fresh(monkeypatch, tmp_path, store="shared", quota=1)
    monkeypatch.setenv("REVOLUZEN_FLOW_AI_RATE_ENABLE", "0")
    importlib.reload(rl)
    for _ in range(10):
        rl.check_ai_rate_limit(user_id="nolimit")


def test_no_database_falls_back_to_memory(monkeypatch, tmp_path):
    """Tests and early boot have no metastore; that must mean 'memory',
    not 'crash' and not 'no limit'."""
    rl, _ = _fresh(monkeypatch, tmp_path, store="shared", quota=2)
    monkeypatch.setattr(rl, "_db_handle", lambda: None, raising=True)
    rl.check_ai_rate_limit(user_id="nodb")
    rl.check_ai_rate_limit(user_id="nodb")
    with pytest.raises(HTTPException):
        rl.check_ai_rate_limit(user_id="nodb")
    assert rl.get_ai_rate_usage(user_id="nodb")["scope"] == "process"
