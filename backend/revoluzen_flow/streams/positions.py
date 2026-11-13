"""Where a log source carries on reading, kept by Flow for each pipeline step.

Kafka, Redpanda, Kinesis, Event Hubs, NATS JetStream, Pulsar and the MySQL /
MariaDB change log let a reader start anywhere in what they keep. Flow keeps
each step's place in the ``sync_state`` table of its metastore, keyed by
pipeline and step, with ``cursor_column`` saying what the place belongs to (for
example ``kafka:orders`` or ``kinesis:clicks``). On one server that table is in
SQLite; when several Enterprise servers and worker pods share a PostgreSQL
metastore, every one of them reads and moves the same row.

When the place moves
--------------------
A step hands its new place to the run (``ctx.pending_sync_cursors``). The
executor saves it only after the whole run, destinations included, has
succeeded, and only if the saved row is still the one the step read when it
started: if another run of the same pipeline, or a Reset, changed it in the
meantime, this run's place is not saved and the run log says so. Nothing is
skipped either way: at worst the next run reads some messages again.
Sample runs, sandbox runs and Test Node never move a place.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from revoluzen_flow.engine.sync_state_store import SyncState

logger = logging.getLogger(__name__)


@dataclass
class Saved:
    """A step's place as it was when the step started reading."""

    key: str
    value: dict
    row: Optional[SyncState]


class PendingPosition(SyncState):
    """A place to save once the whole run has succeeded (see the module text)."""

    expected_column: Optional[str] = None
    expected_cursor: Optional[str] = None
    had_row: bool = False
    label: str = ""

    def commit(self) -> bool:
        from revoluzen_flow.engine.sync_state_store import sync_state_store

        expected = None
        if self.had_row:
            expected = SyncState(
                workflow_id=self.workflow_id, step_id=self.step_id,
                cursor_column=self.expected_column or "", last_cursor=self.expected_cursor,
            )
        saved = sync_state_store.save_if_unchanged(self, expected)
        if not saved:
            logger.warning(
                "%s step %s: its read position changed while this run was reading (another "
                "run of this pipeline, or a Reset), so this run's position was not saved. The "
                "next run may read some of the same messages again.",
                self.label or "Stream", self.step_id,
            )
        return saved


def owner(ctx: Any, step_id: Any) -> tuple[str, str] | None:
    """(pipeline id, step id) when the step runs inside a saved pipeline."""
    workflow_id = str(getattr(ctx, "workflow_id", None) or "")
    step_id = str(step_id or "")
    return (workflow_id, step_id) if workflow_id and step_id else None


def load(ctx: Any, step_id: Any, key: str, label: str = "Stream") -> Saved:
    """The step's saved place for ``key``; empty when there is none.

    Empty when the step has never finished a run, when it runs outside a saved
    pipeline (Test Node), or when the saved place belongs to something else
    (the step's topic or stream changed).
    """
    who = owner(ctx, step_id)
    if who is None:
        return Saved(key, {}, None)
    try:
        from revoluzen_flow.engine.sync_state_store import sync_state_store

        row = sync_state_store.get(*who)
    except Exception:  # noqa: BLE001 — a read error means starting as a first run does
        logger.warning("%s step %s: could not read the saved read position.", label, who[1], exc_info=True)
        return Saved(key, {}, None)
    if row is None or row.cursor_column != key or not row.last_cursor:
        return Saved(key, {}, row)
    try:
        value = json.loads(row.last_cursor)
        if not isinstance(value, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError):
        logger.warning(
            "%s step %s: the saved read position %r could not be understood, so this run "
            "starts as a first run does.", label, who[1], row.last_cursor,
        )
        return Saved(key, {}, row)
    return Saved(key, value, row)


def pending_position(ctx: Any, step_id: Any, saved: Saved, value: dict, messages: int,
                     label: str = "Stream") -> PendingPosition | None:
    """The place to save after the run; None when nothing would change."""
    who = owner(ctx, step_id)
    if who is None or value == saved.value:
        return None
    return PendingPosition(
        workflow_id=who[0],
        step_id=who[1],
        cursor_column=saved.key,
        last_cursor=json.dumps(value, sort_keys=True),
        rows_last_run=int(messages),
        expected_column=saved.row.cursor_column if saved.row is not None else None,
        expected_cursor=saved.row.last_cursor if saved.row is not None else None,
        had_row=saved.row is not None,
        label=label,
    )


def keep(ctx: Any, step_id: Any, saved: Saved, value: dict, messages: int, label: str = "Stream") -> None:
    """Hand the run the place to save once the whole run has succeeded."""
    pending = pending_position(ctx, step_id, saved, value, messages, label)
    if pending is None:
        return
    queue = getattr(ctx, "pending_sync_cursors", None)
    if queue is not None:
        queue.append(pending)
    else:
        # A context built by hand, with no run to wait for.
        pending.commit()
