"""How a Kafka message becomes a row, and a row a message (2026-11-13).

Message formats a Kafka Source reads:

* ``json``: a JSON object's fields become columns; any other value goes into
  a ``value`` column as text (the behaviour before November 2026).
* ``text``: the whole value as text in ``value``.
* ``avro``: Avro with a Schema Registry (``schema_registry.py``); a record's
  fields become columns with their Avro types (decimals, dates and times kept).
* ``debezium``: Debezium change events, as JSON (with or without the schema
  wrapper) or as Avro; each becomes a change row like the Change Data Capture
  step's, with ``_op``, ``_table`` and ``_committed_at``.

With "Include key, headers and time" a row also gets ``_kafka_key`` (text, or
the decoded Avro key), ``_kafka_headers`` (a JSON object) and
``_kafka_timestamp``. ``_kafka_topic``, ``_kafka_partition`` and
``_kafka_offset`` are always there.

A Kafka Sink writes ``json`` (each row as a JSON object) or ``avro`` (the rows'
schema registered under ``<topic>-value``), with an optional key column and an
optional headers column holding a JSON object.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

READ_FORMATS = ("json", "text", "avro", "debezium")
WRITE_FORMATS = ("json", "avro")

META_TYPES = {"_kafka_topic": "VARCHAR", "_kafka_partition": "INTEGER", "_kafka_offset": "BIGINT"}
EXTRA_TYPES = {"_kafka_key": "VARCHAR", "_kafka_headers": "VARCHAR", "_kafka_timestamp": "TIMESTAMPTZ"}
CHANGE_TYPES = {"_op": "VARCHAR", "_table": "VARCHAR", "_committed_at": "TIMESTAMPTZ", "_seq": "BIGINT"}

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_DEBEZIUM_OPS = {"c": "insert", "u": "update", "d": "delete", "r": "snapshot", "t": "truncate"}


@dataclass
class Message:
    """One Kafka message, as either client library hands it over."""

    topic: str
    partition: int
    offset: int
    value: Any = None
    key: Any = None
    headers: list = field(default_factory=list)
    timestamp_ms: int | None = None


def _text(data: Any) -> str | None:
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data).decode("utf-8", errors="replace")
    return str(data)


def check_format(value: Any, who: str, allowed: tuple[str, ...]) -> str:
    chosen = str(value or "json").strip().lower()
    if chosen not in allowed:
        raise ValueError(f"{who}: the message format '{chosen}' is not one Flow reads. Choose "
                         f"{', '.join(allowed[:-1])} or {allowed[-1]}.")
    return chosen


class MessageReader:
    """Turns messages into rows for one Kafka Source step."""

    def __init__(self, fmt: str, registry: Any = None, include_metadata: bool = False,
                 who: str = "Kafka Source") -> None:
        self.fmt = fmt
        self.registry = registry
        self.include_metadata = include_metadata
        self.who = who
        self.seq = 0

    def fixed_types(self) -> dict[str, str]:
        types = dict(META_TYPES)
        if self.include_metadata:
            types.update(EXTRA_TYPES)
        if self.fmt == "debezium":
            types.update(CHANGE_TYPES)
        return types

    def row(self, msg: Message) -> dict | None:
        """The row for one message; None for a message that holds nothing (a delete marker)."""
        value = msg.value
        if value is None:
            return None
        if self.fmt == "debezium":
            row = self._change(value)
            if row is None:
                return None
        elif self.fmt == "text":
            row = {"value": _text(value)}
        elif self.fmt == "avro":
            decoded, _schema_id = self._registry().decode(bytes(value))
            row = dict(decoded) if isinstance(decoded, dict) else {"value": decoded}
        else:
            text = _text(value)
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            row = parsed if isinstance(parsed, dict) else {"value": text}
        row["_kafka_offset"] = msg.offset
        row["_kafka_partition"] = msg.partition
        row["_kafka_topic"] = msg.topic
        if self.include_metadata:
            row["_kafka_key"] = self._key(msg.key)
            row["_kafka_headers"] = json.dumps(
                {str(k): _text(v) for k, v in (msg.headers or [])}, ensure_ascii=False
            ) if msg.headers else None
            row["_kafka_timestamp"] = (
                _EPOCH + _dt.timedelta(milliseconds=int(msg.timestamp_ms))
                if msg.timestamp_ms not in (None, -1) and int(msg.timestamp_ms) >= 0 else None
            )
        return row

    def _registry(self):
        if self.registry is None:
            raise ValueError(
                f"{self.who}: Avro messages need a Schema Registry. Add its address to the Kafka connection."
            )
        return self.registry

    def _key(self, key: Any) -> str | None:
        if key is None:
            return None
        if self.registry is not None and self.registry.is_framed(key):
            try:
                decoded, _ = self.registry.decode(bytes(key))
                return json.dumps(decoded, default=str) if isinstance(decoded, (dict, list)) else str(decoded)
            except ValueError:
                pass
        return _text(key)

    # ── Debezium ─────────────────────────────────────────────────────

    def _change(self, value: Any) -> dict | None:
        schema = None
        if self.registry is not None and self.registry.is_framed(value):
            event, _ = self.registry.decode(bytes(value))
        else:
            try:
                event = json.loads(_text(value))
            except (json.JSONDecodeError, TypeError):
                raise ValueError(
                    f"{self.who}: a message is not a Debezium change event (it is not JSON or "
                    f"registry Avro). Choose another message format for this topic."
                ) from None
            if isinstance(event, dict) and "payload" in event and "schema" in event:
                schema, event = event.get("schema"), event.get("payload")
        if not isinstance(event, dict):
            return None
        op = _DEBEZIUM_OPS.get(str(event.get("op") or ""))
        if op is None:
            return None  # a heartbeat, schema change or other non-row message
        image = event.get("before") if op == "delete" else event.get("after")
        image_schema = _field_schema(schema, "before" if op == "delete" else "after")
        row = convert_values(image or {}, image_schema)
        source = event.get("source") or {}
        if isinstance(source, dict):
            container = source.get("schema") or source.get("db") or ""
            name = source.get("table") or source.get("collection") or ""
            row["_table"] = f"{container}.{name}" if container and name else (name or container or None)
            ts = source.get("ts_ms") or event.get("ts_ms")
        else:
            row["_table"] = None
            ts = event.get("ts_ms")
        row["_op"] = op
        row["_committed_at"] = _EPOCH + _dt.timedelta(milliseconds=int(ts)) if ts else None
        row["_seq"] = self.seq
        self.seq += 1
        return row


def _field_schema(schema: Any, name: str) -> Any:
    if not isinstance(schema, dict):
        return None
    for item in schema.get("fields") or []:
        if isinstance(item, dict) and item.get("field") == name:
            return item
    return None


def convert_values(values: dict, schema: Any) -> dict:
    """Debezium's JSON encodings (decimals as base64, times as numbers) as real values."""
    if not isinstance(values, dict):
        return {}
    kinds = {}
    if isinstance(schema, dict):
        for item in schema.get("fields") or []:
            if isinstance(item, dict) and item.get("field"):
                kinds[item["field"]] = item
    out = {}
    for name, value in values.items():
        out[name] = _convert(value, kinds.get(name))
    return out


def _convert(value: Any, kind: Any) -> Any:
    if value is None or not isinstance(kind, dict):
        return value
    name = kind.get("name") or ""
    params = kind.get("parameters") or {}
    try:
        if name == "org.apache.kafka.connect.data.Decimal" and isinstance(value, str):
            unscaled = int.from_bytes(base64.b64decode(value), "big", signed=True)
            return decimal.Decimal(unscaled).scaleb(-int(params.get("scale", 0)))
        if name == "io.debezium.data.VariableScaleDecimal" and isinstance(value, dict):
            unscaled = int.from_bytes(base64.b64decode(value.get("value", "")), "big", signed=True)
            return decimal.Decimal(unscaled).scaleb(-int(value.get("scale", 0)))
        if name in ("io.debezium.time.Date", "org.apache.kafka.connect.data.Date"):
            return _dt.date(1970, 1, 1) + _dt.timedelta(days=int(value))
        if name in ("io.debezium.time.Timestamp", "org.apache.kafka.connect.data.Timestamp"):
            return _dt.datetime(1970, 1, 1) + _dt.timedelta(milliseconds=int(value))
        if name == "io.debezium.time.MicroTimestamp":
            return _dt.datetime(1970, 1, 1) + _dt.timedelta(microseconds=int(value))
        if name == "io.debezium.time.NanoTimestamp":
            return _dt.datetime(1970, 1, 1) + _dt.timedelta(microseconds=int(value) // 1000)
        if name == "io.debezium.time.ZonedTimestamp" and isinstance(value, str):
            return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if name in ("io.debezium.time.Time", "org.apache.kafka.connect.data.Time"):
            return (_dt.datetime(1970, 1, 1) + _dt.timedelta(milliseconds=int(value))).time()
        if name == "io.debezium.time.MicroTime":
            return (_dt.datetime(1970, 1, 1) + _dt.timedelta(microseconds=int(value))).time()
    except (ValueError, TypeError, decimal.InvalidOperation):
        return value
    return value


# ── Writing ────────────────────────────────────────────────────────────────


def _avro_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class MessageWriter:
    """Turns rows into (key, value, headers) for one Kafka Sink step."""

    def __init__(self, fmt: str, topic: str, columns: list[str], types: list[str],
                 key_column: str = "", headers_column: str = "", registry: Any = None,
                 who: str = "Kafka Sink") -> None:
        self.fmt = fmt
        self.topic = topic
        self.key_column = key_column
        self.headers_column = headers_column
        self.registry = registry
        self.who = who
        for wanted, label in ((key_column, "Key Column"), (headers_column, "Headers Column")):
            if wanted and wanted not in columns:
                raise ValueError(f"{who}: the {label} '{wanted}' is not one of this step's input columns.")
        value_columns = [c for c in columns if c != headers_column]
        self.value_columns = value_columns
        self.schema_id = None
        self.names: dict[str, str] = {}
        if fmt == "avro":
            if registry is None:
                raise ValueError(f"{who}: Avro messages need a Schema Registry. Add its address to the Kafka connection.")
            from revoluzen_flow.streams.schema_registry import record_schema

            kinds = [t for c, t in zip(columns, types) if c != headers_column]
            schema, self.names = record_schema(topic, value_columns, kinds)
            self.schema_id = registry.register(f"{topic}-value", schema)

    def message(self, row: dict) -> tuple[bytes | None, bytes, list[tuple[str, bytes]]]:
        key = None
        if self.key_column:
            raw = row.get(self.key_column)
            key = None if raw is None else str(raw).encode("utf-8")
        headers: list[tuple[str, bytes]] = []
        if self.headers_column and row.get(self.headers_column) not in (None, ""):
            raw = row[self.headers_column]
            try:
                parsed = raw if isinstance(raw, dict) else json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"{self.who}: the Headers Column '{self.headers_column}' must hold a JSON object, "
                    f'such as {{"source": "flow"}}.'
                )
            headers = [(str(k), str(v).encode("utf-8")) for k, v in parsed.items() if v is not None]
        values = {c: row.get(c) for c in self.value_columns}
        if self.fmt == "avro":
            record = {self.names[c]: _avro_value(v) for c, v in values.items()}
            return key, self.registry.encode(self.schema_id, record), headers
        return key, json.dumps(values, default=str).encode("utf-8"), headers
