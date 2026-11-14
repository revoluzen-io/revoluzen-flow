"""Where a Kafka Source step carries on reading (2026-11-11).

Every run of a Kafka Source used to start again at the oldest message the topic
still kept: both readers turned off Kafka's own position saving and never saved a
position themselves. A scheduled pipeline read the same messages on every run,
and an append destination filled with copies.

Flow now keeps each step's read position itself: for every partition of the
topic, the offset of the next message to read. It is stored with the other
incremental-load cursors (the ``sync_state`` table), for one pipeline and step.

When the position moves
-----------------------
Only after the whole run succeeds. The step hands the new position to the run
(``ctx.pending_sync_cursors``), and the executor saves it once every step,
destinations included, has finished without an error. A run that fails or is
stopped leaves the position where it was, so the next run reads those messages
again: after a failure a message can be delivered twice, but none is skipped.
The incremental Database Source works the same way. Sample runs, sandbox runs
and Test Node never save a position. A run resumed from a failed one does not
read the topic again, so it saves no position either; the next run then reads
those messages once more.

Why not a Kafka consumer group
------------------------------
Kafka saves a group's position while the step is still reading, before the rest
of the run is known to succeed. And steps that share a group name split the
topic's partitions between them and move each other's position, while every
Kafka step had the same default group. So the step joins no group: it takes
every partition of the topic itself and starts each one at its saved offset. Two
pipelines that read one topic each get every message, whatever group name they
use. Kafka's own tools therefore do not show these positions.

A saved position belongs to one topic. When the step's topic changes, the old
position is ignored and the next run starts again as a first run does.
Deleting the step's saved state (``DELETE /api/sync-state/{pipeline}/{step}``,
the Reset button in the step's form) does the same.

Where a first run starts
------------------------
The step's "First run starts at" setting (``start_from``): the oldest message
the topic still keeps, or only new messages. With "newest" the first run starts
where each partition ends at that moment, usually reads nothing, and that place
is kept once the run succeeds, so the next run carries on from it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "kafka:"


def position_key(topic: str) -> str:
    """The saved row's cursor column: which topic the offsets belong to."""
    return f"{_KEY_PREFIX}{topic}"


def _owner(ctx: Any, step_id: str | None) -> tuple[str, str] | None:
    workflow_id = str(getattr(ctx, "workflow_id", None) or "")
    step_id = str(step_id or "")
    return (workflow_id, step_id) if workflow_id and step_id else None


def saved_positions(ctx: Any, step_id: str | None, topic: str) -> dict[int, int]:
    """``{partition: offset of the next message to read}`` from the last successful run.

    Empty when the step has never finished a run, when it runs outside a saved
    pipeline (Test Node), or when the saved offsets belong to another topic.
    """
    owner = _owner(ctx, step_id)
    if owner is None:
        return {}
    try:
        from revoluzen_flow.engine.sync_state_store import sync_state_store

        state = sync_state_store.get(*owner)
    except Exception:  # noqa: BLE001 — a read error means starting at the oldest message
        logger.warning("Kafka Source %s: could not read the saved read position.", owner[1], exc_info=True)
        return {}
    if state is None or state.cursor_column != position_key(topic) or not state.last_cursor:
        return {}
    try:
        stored = json.loads(state.last_cursor)["next_offsets"]
        return {int(partition): int(offset) for partition, offset in stored.items() if int(offset) >= 0}
    except (ValueError, TypeError, KeyError, AttributeError):
        logger.warning(
            "Kafka Source %s: the saved read position %r could not be understood, so this run "
            "starts at the oldest message in the topic.", owner[1], state.last_cursor,
        )
        return {}


def keep_positions(
    ctx: Any, step_id: str | None, topic: str,
    start: dict[int, int], end: dict[int, int], messages: int,
) -> None:
    """Hand the run the position to save if the whole run succeeds.

    ``end`` is the offset after the last message read in each partition, with the
    partitions this run read nothing from carried over from ``start``.
    """
    owner = _owner(ctx, step_id)
    if owner is None or end == start:
        return
    from revoluzen_flow.engine.sync_state_store import SyncState, sync_state_store

    state = SyncState(
        workflow_id=owner[0],
        step_id=owner[1],
        cursor_column=position_key(topic),
        last_cursor=json.dumps({"next_offsets": {str(p): end[p] for p in sorted(end)}}),
        rows_last_run=int(messages),
    )
    pending = getattr(ctx, "pending_sync_cursors", None)
    if pending is not None:
        pending.append(state)
    else:
        # A context built by hand, with no run to wait for.
        sync_state_store.upsert(state)
