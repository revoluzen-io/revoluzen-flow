"""PostgreSQL change data capture, read straight from the database (2026-11-13).

No Debezium and no Kafka: PostgreSQL's own logical decoding, with the
``pgoutput`` plugin every PostgreSQL 10+ ships, through ordinary SQL calls.

How a run reads
---------------
The step owns one replication slot (named after the pipeline and step) and one
publication (the tables it captures). A run asks the database for the changes
the slot holds with ``pg_logical_slot_peek_binary_changes``, which leaves them
in the slot, decodes them (``connectors/pgoutput.py``) and turns each insert,
update, delete and truncate into a row. Only after the whole run has succeeded,
destinations included, does Flow move the slot past those changes
(``pg_replication_slot_advance``). A failed, stopped or Sample run moves
nothing, so the next run gets the same changes again: a change can arrive
twice after a failure, but none is lost. The slot lives in the database, so it
does not matter which Flow server or worker runs the next batch.

A transaction is never split: a run reads whole transactions until it has at
least Max Changes changes. Very large transactions are read whole.

First run
---------
The first real run creates the publication (when Flow may) and the slot. With
"First run starts at: copy the tables", it then copies every row of the tables
(``_op = 'snapshot'``) followed by the changes made since the slot was created;
a row changed during the copy can appear in both, which applying the changes
by key settles. With "only new changes" it reads changes from the slot's
creation on. Sample runs and Test Node never create anything; they show the
waiting changes, or a sample of the table's rows when there is no slot yet.

Output
------
Every column of the table, plus ``_op`` (insert, update, delete, truncate,
snapshot), ``_table`` (schema.table), ``_lsn`` (the change's place in the
database log, a number that grows), ``_committed_at`` (when its transaction
committed, UTC), ``_txid`` and ``_unchanged`` (columns PostgreSQL did not send
because their large value did not change; empty otherwise). A delete carries
the key columns (every column with REPLICA IDENTITY FULL).
"""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

WHO = "Change Data Capture (PostgreSQL)"

META_TYPES = {
    "_op": "VARCHAR",
    "_table": "VARCHAR",
    "_lsn": "BIGINT",
    "_committed_at": "TIMESTAMPTZ",
    "_txid": "BIGINT",
    "_unchanged": "VARCHAR",
}

# PostgreSQL type id -> DuckDB type. Anything else arrives as text.
_SIMPLE = {
    16: "BOOLEAN", 20: "BIGINT", 21: "SMALLINT", 23: "INTEGER", 26: "BIGINT",
    700: "FLOAT", 701: "DOUBLE", 1082: "DATE", 1083: "TIME", 1114: "TIMESTAMP",
    1184: "TIMESTAMPTZ", 17: "BLOB",
}
_NUMERIC = 1700
_PG_EPOCH = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def duck_type(type_oid: int, type_modifier: int) -> str:
    """The DuckDB type for a PostgreSQL column."""
    if type_oid in _SIMPLE:
        return _SIMPLE[type_oid]
    if type_oid == _NUMERIC:
        if type_modifier is not None and type_modifier >= 4:
            precision = ((type_modifier - 4) >> 16) & 0xFFFF
            scale = (type_modifier - 4) & 0xFFFF
            if 0 < precision <= 38 and scale <= precision:
                return f"DECIMAL({precision},{scale})"
        # A numeric with no declared size, as DuckDB's own PostgreSQL reader does.
        return "DOUBLE"
    return "VARCHAR"


def from_text(text: Any, type_oid: int) -> Any:
    """A value pgoutput sent as text, as the Python value of its type."""
    if text is None or not isinstance(text, str):
        return text
    try:
        if type_oid == 16:
            return text == "t"
        if type_oid in (20, 21, 23, 26):
            return int(text)
        if type_oid == 1700:
            value = decimal.Decimal(text)
            return value if value.is_finite() else None
        if type_oid == 17 and text.startswith("\\x"):
            return bytes.fromhex(text[2:])
    except (ValueError, decimal.InvalidOperation):
        return text
    # Floats, dates and times stay as PostgreSQL's ISO text; DuckDB reads them
    # into the column's type (including NaN and infinity).
    return text


def lsn_text(value: int) -> str:
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"


def lsn_number(text: str) -> int:
    high, low = str(text).split("/")
    return (int(high, 16) << 32) + int(low, 16)


def default_name(workflow_id: str, step_id: str) -> str:
    """A slot / publication name unique to one pipeline step."""
    digest = hashlib.sha1(f"{workflow_id}:{step_id}".encode("utf-8")).hexdigest()[:16]
    return f"flow_{digest}"


def _check_name(value: str, what: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,63}", value):
        raise ValueError(
            f"{WHO}: the {what} name '{value}' can only use lower-case letters, digits and "
            f"underscores, up to 63 characters."
        )
    return value


def parse_tables(value: Any, default_schema: str = "public") -> list[tuple[str, str]]:
    """``[(schema, table)]`` from "orders, sales.items" or a list."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\n]", str(value or ""))
    out: list[tuple[str, str]] = []
    for item in items:
        name = str(item or "").strip()
        if not name:
            continue
        parts = [p.strip().strip('"') for p in name.split(".")]
        if len(parts) == 1:
            parts = [default_schema or "public", parts[0]]
        if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
            raise ValueError(
                f"{WHO}: '{name}' is not a table name Flow can capture. Write schema.table, "
                f"for example public.orders."
            )
        if tuple(parts) not in out:
            out.append((parts[0], parts[1]))
    return out


def publication_tables(cur, publication: str) -> list[tuple[str, str]]:
    """The tables an existing publication holds (for a step that names only it)."""
    cur.execute(
        "SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = %s "
        "ORDER BY schemaname, tablename",
        (publication,),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def column_union(columns: dict) -> dict[str, str]:
    """Every captured table's columns and the change columns, with one type each.

    A column name two tables share with different types becomes text.
    """
    types: dict[str, str] = {}
    for _table, cols in columns.items():
        for name, oid, mod in cols:
            kind = duck_type(oid, mod)
            if types.setdefault(name, kind) != kind:
                types[name] = "VARCHAR"
    types.update(META_TYPES)
    return types


@dataclass
class Batch:
    """What one run read."""

    rows: list[dict] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    end_lsn: int | None = None
    changes: int = 0
    copied: int = 0
    copy_query: list[tuple[str, str, list[str]]] = field(default_factory=list)


class PgChangeReader:
    """One run's view of a PostgreSQL slot. ``connect`` is psycopg2-like."""

    def __init__(self, config: dict, slot: str, publication: str,
                 tables: list[tuple[str, str]], connect=None, timeout_seconds: int = 120) -> None:
        self.config = dict(config or {})
        self.slot = _check_name(slot, "replication slot")
        self.publication = _check_name(publication, "publication")
        self.tables = tables
        self.timeout_seconds = max(int(timeout_seconds or 120), 5)
        self._connect = connect
        self.where = f"{self.config.get('host') or 'localhost'}:{self.config.get('port') or 5432}"

    # ── Connection ───────────────────────────────────────────────────

    def open(self):
        """An autocommit connection with UTC times and ISO dates."""
        connect = self._connect
        if connect is None:
            try:
                import psycopg2  # type: ignore
            except ImportError:
                from revoluzen_flow.connections.addons import driver_message

                raise ValueError(f"{WHO}: {driver_message('PostgreSQL', 'psycopg2-binary')}") from None
            connect = psycopg2.connect
        kwargs = {
            "host": self.config.get("host") or "localhost",
            "port": int(self.config.get("port") or 5432),
            "dbname": self.config.get("database") or self.config.get("dbname") or "postgres",
            "user": self.config.get("username") or self.config.get("user"),
            "password": self.config.get("password"),
            "connect_timeout": 10,
            "application_name": "revoluzen-flow-cdc",
        }
        if str(self.config.get("ssl") or "").lower() in ("1", "true", "yes", "on"):
            kwargs["sslmode"] = "require"
        try:
            conn = connect(**kwargs)
        except Exception as exc:  # noqa: BLE001 — drivers raise their own types
            raise ValueError(f"{WHO}: could not connect to PostgreSQL at {self.where}: {_first_line(exc)}") from None
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SET DateStyle TO ISO")
        cur.execute("SET statement_timeout = %s", (f"{self.timeout_seconds}s",))
        return conn

    # ── Checks and setup ─────────────────────────────────────────────

    def check_server(self, cur) -> None:
        cur.execute("SHOW wal_level")
        level = (cur.fetchone() or [""])[0]
        if str(level).lower() != "logical":
            raise ValueError(
                f"{WHO}: PostgreSQL at {self.where} does not keep enough in its change log "
                f"(wal_level is '{level}'). Ask the database administrator to set wal_level = "
                f"logical and restart the database. On Amazon RDS and Aurora that is the "
                f"parameter rds.logical_replication = 1; on Azure, wal_level = logical; on "
                f"Google Cloud SQL, the flag cloudsql.logical_decoding = on."
            )

    def table_columns(self, cur) -> dict[tuple[str, str], list[tuple[str, int, int]]]:
        """``{(schema, table): [(column, type id, type modifier)]}``; refuses unsafe tables."""
        out: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
        for schema, table in self.tables:
            cur.execute(
                "SELECT c.oid, c.relreplident, "
                "EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')",
                (schema, table),
            )
            found = cur.fetchone()
            if not found:
                raise ValueError(f"{WHO}: table {schema}.{table} was not found at {self.where}.")
            oid, replident, has_key = found
            replident = replident.decode() if isinstance(replident, (bytes, bytearray)) else str(replident)
            if replident == "n" or (replident == "d" and not has_key):
                raise ValueError(
                    f"{WHO}: {schema}.{table} has no primary key. Capturing its changes would "
                    f"stop updates and deletes on it in the database. Ask the database "
                    f"administrator to add a primary key, or to run: ALTER TABLE "
                    f"{_qident(schema)}.{_qident(table)} REPLICA IDENTITY FULL;"
                )
            cur.execute(
                "SELECT attname, atttypid, atttypmod FROM pg_attribute WHERE attrelid = %s "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                (oid,),
            )
            out[(schema, table)] = [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]
        return out

    def ensure_publication(self, cur, may_create: bool) -> None:
        cur.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (self.publication,))
        exists = cur.fetchone() is not None
        names = ", ".join(f"{_qident(s)}.{_qident(t)}" for s, t in self.tables)
        if not exists:
            if not may_create:
                raise ValueError(
                    f"{WHO}: the publication '{self.publication}' does not exist yet. A full run "
                    f"creates it, or ask the database administrator to run: CREATE PUBLICATION "
                    f"{self.publication} FOR TABLE {names};"
                )
            try:
                cur.execute(f"CREATE PUBLICATION {self.publication} FOR TABLE {names}")
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"{WHO}: Flow could not create the publication '{self.publication}' "
                    f"({_first_line(exc)}). Ask the database administrator to run: CREATE "
                    f"PUBLICATION {self.publication} FOR TABLE {names}; and enter that "
                    f"publication's name in the step."
                ) from None
            return
        cur.execute(
            "SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = %s",
            (self.publication,),
        )
        published = {(r[0], r[1]) for r in cur.fetchall()}
        missing = [t for t in self.tables if t not in published]
        if not missing:
            return
        add = ", ".join(f"{_qident(s)}.{_qident(t)}" for s, t in missing)
        if not may_create:
            raise ValueError(
                f"{WHO}: the publication '{self.publication}' does not include "
                f"{', '.join(f'{s}.{t}' for s, t in missing)}. A full run adds them, or ask the "
                f"database administrator to run: ALTER PUBLICATION {self.publication} ADD TABLE {add};"
            )
        try:
            cur.execute(f"ALTER PUBLICATION {self.publication} ADD TABLE {add}")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"{WHO}: the publication '{self.publication}' does not include "
                f"{', '.join(f'{s}.{t}' for s, t in missing)}, and Flow could not add them "
                f"({_first_line(exc)}). Ask the database administrator to run: ALTER PUBLICATION "
                f"{self.publication} ADD TABLE {add};"
            ) from None

    def slot_state(self, cur) -> dict | None:
        try:
            cur.execute(
                "SELECT plugin, database, active, confirmed_flush_lsn::text, wal_status "
                "FROM pg_replication_slots WHERE slot_name = %s",
                (self.slot,),
            )
        except Exception:  # noqa: BLE001 — PostgreSQL 11 and 12 have no wal_status
            cur.execute(
                "SELECT plugin, database, active, confirmed_flush_lsn::text, NULL "
                "FROM pg_replication_slots WHERE slot_name = %s",
                (self.slot,),
            )
        row = cur.fetchone()
        if not row:
            return None
        plugin, database, active, confirmed, wal_status = row
        dbname = self.config.get("database") or self.config.get("dbname") or "postgres"
        if plugin != "pgoutput" or (database and database != dbname):
            raise ValueError(
                f"{WHO}: a replication slot named '{self.slot}' already exists at {self.where} "
                f"for another use (plugin {plugin}, database {database}). Choose another slot name."
            )
        if wal_status == "lost":
            raise ValueError(
                f"{WHO}: the database removed the changes the slot '{self.slot}' still needed "
                f"(it went too long without a run, past max_slot_wal_keep_size). Reset the step's "
                f"read position to start again; changes made in between cannot be read."
            )
        return {"active": bool(active), "confirmed": confirmed}

    def create_slot(self, cur) -> None:
        try:
            cur.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (self.slot,))
        except Exception as exc:  # noqa: BLE001
            text = _first_line(exc)
            if "already exists" in text:
                return
            if "permission" in text.lower() or "must be superuser" in text.lower() or "replication role" in text.lower():
                user = self.config.get("username") or self.config.get("user") or "this user"
                raise ValueError(
                    f"{WHO}: the database user '{user}' may not read the change log. Ask the "
                    f"database administrator to run: ALTER ROLE {_qident(str(user))} WITH REPLICATION; "
                    f"(on Amazon RDS: GRANT rds_replication TO {_qident(str(user))};)."
                ) from None
            if "replication slots are in use" in text or "max_replication_slots" in text:
                raise ValueError(
                    f"{WHO}: every replication slot at {self.where} is in use. Ask the database "
                    f"administrator to raise max_replication_slots, or remove slots no one reads."
                ) from None
            raise ValueError(f"{WHO}: could not create the replication slot '{self.slot}': {text}") from None

    # ── Reading ──────────────────────────────────────────────────────

    def peek(self, cur, max_changes: int, columns: dict) -> Batch:
        """Decode up to ``max_changes`` waiting changes (whole transactions)."""
        from revoluzen_flow.connectors.pgoutput import PgoutputDecoder

        try:
            cur.execute(
                "SELECT lsn::text, data FROM pg_logical_slot_peek_binary_changes(%s, NULL, %s, "
                "'proto_version', '1', 'publication_names', %s)",
                (self.slot, int(max_changes), self.publication),
            )
            raw = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            text = _first_line(exc)
            if "is active for PID" in text:
                raise ValueError(
                    f"{WHO}: another run of this pipeline is reading these changes right now. "
                    f"This run stopped; the next run picks them up."
                ) from None
            if "statement timeout" in text or "canceling statement" in text:
                raise ValueError(
                    f"{WHO}: reading the waiting changes took longer than {self.timeout_seconds} "
                    f"seconds. Raise the step's Timeout, or run the pipeline more often."
                ) from None
            raise ValueError(f"{WHO}: could not read the changes from slot '{self.slot}': {text}") from None

        decoder = PgoutputDecoder()
        batch = Batch()
        wanted = {f"{s}.{t}" for s, t in self.tables}
        types = column_union(columns)
        oids: dict[int, dict[str, int]] = {}
        pending: list[dict] = []
        committed_at = None
        txid = None
        for lsn, data in raw:
            event = decoder.decode(bytes(data))
            if event is None:
                continue
            op = event["op"]
            if op == "B":
                pending = []
                committed_at = _PG_EPOCH + _dt.timedelta(microseconds=int(event["timestamp_us"]))
                txid = int(event["xid"])
                continue
            if op == "R":
                oids[event["relation_id"]] = {c["name"]: int(c["type_oid"]) for c in event["columns"]}
                for c in event["columns"]:
                    # A column the table has since dropped, or one a table
                    # shares with a different type, arrives as text.
                    kind = duck_type(int(c["type_oid"]), int(c["type_modifier"]))
                    if types.setdefault(c["name"], kind) != kind:
                        types[c["name"]] = "VARCHAR"
                continue
            if op == "C":
                for row in pending:
                    row["_committed_at"] = committed_at
                    row["_txid"] = txid
                batch.rows.extend(pending)
                batch.changes += len(pending)
                pending = []
                batch.end_lsn = lsn_number(lsn)
                continue
            if op == "T":
                for item in event.get("namespaces") or []:
                    name = f"{item.get('namespace')}.{item.get('name')}"
                    if name in wanted:
                        pending.append({"_op": "truncate", "_table": name, "_lsn": lsn_number(lsn),
                                        "_unchanged": ""})
                continue
            if op not in ("I", "U", "D"):
                continue
            name = f"{event.get('namespace')}.{event.get('name')}"
            if name not in wanted:
                continue
            values = event.get("after") if op != "D" else event.get("before")
            row: dict[str, Any] = {}
            unchanged: list[str] = []
            col_oids = oids.get(event["relation_id"], {})
            for col, value in (values or {}).items():
                if isinstance(value, dict) and value.get("__unchanged_toast"):
                    unchanged.append(col)
                    row[col] = None
                else:
                    row[col] = from_text(value, col_oids.get(col, 25))
            row["_op"] = {"I": "insert", "U": "update", "D": "delete"}[op]
            row["_table"] = name
            row["_lsn"] = lsn_number(lsn)
            row["_unchanged"] = ",".join(unchanged)
            pending.append(row)
        batch.types = types
        return batch

    def sample_rows(self, cur, columns: dict, limit: int) -> list[dict]:
        """A few current rows of each table, for Sample runs and Test Node."""
        rows: list[dict] = []
        for (schema, table), cols in columns.items():
            names = [c[0] for c in cols]
            if not names:
                continue
            cur.execute(
                f"SELECT {', '.join(_qident(n) + '::text' for n in names)} FROM "
                f"{_qident(schema)}.{_qident(table)} LIMIT %s",
                (int(limit),),
            )
            oids = {c[0]: c[1] for c in cols}
            for values in cur.fetchall():
                row = {n: from_text(v, oids[n]) for n, v in zip(names, values)}
                row.update({"_op": "snapshot", "_table": f"{schema}.{table}", "_unchanged": ""})
                rows.append(row)
        return rows

    def copy_tables(self, conn, columns: dict, add) -> int:
        """Every row of every table, streamed to ``add`` as snapshot rows."""
        total = 0
        for (schema, table), cols in columns.items():
            names = [c[0] for c in cols]
            if not names:
                continue
            oids = {c[0]: c[1] for c in cols}
            # A named (server-side) cursor streams the table instead of
            # loading it whole.
            conn.autocommit = False
            setup = conn.cursor()
            # A whole table can take longer than the step's Timeout.
            setup.execute("SET LOCAL statement_timeout = 0")
            cur = conn.cursor(name=f"flow_copy_{abs(hash((schema, table))) % 10 ** 8}")
            try:
                cur.itersize = 5000
                cur.execute(
                    f"SELECT {', '.join(_qident(n) + '::text' for n in names)} FROM "
                    f"{_qident(schema)}.{_qident(table)}"
                )
                for values in cur:
                    row = {n: from_text(v, oids[n]) for n, v in zip(names, values)}
                    row.update({"_op": "snapshot", "_table": f"{schema}.{table}", "_unchanged": ""})
                    add(row)
                    total += 1
            finally:
                try:
                    cur.close()
                finally:
                    conn.rollback()
                    conn.autocommit = True
        return total

    def advance(self, end_lsn: int, attempts: int = 5) -> None:
        """Move the slot past ``end_lsn`` (done only after the run succeeded)."""
        conn = self.open()
        try:
            cur = conn.cursor()
            for attempt in range(attempts):
                try:
                    cur.execute("SELECT pg_replication_slot_advance(%s, %s::pg_lsn)",
                                (self.slot, lsn_text(end_lsn)))
                    return
                except Exception as exc:  # noqa: BLE001
                    if "is active for PID" in str(exc) and attempt < attempts - 1:
                        time.sleep(1 + attempt)
                        continue
                    raise
        finally:
            conn.close()

    def drop(self, drop_publication: bool) -> bool:
        """Remove the slot (and the publication Flow made). True when the slot was there."""
        conn = self.open()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                "WHERE slot_name = %s", (self.slot,),
            )
            dropped = cur.rowcount > 0
            if drop_publication:
                cur.execute(f"DROP PUBLICATION IF EXISTS {self.publication}")
            return dropped
        finally:
            conn.close()


def _first_line(exc: Exception) -> str:
    text = str(getattr(exc, "pgerror", None) or exc).strip()
    if text.startswith("ERROR:"):
        text = text[6:].strip()
    return text.splitlines()[0] if text else type(exc).__name__


# ── One step's run ───────────────────────────────────────────────────────


def position_key(slot: str) -> str:
    return f"pgcdc:{slot}"


def names_for(ctx: Any, params: dict, step_id: str) -> tuple[str, str] | None:
    """(slot, publication) of a step in a saved pipeline; None for Test Node."""
    from revoluzen_flow.streams.positions import owner

    who = owner(ctx, step_id)
    if who is None:
        return None
    base = default_name(*who)
    # ``slot`` is the name steps saved before 2026-11-13 used.
    slot = str(params.get("slot_name") or params.get("slot") or "").strip() or base
    publication = str(params.get("publication") or "").strip() or base
    return slot, publication


def _tables_or_publication(reader: "PgChangeReader", cur, named: str) -> None:
    """With no tables named, capture every table of the step's own publication."""
    if reader.tables:
        return
    found = publication_tables(cur, reader.publication) if named else []
    if not found:
        raise ValueError(
            f"{WHO}: name at least one table to capture, for example public.orders, or the "
            f"name of an existing publication."
        )
    reader.tables = found


def read_postgres(ctx: Any, params: dict, config: dict, step_id: str, connect=None) -> Any:
    """Run one Change Data Capture step against PostgreSQL; returns a relation."""
    from revoluzen_flow.streams import positions
    from revoluzen_flow.streams.rows import RowsWriter, rows_to_relation

    tables = parse_tables(params.get("tables"), str(config.get("schema") or "public"))
    max_changes = _positive(params.get("max_changes"), 10000)
    timeout = _positive(params.get("timeout_seconds"), 120)
    start_from = str(params.get("start_from") or "new").strip().lower()
    if start_from not in ("new", "copy"):
        raise ValueError(f"{WHO}: 'First run starts at' is '{start_from}'. Choose new or copy.")
    names = names_for(ctx, params, step_id)
    preview = bool(getattr(ctx, "preview_mode", False) or getattr(ctx, "sandboxed", False))
    sample = _positive(getattr(ctx, "sample_row_limit", None), 100)

    named_publication = str(params.get("publication") or "").strip()
    if names is None:
        # Test Node: no pipeline, so no slot of its own. Show current rows.
        reader = PgChangeReader(config, "flow_preview", named_publication or "flow_preview",
                                tables, connect, timeout)
        conn = reader.open()
        try:
            cur = conn.cursor()
            _tables_or_publication(reader, cur, named_publication)
            columns = reader.table_columns(cur)
            return rows_to_relation(ctx.conn, reader.sample_rows(cur, columns, sample),
                                    "__cdc_rows", fixed=column_union(columns))
        finally:
            conn.close()

    slot, publication = names
    reader = PgChangeReader(config, slot, publication, tables, connect, timeout)
    saved = positions.load(ctx, step_id, position_key(slot), label=WHO)
    conn = reader.open()
    try:
        cur = conn.cursor()
        reader.check_server(cur)
        _tables_or_publication(reader, cur, named_publication)
        columns = reader.table_columns(cur)
        state = reader.slot_state(cur)
        if state is not None and state["active"]:
            raise ValueError(
                f"{WHO}: another run of this pipeline is reading these changes right now. "
                f"This run stopped; the next run picks them up."
            )
        if preview:
            # Sample runs and sandbox runs read but never create or move anything.
            if state is None:
                return rows_to_relation(ctx.conn, reader.sample_rows(cur, columns, sample),
                                        "__cdc_rows", fixed=column_union(columns))
            reader.ensure_publication(cur, may_create=False)
            batch = reader.peek(cur, min(max_changes, max(sample, 1)), columns)
            return rows_to_relation(ctx.conn, batch.rows, "__cdc_rows", fixed=batch.types)

        if state is None and saved.value:
            raise ValueError(
                f"{WHO}: the replication slot '{slot}' that held this step's place is gone "
                f"(it was removed in the database). Changes made since then cannot be read. "
                f"Reset the step's read position to start again."
            )
        reader.ensure_publication(cur, may_create=True)
        if state is None:
            reader.create_slot(cur)
        first_run = not saved.value
        batch = reader.peek(cur, max_changes, columns)
        writer = RowsWriter(ctx.conn, batch.types, "__cdc_rows")
        try:
            copied = 0
            whole_copy = True
            if first_run and start_from == "copy":
                if getattr(ctx, "full_run", True):
                    copied = reader.copy_tables(conn, columns, writer.add)
                else:
                    # A capped run copies only a sample and keeps no place, so
                    # the first full run makes the whole copy.
                    for row in reader.sample_rows(cur, columns, sample):
                        writer.add(row)
                    whole_copy = False
            for row in batch.rows:
                writer.add(row)
            relation = writer.relation()
        except BaseException:
            writer.discard()
            raise
    finally:
        conn.close()

    if not whole_copy:
        return relation
    value = {
        "slot": slot,
        "publication": publication,
        "lsn": lsn_text(batch.end_lsn) if batch.end_lsn is not None else saved.value.get("lsn"),
        "tables": [f"{s}.{t}" for s, t in reader.tables],
        "copied": bool(saved.value.get("copied")) or copied > 0,
    }
    pending = SlotAdvance(
        reader=reader, end_lsn=batch.end_lsn, step_id=step_id,
        record=positions.pending_position(ctx, step_id, saved, value, batch.changes + copied, label=WHO),
    )
    queue = getattr(ctx, "pending_sync_cursors", None)
    if queue is not None:
        queue.append(pending)
    else:
        pending.commit()
    return relation


class SlotAdvance:
    """After a successful run: move the slot past what the run read, then
    record the place (shown in the step's form, and how Reset knows it)."""

    def __init__(self, reader: PgChangeReader, end_lsn: int | None, step_id: str, record: Any) -> None:
        self.reader = reader
        self.end_lsn = end_lsn
        self.step_id = step_id
        self.record = record

    def commit(self) -> bool:
        if self.end_lsn is not None:
            try:
                self.reader.advance(self.end_lsn)
            except Exception as exc:  # noqa: BLE001 — the run's data is already written
                logger.warning(
                    "%s step %s: the run succeeded, but the slot '%s' could not be moved past "
                    "its changes (%s); the next run reads them again.",
                    WHO, self.step_id, self.reader.slot, exc,
                )
                return False
        if self.record is not None:
            self.record.commit()
        return True


def drop_for_step(params: dict, config: dict, workflow_id: str, step_id: str) -> bool:
    """Remove a step's slot, and its publication when Flow named it.

    For Reset and for deleting a pipeline: a slot nobody reads keeps the
    database's change log from being cleaned up until its disk fills.
    """
    base = default_name(workflow_id, step_id)
    slot = str(params.get("slot_name") or "").strip() or base
    publication = str(params.get("publication") or "").strip() or base
    reader = PgChangeReader(config, slot, publication, [("public", "x")], None, 30)
    return reader.drop(drop_publication=publication == base)


def _positive(value: Any, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
