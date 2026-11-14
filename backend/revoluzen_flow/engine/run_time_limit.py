"""A wall-clock time limit on one pipeline run.

The Save dialog has offered a "Maximum Running Time" for a long while, but it
never stopped anything: the run carried on, and only a run started from the
editor got a toast afterwards saying it had taken too long. A scheduled run
that hung simply hung. This module makes the limit real.

How a run is stopped
--------------------
A timer cancels the run's cancellation token (``engine.cancellation``) when the
limit passes, the same way the Stop button does. That

  * interrupts a DuckDB query in flight, because the executor registers every
    run's DuckDB connection on its token;
  * fires any database-driver cancel a connector registered on the token
    (``register_connection_cancel``);
  * is seen by the executor between steps and in a step's retry loop, which
    stops instead of starting more work.

A step blocked in plain Python (an HTTP call, a sleep in custom code) cannot be
interrupted from outside; the run stops as soon as that call returns.

The run then ends as a failure, not a cancellation, so the failure alerts a
team relies on go out and the Executions page says why.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from revoluzen_flow.engine.cancellation import create_token

logger = logging.getLogger(__name__)

# The id the executor gives the result it adds when the limit passes between
# steps, where no step was running to carry the message. Like "validation",
# it is not a node on the canvas.
TIME_LIMIT_STEP_ID = "time_limit"

# The token's reason when the limit cancels a run, so the executor can tell
# it from a person pressing Stop.
TIME_LIMIT_REASON = "time limit"


def configured_limit_seconds(workflow: Any) -> float | None:
    """The pipeline's own limit, from ``metadata.execution_settings``.

    The Save dialog stores ``max_runtime_minutes`` (0 = no limit) and
    ``enable_timeout``. Anything missing, non-numeric or not positive means no
    limit, so every pipeline saved without one runs exactly as before.
    """
    metadata = getattr(workflow, "metadata", None) or {}
    settings = metadata.get("execution_settings") if isinstance(metadata, dict) else None
    if not isinstance(settings, dict):
        return None
    if settings.get("enable_timeout") is False:
        return None
    try:
        minutes = float(settings.get("max_runtime_minutes") or 0)
    except (TypeError, ValueError):
        return None
    return minutes * 60.0 if minutes > 0 else None


def effective_limit_seconds(*limits: Any) -> float | None:
    """The tightest of the limits that apply to a run, or None for no limit.

    A run started from a published endpoint is bound by both the pipeline's
    limit and the endpoint's own timeout; whichever is shorter wins.
    """
    values: list[float] = []
    for limit in limits:
        try:
            value = float(limit) if limit is not None else 0.0
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return min(values) if values else None


def describe_limit(seconds: float) -> str:
    """A limit in words: "45 seconds", "30 minutes", "1 hour 30 minutes"."""
    total = max(0.0, float(seconds))
    if total < 60:
        shown = f"{total:.1f}".rstrip("0").rstrip(".")
        return f"{shown} second{'' if shown == '1' else 's'}"
    whole_minutes = int(round(total / 60))
    hours, minutes = divmod(whole_minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'' if hours == 1 else 's'}")
    if minutes:
        parts.append(f"{minutes} minute{'' if minutes == 1 else 's'}")
    return " ".join(parts) or "1 minute"


class RunTimeLimit:
    """The timer for one run. ``start()`` arms it; ``stop()`` must follow."""

    def __init__(self, run_id: str, seconds: float):
        self.run_id = run_id
        self.seconds = float(seconds)
        self._reached = threading.Event()
        self._timer: threading.Timer | None = None
        self._token = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> "RunTimeLimit":
        self._token = create_token(self.run_id)
        self._timer = threading.Timer(self.seconds, self._expire)
        self._timer.daemon = True
        self._timer.name = f"run-time-limit-{self.run_id}"
        self._timer.start()
        return self

    def stop(self) -> None:
        """Disarm the timer once the run's steps are done, so cleanup queries
        after the step loop are never interrupted."""
        if self._timer is not None:
            self._timer.cancel()

    def _expire(self) -> None:
        if self._token is not None and self._token.is_cancelled:
            # Someone pressed Stop first; that is the reason that stands.
            return
        self._reached.set()
        logger.info(
            "run %s reached its time limit of %s; stopping it",
            self.run_id, describe_limit(self.seconds),
        )
        if self._token is not None:
            self._token.cancel(TIME_LIMIT_REASON)

    # ── State ────────────────────────────────────────────────────────

    @property
    def is_reached(self) -> bool:
        return self._reached.is_set()

    # ── Words for the run record ─────────────────────────────────────

    def message_during_step(self) -> str:
        return (
            f"Stopped: the run went past its time limit of "
            f"{describe_limit(self.seconds)} while this step was running."
        )

    def message_between_steps(self) -> str:
        return (
            f"Stopped: the run went past its time limit of "
            f"{describe_limit(self.seconds)}, so the remaining steps did not run."
        )


__all__ = [
    "TIME_LIMIT_REASON",
    "TIME_LIMIT_STEP_ID",
    "RunTimeLimit",
    "configured_limit_seconds",
    "describe_limit",
    "effective_limit_seconds",
]
