"""Debezium change events from Kafka, for the Change Data Capture step (2026-11-13).

Debezium reads the change logs of SQL Server, Oracle, MongoDB, Db2, MySQL,
PostgreSQL and others and writes one Kafka topic per table. The step reads such
a topic with the Kafka Source's reader (same connection, sign-in, certificates
and read positions, saved only after the whole run succeeds), and turns each
event into a change row (``kafka_formats.MessageReader._change``):

* ``_op``: insert, update, delete, snapshot (Debezium's initial copy) or truncate;
* the row after the change (before it, for a delete), with Debezium's JSON
  encodings of decimals, dates and times turned back into real values when the
  events carry their schema;
* ``_table``, ``_committed_at`` (the source database's commit time) and
  ``_seq`` (the order the run read them in).

JSON events with or without the schema wrapper and Avro events with a Schema
Registry are recognised by themselves. Delete markers that follow a delete
(tombstones) and heartbeat messages give no row.
"""

from __future__ import annotations

from typing import Any

WHO = "Change Data Capture (Debezium)"


def read_debezium(ctx: Any, params: dict, config: dict, step_id: str) -> Any:
    from revoluzen_flow.nodes.sources import read_kafka_rows

    settings = dict(params)
    settings["max_messages"] = params.get("max_changes") or params.get("max_messages") or 10000
    # "copy": the first run reads everything the topic keeps, which starts with
    # Debezium's own copy of the table. "new": only events after the first run.
    settings["start_from"] = "oldest" if str(params.get("start_from") or "new") == "copy" else "newest"
    settings["timeout_seconds"] = params.get("timeout_seconds") or 30
    return read_kafka_rows(ctx, settings, who=WHO, fmt="debezium")
