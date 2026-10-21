"""Durable scheduler lease — makes running more than one instance safe.

The problem
-----------
`PipelineScheduler` tracked in-flight work in process-local dicts
(`self._running_jobs`, `self._run_threads`). Those stop a single process
double-firing a schedule; they say nothing about a *second process*. And the
scheduler started unconditionally on every instance, so two instances behind
a load balancer fired every schedule twice — silently, with both runs looking
legitimate.

This is one row in the metadata database that at most one instance holds at a
time. Only the holder fires schedules.

Design notes that matter
------------------------
**Fail closed.** Every failure path returns "not held", so an instance that
cannot reach the database, or cannot tell, does not schedule. Missing a tick
is recoverable — the schedule-miss detection already alerts on it — whereas a
double run can double-charge a customer or duplicate rows in their warehouse.

**One atomic statement.** Acquire and renew are the same conditional UPDATE,
and `rowcount` decides. A read-then-write would race with another instance
between the two queries, which is the bug this module exists to prevent.

**The commit is load-bearing.** `Database.execute()` returns a cursor and does
*not* commit, and connections are thread-local (see `storage/database.py`).
Without the explicit `commit()` the lease would live in an uncommitted
transaction on the scheduler thread's own connection: this process would read
its own write and believe it held the lease, every other instance would see
nothing, and all of them would schedule. That is the exact failure this
module is supposed to prevent, so the commit is not boilerplate.

**`term` detects a lost lease.** It increments when the holder changes, never
on renewal. A process paused long enough for its lease to expire and be taken
over comes back with a stale term and can tell, rather than assuming
continuity. Full fencing — carrying the term through to every write — is not
implemented; the TTL being several times the tick interval is what keeps the
window small, and `held_term` is exposed so a caller can check.

**TTL must exceed the tick interval comfortably.** At the default 30s tick and
90s TTL, the holder renews every tick and must miss three in a row before
another instance can take over.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Three missed ticks at the default 30s interval before a takeover is allowed.
DEFAULT_TTL_SECONDS = 90

_HOLDER_ID: str | None = None


def holder_id() -> str:
    """Stable identity for this process, for the life of the process.

    Hostname and pid make it diagnosable in the lease table; the random
    suffix stops a recycled pid on the same host from being mistaken for the
    previous holder and inheriting its lease.
    """
    global _HOLDER_ID
    if _HOLDER_ID is None:
        _HOLDER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    return _HOLDER_ID


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerLease:
    """Acquire-or-renew the single scheduler lease row."""

    def __init__(self, db: Any, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._db = db
        self._ttl = int(ttl_seconds)
        self._held_term: int | None = None

    @property
    def held_term(self) -> int | None:
        """Term of the lease this process last held, or None if never held."""
        return self._held_term

    def _ensure_row(self) -> None:
        """Create the single row if it is missing. Idempotent."""
        self._db.execute(
            "INSERT OR IGNORE INTO scheduler_lease (id, holder, term) VALUES (1, NULL, 0)"
        )
        self._db.commit()

    def acquire_or_renew(self) -> bool:
        """True if this process now holds the lease.

        Takes the lease when it is unheld, expired, or already ours; otherwise
        leaves it alone and returns False.
        """
        me = holder_id()
        now = _now()
        now_s = now.isoformat()
        expires_s = (now + timedelta(seconds=self._ttl)).isoformat()

        try:
            self._ensure_row()

            # One statement, so no other instance can slip between a read and
            # a write. `term` and `acquired_at` only change on a genuine
            # takeover, which is what makes a renewal distinguishable from
            # stealing the lease back after an expiry.
            cur = self._db.execute(
                """
                UPDATE scheduler_lease
                   SET holder      = ?,
                       expires_at  = ?,
                       acquired_at = CASE WHEN holder = ? THEN acquired_at ELSE ? END,
                       term        = CASE WHEN holder = ? THEN term ELSE term + 1 END
                 WHERE id = 1
                   AND (holder IS NULL OR holder = ? OR expires_at IS NULL OR expires_at < ?)
                """,
                (me, expires_s, me, now_s, me, me, now_s),
            )
            won = bool(getattr(cur, "rowcount", 0))
            # Without this the row sits in an uncommitted transaction on this
            # thread's connection — see the module docstring.
            self._db.commit()

            if not won:
                return False

            row = self._db.execute(
                "SELECT term FROM scheduler_lease WHERE id = 1"
            ).fetchone()
            if row is not None:
                term = row[0] if not isinstance(row, dict) else row["term"]
                if self._held_term is not None and term != self._held_term:
                    logger.warning(
                        "Scheduler lease term changed %s -> %s; this instance "
                        "lost and re-took the lease, so another instance may "
                        "have scheduled in between.",
                        self._held_term, term,
                    )
                self._held_term = int(term)
            return True

        except Exception as exc:  # noqa: BLE001 — fail closed, see docstring
            logger.warning("Scheduler lease unavailable (%s); not scheduling", exc)
            return False

    def release(self) -> None:
        """Give up the lease so another instance can take over immediately.

        Called on clean shutdown. Best-effort: if it fails the lease simply
        expires, which costs one TTL of scheduling delay rather than
        correctness.
        """
        try:
            self._db.execute(
                "UPDATE scheduler_lease SET holder = NULL, expires_at = NULL "
                "WHERE id = 1 AND holder = ?",
                (holder_id(),),
            )
            self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Scheduler lease release failed (%s); it will expire", exc)
        finally:
            self._held_term = None

    def current_holder(self) -> dict[str, Any] | None:
        """Who holds the lease right now — for diagnostics and health output."""
        try:
            row = self._db.execute(
                "SELECT holder, term, acquired_at, expires_at "
                "FROM scheduler_lease WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            if isinstance(row, dict):
                return dict(row)
            return {
                "holder": row[0], "term": row[1],
                "acquired_at": row[2], "expires_at": row[3],
            }
        except Exception:  # noqa: BLE001
            return None
