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

Several runs, several servers (2026-11-13)
------------------------------------------
The position is saved only if the saved row is still the one this run read
when it started (``streams/positions.py``). When two runs of one pipeline
overlap, or several Enterprise servers and worker pods share one metastore,
a run never puts back a position another run moved further, and a Reset made
during a run stays a Reset.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "kafka:"


def position_key(topic: str) -> str:
    """The saved row's cursor column: which topic the offsets belong to."""
    return f"{_KEY_PREFIX}{topic}"


def load(ctx: Any, step_id: str | None, topic: str, label: str = "Kafka Source"):
    """``({partition: next offset}, the saved row as it was)`` for one step.

    Since 2026-11-13 the position is saved through ``streams/positions.py``:
    only if no other run, and no Reset, moved it while this run was reading.
    """
    from revoluzen_flow.streams import positions

    saved = positions.load(ctx, step_id, position_key(topic), label=label)
    try:
        stored = saved.value.get("next_offsets") or {}
        start = {int(partition): int(offset) for partition, offset in stored.items() if int(offset) >= 0}
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "%s %s: the saved read position %r could not be understood, so this run starts at "
            "the oldest message in the topic.", label, step_id, saved.value,
        )
        start = {}
    return start, saved


def keep(ctx: Any, step_id: str | None, saved: Any, start: dict[int, int], end: dict[int, int],
         messages: int, label: str = "Kafka Source") -> None:
    """Hand the run the position to save if the whole run succeeds.

    ``end`` is the offset after the last message read in each partition, with the
    partitions this run read nothing from carried over from ``start``.
    """
    if end == start:
        return
    from revoluzen_flow.streams import positions

    positions.keep(ctx, step_id, saved, {"next_offsets": {str(p): end[p] for p in sorted(end)}},
                   messages, label=label)


def saved_positions(ctx: Any, step_id: str | None, topic: str) -> dict[int, int]:
    """``{partition: offset of the next message to read}`` from the last successful run.

    Empty when the step has never finished a run, when it runs outside a saved
    pipeline (Test Node), or when the saved offsets belong to another topic.
    """
    start, saved = load(ctx, step_id, topic)
    _started = getattr(ctx, "_kafka_saved_rows", None)
    if _started is None:
        try:
            ctx._kafka_saved_rows = _started = {}
        except AttributeError:
            return start
    _started[(str(step_id or ""), topic)] = saved
    return start


def keep_positions(
    ctx: Any, step_id: str | None, topic: str,
    start: dict[int, int], end: dict[int, int], messages: int,
) -> None:
    """The older name of ``keep``, for callers that used ``saved_positions``."""
    saved = (getattr(ctx, "_kafka_saved_rows", None) or {}).get((str(step_id or ""), topic))
    if saved is None:
        _start, saved = load(ctx, step_id, topic)
    keep(ctx, step_id, saved, start, end, messages)
