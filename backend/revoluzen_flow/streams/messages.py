"""Messages from queues and streams other than Kafka, as rows (2026-11-13).

Amazon Kinesis and SQS, Google Pub/Sub, Azure Event Hubs, MQTT, RabbitMQ,
NATS JetStream and Apache Pulsar all hand over a body (bytes) and a few
details. Every source step turns them into rows the same way:

* Message Format ``json``: a JSON object's fields become columns, any other
  body goes into ``value`` as text; ``text``: the whole body in ``value``.
* With "Include message details" (on for new steps): ``_source`` (the stream,
  queue, subject or topic), ``_message_id``, ``_message_key`` (partition key,
  routing key, ordering key), ``_message_time`` and ``_message_attributes``
  (a JSON object of the message's attributes, headers or properties).

A destination step writes each row as a JSON object (or one column's text with
Message Format ``text``), with an optional key column and attributes column.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any

FORMATS = ("json", "text")
META_TYPES = {
    "_source": "VARCHAR", "_message_id": "VARCHAR", "_message_key": "VARCHAR",
    "_message_time": "TIMESTAMPTZ", "_message_attributes": "VARCHAR",
}


@dataclass
class Received:
    body: Any
    source: str = ""
    message_id: str | None = None
    key: str | None = None
    time: _dt.datetime | None = None
    attributes: dict = field(default_factory=dict)


def check_format(value: Any, who: str) -> str:
    chosen = str(value or "json").strip().lower()
    if chosen not in FORMATS:
        raise ValueError(f"{who}: the message format '{chosen}' is not one Flow reads here. Choose json or text.")
    return chosen


def _text(body: Any) -> str:
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body).decode("utf-8", errors="replace")
    return "" if body is None else str(body)


def to_row(msg: Received, fmt: str, include_details: bool) -> dict:
    text = _text(msg.body)
    if fmt == "json":
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        row = dict(parsed) if isinstance(parsed, dict) else {"value": text}
    else:
        row = {"value": text}
    if include_details:
        stamp = msg.time
        if isinstance(stamp, _dt.datetime) and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=_dt.timezone.utc)
        row.update({
            "_source": msg.source or None,
            "_message_id": None if msg.message_id is None else str(msg.message_id),
            "_message_key": None if msg.key is None else str(msg.key),
            "_message_time": stamp,
            "_message_attributes": json.dumps(
                {str(k): _text(v) if isinstance(v, (bytes, bytearray)) else v for k, v in msg.attributes.items()},
                default=str, ensure_ascii=False,
            ) if msg.attributes else None,
        })
    return row


def relation(ctx: Any, messages: list[Received], fmt: str, include_details: bool, name: str) -> Any:
    from revoluzen_flow.streams.rows import rows_to_relation

    fixed = dict(META_TYPES) if include_details else {}
    rows = [to_row(m, fmt, include_details) for m in messages]
    return rows_to_relation(ctx.conn, rows, name, fixed=fixed, when_empty={"value": "VARCHAR", **fixed})


class Outgoing:
    """Rows as message bodies, keys and attributes for a destination step."""

    def __init__(self, fmt: str, columns: list[str], key_column: str = "", attributes_column: str = "",
                 text_column: str = "", who: str = "Destination") -> None:
        self.fmt = fmt
        self.key_column = key_column
        self.attributes_column = attributes_column
        self.who = who
        for wanted, label in ((key_column, "Key Column"), (attributes_column, "Attributes Column"),
                              (text_column, "Text Column")):
            if wanted and wanted not in columns:
                raise ValueError(f"{who}: the {label} '{wanted}' is not one of this step's input columns.")
        if fmt == "text" and not text_column:
            if len([c for c in columns if c != attributes_column]) != 1:
                raise ValueError(
                    f"{who}: with Message Format text, name the column that holds each message's "
                    f"text (Text Column), or choose json."
                )
            text_column = next(c for c in columns if c != attributes_column)
        self.text_column = text_column
        self.body_columns = [c for c in columns if c != attributes_column]

    def body(self, row: dict) -> bytes:
        if self.fmt == "text":
            value = row.get(self.text_column)
            return b"" if value is None else str(value).encode("utf-8")
        return json.dumps({c: row.get(c) for c in self.body_columns}, default=str).encode("utf-8")

    def key(self, row: dict) -> str | None:
        if not self.key_column:
            return None
        value = row.get(self.key_column)
        return None if value is None else str(value)

    def attributes(self, row: dict) -> dict[str, str]:
        if not self.attributes_column or row.get(self.attributes_column) in (None, ""):
            return {}
        raw = row[self.attributes_column]
        try:
            parsed = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            raise ValueError(
                f"{self.who}: the Attributes Column '{self.attributes_column}' must hold a JSON object, "
                f'such as {{"source": "flow"}}.'
            )
        return {str(k): str(v) for k, v in parsed.items() if v is not None}


def whole_number(value: Any, default: int, high: int | None = None) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, high) if high else number
