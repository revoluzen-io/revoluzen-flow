"""Avro messages with a Schema Registry (2026-11-13).

Confluent's wire format, which Confluent Cloud, Redpanda's built-in registry,
Apicurio (its Confluent-compatible API), Aiven Karapace and Debezium's Avro
converter all use: one zero byte, the schema's id as four bytes, then the Avro
record without its schema. Flow asks the registry for the schema by id (and
keeps it for the run), and when writing registers the schema of the rows under
the subject ``<topic>-value`` (``<topic>-key`` for keys), which returns the id
of an identical schema that is already there.

Avro is read and written with ``fastavro``. Only Avro schemas are understood;
a registry message in JSON Schema or Protobuf says so plainly.
"""

from __future__ import annotations

import base64
import io
import json
import re
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAGIC = 0


def _fastavro():
    try:
        import fastavro  # type: ignore
    except ImportError:
        from revoluzen_flow.connections.addons import driver_message

        raise ValueError(f"Kafka: {driver_message('Avro', 'fastavro')}") from None
    return fastavro


class SchemaRegistry:
    """A Confluent-compatible Schema Registry, reached over HTTP(S)."""

    def __init__(self, url: str, username: str = "", password: str = "", ca_pem: str = "",
                 timeout: int = 10, who: str = "Kafka") -> None:
        url = (url or "").strip().rstrip("/")
        if not re.match(r"^https?://", url):
            raise ValueError(
                f"{who}: Avro messages need the Schema Registry address in the Kafka connection, "
                f"for example https://registry.example.com:8081."
            )
        self.url = url
        self.who = who
        self.timeout = timeout
        self._auth = None
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self._auth = f"Basic {token}"
        self._password = password
        self._context = ssl.create_default_context(cadata=ca_pem or None) if url.startswith("https") else None
        self._by_id: dict[int, Any] = {}
        self._raw_by_id: dict[int, Any] = {}
        self._ids: dict[tuple[str, str], int] = {}

    @classmethod
    def from_settings(cls, settings: Any, who: str) -> "SchemaRegistry":
        return cls(settings.registry_url, settings.registry_username, settings.registry_password,
                   getattr(settings, "ca_pem", "") if str(settings.registry_url).startswith("https") else "",
                   who=who)

    # ── HTTP ─────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method)
        request.add_header("Accept", "application/vnd.schemaregistry.v1+json, application/json")
        if data is not None:
            request.add_header("Content-Type", "application/vnd.schemaregistry.v1+json")
        if self._auth:
            request.add_header("Authorization", self._auth)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._context) as reply:
                return json.loads(reply.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            if exc.code in (401, 403):
                raise ValueError(
                    f"{self.who}: the Schema Registry at {self.url} refused the sign-in (HTTP "
                    f"{exc.code}). Check the registry username and password in the Kafka connection."
                ) from None
            raise ValueError(
                f"{self.who}: the Schema Registry at {self.url} answered HTTP {exc.code}"
                f"{': ' + self._hide(detail) if detail else ''}."
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ValueError(f"{self.who}: could not reach the Schema Registry at {self.url}: {self._hide(reason)}") from None

    def _hide(self, text: Any) -> str:
        text = str(text)
        return text.replace(self._password, "\N{BULLET}" * 8) if self._password else text

    # ── Schemas ──────────────────────────────────────────────────────

    def schema(self, schema_id: int) -> Any:
        """The parsed Avro schema with this id."""
        if schema_id not in self._by_id:
            found = self._call("GET", f"/schemas/ids/{int(schema_id)}")
            kind = str((found or {}).get("schemaType") or "AVRO").upper()
            if kind != "AVRO":
                raise ValueError(
                    f"{self.who}: these messages use a {kind} schema. Flow reads Avro and JSON messages; "
                    f"choose the JSON message format if the values are JSON."
                )
            raw = json.loads(found["schema"])
            self._raw_by_id[schema_id] = raw
            self._by_id[schema_id] = _fastavro().parse_schema(raw)
        return self._by_id[schema_id]

    def raw_schema(self, schema_id: int) -> Any:
        self.schema(schema_id)
        return self._raw_by_id[schema_id]

    def register(self, subject: str, schema: dict) -> int:
        """The id of ``schema`` under ``subject``, registering it when new."""
        text = json.dumps(schema, sort_keys=True)
        key = (subject, text)
        if key not in self._ids:
            found = self._call("POST", f"/subjects/{urllib.parse.quote(subject, safe='')}/versions",
                               {"schema": text})
            self._ids[key] = int(found["id"])
            self._by_id.setdefault(self._ids[key], _fastavro().parse_schema(schema))
            self._raw_by_id.setdefault(self._ids[key], schema)
        return self._ids[key]

    # ── The wire format ──────────────────────────────────────────────

    @staticmethod
    def is_framed(data: Any) -> bool:
        return isinstance(data, (bytes, bytearray)) and len(data) > 5 and data[0] == MAGIC

    def decode(self, data: bytes) -> tuple[Any, int]:
        """``(value, schema id)`` of one framed Avro message."""
        if not self.is_framed(data):
            raise ValueError(
                f"{self.who}: a message is not Avro in the Schema Registry format (it does not "
                f"start with the registry's marker). Choose the JSON or Text message format."
            )
        schema_id = struct.unpack(">I", bytes(data[1:5]))[0]
        value = _fastavro().schemaless_reader(io.BytesIO(bytes(data[5:])), self.schema(schema_id), None)
        return value, schema_id

    def encode(self, schema_id: int, value: Any) -> bytes:
        out = io.BytesIO()
        out.write(struct.pack(">bI", MAGIC, schema_id))
        _fastavro().schemaless_writer(out, self.schema(schema_id), value)
        return out.getvalue()


# ── Avro schemas for rows ───────────────────────────────────────────────


def _avro_type(duck_type: str) -> Any:
    t = str(duck_type).upper()
    if t == "BOOLEAN":
        return "boolean"
    if t in ("TINYINT", "SMALLINT", "INTEGER", "UTINYINT", "USMALLINT"):
        return "int"
    if t in ("BIGINT", "UINTEGER"):
        return "long"
    if t in ("FLOAT", "REAL"):
        return "float"
    if t == "DOUBLE":
        return "double"
    found = re.match(r"DECIMAL\((\d+),\s*(\d+)\)", t)
    if found:
        return {"type": "bytes", "logicalType": "decimal",
                "precision": int(found.group(1)), "scale": int(found.group(2))}
    if t == "DATE":
        return {"type": "int", "logicalType": "date"}
    if t in ("TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"):
        return {"type": "long", "logicalType": "timestamp-micros"}
    if t.startswith("TIMESTAMP"):
        return {"type": "long", "logicalType": "local-timestamp-micros"}
    if t == "TIME":
        return {"type": "long", "logicalType": "time-micros"}
    if t == "BLOB":
        return "bytes"
    if t == "UUID":
        return {"type": "string", "logicalType": "uuid"}
    return "string"


def avro_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(text))
    return name if re.match(r"^[A-Za-z_]", name) else f"_{name}"


def record_schema(topic: str, columns: list[str], types: list[str]) -> tuple[dict, dict[str, str]]:
    """An Avro record schema for these columns, and column -> Avro field name."""
    fields = []
    names: dict[str, str] = {}
    for column, kind in zip(columns, types):
        name = avro_name(column)
        if name in names.values():
            raise ValueError(
                f"Kafka Sink: the columns '{column}' and '{next(c for c, n in names.items() if n == name)}' "
                f"both become the Avro field '{name}'. Rename one of them before this step."
            )
        names[column] = name
        fields.append({"name": name, "type": ["null", _avro_type(kind)], "default": None})
    return {"type": "record", "name": avro_name(topic) + "_value", "namespace": "revoluzen.flow",
            "fields": fields}, names
