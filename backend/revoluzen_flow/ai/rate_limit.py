"""Per-user AI endpoint rate limiter.

The AI agent + AI pipeline endpoints (`/api/ai/agent`,
`/api/ai/agent/stream`, etc.) make outbound LLM calls that cost real
money (OpenAI / Anthropic / OpenRouter charge per token, Ollama is
free but CPU-bound). Without per-user throttling, a single user — or
a single leaked session token — can:

  * drain the operator's LLM budget in minutes (cost abuse)
  * saturate the Ollama queue and DoS other workspace users
  * trigger LLM-vendor abuse flags that get the org's API key
    rate-limited or banned

Defense: sliding-window counter keyed on `(user_id_or_ip)`. When a
window's quota is exhausted, subsequent requests fail with HTTP 429
+ Retry-After so the frontend can show a friendly cool-down.

Where the window lives (2026-10-23)
-----------------------------------
The counter used to be a process-local dict, with the note "no external
store required". That is true and correct for one process, and silently
wrong for several: behind a load balancer each instance keeps its own
window, so N instances allow N x the configured quota. A cost control that
stops working at the moment you scale out is worse than none, because the
number in the config still says 60.

So there are two stores, chosen by ``REVOLUZEN_FLOW_AI_RATE_STORE``:

  memory  - the original per-process deque. Fastest, correct for one
            process.
  shared  - a row per request in the metastore, so every instance sees one
            window. Costs a handful of SQLite statements per AI request,
            which is nothing next to the LLM round-trip it is guarding.
  auto    - (default) shared when the install is in server mode and a
            database handle exists, memory otherwise.

``auto`` keys off ``IS_SERVER_MODE`` because that is already the project's
"this is a shared/exposed deployment" switch — the same one that decides
whether anonymous callers are refused. A laptop install stays on the fast
path; anything that could be load-balanced is by definition server mode.

Still not distributed-perfect: the shared store serialises on SQLite, so
it inherits the single-host topology documented in docs/deployment.md.
A multi-host deployment needs a real shared store, and `_SharedStore` is
the seam to put one behind.

Tunables via env:
  REVOLUZEN_FLOW_AI_RATE_MAX_PER_HOUR    default 60   (per-user calls per hour)
  REVOLUZEN_FLOW_AI_RATE_WINDOW_SECONDS  default 3600 (sliding window length)
  REVOLUZEN_FLOW_AI_RATE_ENABLE          default 1    (set to 0 to disable)
  REVOLUZEN_FLOW_AI_RATE_STORE           default auto (auto | memory | shared)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────

_MAX_PER_WINDOW = int(os.environ.get("REVOLUZEN_FLOW_AI_RATE_MAX_PER_HOUR", "60"))
_WINDOW_SECONDS = int(os.environ.get("REVOLUZEN_FLOW_AI_RATE_WINDOW_SECONDS", "3600"))
_RATE_LIMIT_ENABLED = os.environ.get(
    "REVOLUZEN_FLOW_AI_RATE_ENABLE", "1"
).strip().lower() in ("1", "true", "yes", "on")
_STORE_MODE = os.environ.get("REVOLUZEN_FLOW_AI_RATE_STORE", "auto").strip().lower()


# ── State ─────────────────────────────────────────────────────────────
#
# Per-key timestamp deque. Each entry is a unix-seconds float. On every
# request we drop entries older than now - WINDOW and check the length.
# This is O(N) over the window length per request, but N is bounded by
# the quota so the cost is trivial in absolute terms.

_state_lock = threading.Lock()
_state: dict[str, deque] = {}


def _key(user_id: str | None, fallback_ip: str | None) -> str:
    """Build the rate-limit key. Prefer user_id; fall back to IP."""
    if user_id:
        return f"user:{user_id}"
    return f"ip:{fallback_ip or 'unknown'}"


# ── Shared (cross-instance) store ─────────────────────────────────────


def _db_handle():
    """The metastore handle, or None when there isn't one.

    Lazy and defensive on purpose: this module is imported by tests that
    never build an app, and a rate limiter must never be the reason a
    request fails. No handle simply means the memory store.
    """
    try:
        from revoluzen_flow.main import app_state
        return app_state.get("db")
    except Exception:
        return None


def _use_shared() -> bool:
    if _STORE_MODE == "memory":
        return False
    if _STORE_MODE == "shared":
        return _db_handle() is not None
    # auto
    try:
        from revoluzen_flow import runtime_config
        if not runtime_config.IS_SERVER_MODE:
            return False
    except Exception:
        return False
    return _db_handle() is not None


_schema_ready = False


def _ensure_schema(db) -> None:
    global _schema_ready
    if _schema_ready:
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS ai_rate_events ("
        "  key TEXT NOT NULL,"
        "  ts  REAL NOT NULL"
        ")"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_rate_events_key_ts "
        "ON ai_rate_events (key, ts)"
    )
    db.commit()
    _schema_ready = True


def _shared_check(db, key: str, cost: int, now: float, cutoff: float) -> int | None:
    """Count-and-record in one atomic step. Returns the retry-after seconds
    when over quota, or None when the request was recorded.

    ``BEGIN IMMEDIATE`` is what makes this safe across processes: it takes
    the write lock up front, so two instances cannot both read "59 used"
    and both insert. Without it this would be a textbook check-then-act
    race and the shared store would be no better than the per-process one.
    """
    _ensure_schema(db)
    conn = db.conn
    # Only open a transaction if this connection isn't already in one.
    # sqlite3 has no nested transactions, and an earlier statement on this
    # thread's connection may well have left an implicit one open — that is
    # exactly the condition the write-durability work documented. Issuing
    # BEGIN then would raise "cannot start a transaction within a
    # transaction" and drop us to the per-process window, silently
    # restoring the N x quota bug this code exists to fix.
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM ai_rate_events WHERE key = ? AND ts < ?", (key, cutoff)
        )
        used = conn.execute(
            "SELECT COUNT(*) FROM ai_rate_events WHERE key = ?", (key,)
        ).fetchone()[0]
        if used + cost > _MAX_PER_WINDOW:
            row = conn.execute(
                "SELECT MIN(ts) FROM ai_rate_events WHERE key = ?", (key,)
            ).fetchone()
            oldest = (row[0] if row and row[0] is not None else now)
            if started:
                conn.commit()
            return max(1, int(_WINDOW_SECONDS - (now - oldest)))
        conn.executemany(
            "INSERT INTO ai_rate_events (key, ts) VALUES (?, ?)",
            [(key, now)] * cost,
        )
        if started:
            conn.commit()
        return None
    except Exception:
        if started:
            try:
                conn.rollback()
            except Exception:
                pass
        raise


def _shared_usage(db, key: str, cutoff: float) -> int:
    _ensure_schema(db)
    row = db.conn.execute(
        "SELECT COUNT(*) FROM ai_rate_events WHERE key = ? AND ts >= ?",
        (key, cutoff),
    ).fetchone()
    return int(row[0]) if row else 0


def check_ai_rate_limit(
    *,
    user_id: str | None,
    fallback_ip: str | None = None,
    cost: int = 1,
) -> None:
    """Enforce the AI per-user rate limit.

    Call this at the top of any AI-cost endpoint. If the user is over
    quota, raises HTTP 429 with a Retry-After header. Otherwise records
    the request and returns.

    Args:
        user_id: authenticated user id (preferred key).
        fallback_ip: client IP for anonymous callers.
        cost: how many "calls" this request counts as. Streaming
            endpoints may want to charge 2-3 to reflect their
            longer LLM session. Default 1.

    The check is a no-op when ``REVOLUZEN_FLOW_AI_RATE_ENABLE=0``.
    """
    if not _RATE_LIMIT_ENABLED:
        return

    key = _key(user_id, fallback_ip)
    now = time.time()
    cutoff = now - _WINDOW_SECONDS

    if _use_shared():
        db = _db_handle()
        try:
            retry_after = _shared_check(db, key, cost, now, cutoff)
        except Exception as exc:  # noqa: BLE001
            # A limiter must never be the reason a request fails. Fall back
            # to the per-process window and say so once, loudly enough to
            # be noticed in logs — a silent downgrade to N x quota is the
            # thing this whole change exists to prevent.
            logger.warning(
                "AI rate limit: shared store unavailable (%s); "
                "falling back to this process's own window, so the "
                "effective quota is per-instance until it recovers", exc,
            )
        else:
            if retry_after is not None:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "ai_rate_limit_exceeded",
                        "message": (
                            f"AI request quota exceeded ({_MAX_PER_WINDOW} per "
                            f"{_WINDOW_SECONDS // 60} minutes). Try again in "
                            f"{retry_after // 60} minute(s)."
                        ),
                        "retry_after_seconds": retry_after,
                        "quota": _MAX_PER_WINDOW,
                        "window_seconds": _WINDOW_SECONDS,
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            return

    with _state_lock:
        dq = _state.get(key)
        if dq is None:
            dq = deque()
            _state[key] = dq
        # Drop entries that have aged out of the window.
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) + cost > _MAX_PER_WINDOW:
            # Over quota — figure out when the oldest in-window entry
            # will age out, that's when capacity returns.
            oldest = dq[0] if dq else now
            retry_after = max(1, int(_WINDOW_SECONDS - (now - oldest)))
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "ai_rate_limit_exceeded",
                    "message": (
                        f"AI request quota exceeded ({_MAX_PER_WINDOW} per "
                        f"{_WINDOW_SECONDS // 60} minutes). Try again in "
                        f"{retry_after // 60} minute(s)."
                    ),
                    "retry_after_seconds": retry_after,
                    "quota": _MAX_PER_WINDOW,
                    "window_seconds": _WINDOW_SECONDS,
                },
                headers={"Retry-After": str(retry_after)},
            )

        # Record this request.
        for _ in range(cost):
            dq.append(now)


def get_ai_rate_usage(
    *,
    user_id: str | None,
    fallback_ip: str | None = None,
) -> dict:
    """Inspect current usage for a key. Used by the agent budget endpoint.

    Returns:
        {used: int, quota: int, remaining: int, window_seconds: int}
    """
    key = _key(user_id, fallback_ip)
    now = time.time()
    cutoff = now - _WINDOW_SECONDS

    shared = False
    used = 0
    if _use_shared():
        try:
            used = _shared_usage(_db_handle(), key, cutoff)
            shared = True
        except Exception:
            shared = False
    if not shared:
        with _state_lock:
            dq = _state.get(key, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            used = len(dq)
    return {
        "used": used,
        "quota": _MAX_PER_WINDOW,
        "remaining": max(0, _MAX_PER_WINDOW - used),
        "window_seconds": _WINDOW_SECONDS,
        "enabled": _RATE_LIMIT_ENABLED,
        # Which window this number came from. A caller comparing two
        # instances' numbers needs to know whether they share one.
        "scope": "shared" if shared else "process",
    }
