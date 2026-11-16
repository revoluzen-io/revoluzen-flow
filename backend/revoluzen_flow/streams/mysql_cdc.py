"""MySQL and MariaDB change data capture, from the binary log (2026-11-13).

The server must keep a row-based binary log (``log_bin`` on,
``binlog_format = ROW``, ``binlog_row_image = FULL``) and the connection's user
needs ``REPLICATION SLAVE`` and ``REPLICATION CLIENT`` (MariaDB 10.5+: also
``BINLOG MONITOR``) plus ``SELECT`` on the tables. Flow reads the log the way a
replica does, with the ``mysql-replication`` package (an add-on), and never
writes to the server.

Where a step carries on is a place in the binary log (file and position),
which Flow keeps for the step in its metastore and saves only after the whole
run has succeeded, and only if no other run moved it. A run reads until it has
Max Changes changes or its Timeout passes, and always stops at the end of a
transaction, so a transaction is never split. A change can arrive twice after
a failure, but none is lost. When the server has already removed the log file
that holds the step's place (``binlog_expire_logs_seconds``), the run says so;
Reset starts the step again.

First run: "new" starts at the end of the log as it is then; "copy" first
copies every row of the tables (``_op = 'snapshot'``), then reads the changes
made since. Each row carries the table's columns plus ``_op``, ``_table``
(database.table), ``_position`` (log file and position; sorts in log order),
``_committed_at`` and ``_seq``.

Column names come from the log when the server writes them
(``binlog_row_metadata = FULL``, MySQL 8.0+, MariaDB 10.5+), otherwise from the
server's catalog, which is right unless the table's columns changed since the
change was made.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import random
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

WHO = "Change Data Capture (MySQL / MariaDB)"
META_TYPES = {
    "_op": "VARCHAR", "_table": "VARCHAR", "_position": "VARCHAR",
    "_committed_at": "TIMESTAMPTZ", "_seq": "BIGINT",
}
_IDENT = re.compile(r"^[A-Za-z0-9_$]+$")


def parse_tables(value: Any, default_database: str) -> list[tuple[str, str]]:
    """``[(database, table)]`` from "orders, shop.items" or a list."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\n]", str(value or ""))
    out: list[tuple[str, str]] = []
    for item in items:
        name = str(item or "").strip()
        if not name:
            continue
        parts = [p.strip().strip("`") for p in name.split(".")]
        if len(parts) == 1:
            parts = [default_database, parts[0]]
        if len(parts) != 2 or not all(p and _IDENT.match(p) for p in parts):
            raise ValueError(
                f"{WHO}: '{name}' is not a table name Flow can capture. Write database.table, "
                f"for example shop.orders."
            )
        if tuple(parts) not in out:
            out.append((parts[0], parts[1]))
    if not out:
        raise ValueError(f"{WHO}: name at least one table to capture, for example shop.orders.")
    return out


def duck_type(data_type: str, column_type: str, precision: Any, scale: Any) -> str:
    """The DuckDB type for a MySQL / MariaDB column."""
    t = (data_type or "").lower()
    unsigned = "unsigned" in (column_type or "").lower()
    if t == "tinyint":
        return "SMALLINT" if unsigned else "TINYINT"
    if t == "smallint":
        return "INTEGER" if unsigned else "SMALLINT"
    if t in ("mediumint", "int", "integer"):
        return "BIGINT" if unsigned and t != "mediumint" else "INTEGER"
    if t == "bigint":
        return "DECIMAL(20,0)" if unsigned else "BIGINT"
    if t in ("decimal", "numeric"):
        p, s = int(precision or 10), int(scale or 0)
        return f"DECIMAL({p},{s})" if 0 < p <= 38 else "DOUBLE"
    if t == "float":
        return "FLOAT"
    if t in ("double", "real"):
        return "DOUBLE"
    if t == "bit":
        return "BIGINT"
    if t == "year":
        return "INTEGER"
    if t == "date":
        return "DATE"
    if t == "datetime":
        return "TIMESTAMP"
    if t == "timestamp":
        return "TIMESTAMPTZ"
    if t in ("binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"):
        return "BLOB"
    return "VARCHAR"


def _value(value: Any, kind: str) -> Any:
    """A value the binlog reader gives, as the row keeps it."""
    if value is None:
        return None
    if isinstance(value, _dt.timedelta):  # TIME can pass 24 hours
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    if isinstance(value, (set, frozenset)):
        return ",".join(sorted(str(v) for v in value))
    if isinstance(value, (dict, list)):
        return json.dumps(_plain(value), default=str)
    if isinstance(value, _dt.datetime) and kind == "TIMESTAMPTZ" and value.tzinfo is None:
        return value.replace(tzinfo=_dt.timezone.utc)  # the reader gives TIMESTAMP in UTC
    if isinstance(value, bytes) and kind == "VARCHAR":
        return value.decode("utf-8", errors="replace")
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {(k.decode() if isinstance(k, bytes) else k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _position(log_file: str, log_pos: int) -> str:
    return f"{log_file}:{int(log_pos):020d}"


class MysqlChangeReader:
    """One run's view of a MySQL or MariaDB binary log."""

    def __init__(self, config: dict, tables: list[tuple[str, str]], timeout_seconds: int = 120,
                 connect=None, stream_class=None) -> None:
        self.config = dict(config or {})
        self.tables = tables
        self.timeout_seconds = max(int(timeout_seconds or 120), 5)
        self._connect = connect
        self._stream_class = stream_class
        self.where = f"{self.config.get('host') or 'localhost'}:{self.config.get('port') or 3306}"
        self.is_mariadb = False

    def settings(self) -> dict:
        return {
            "host": self.config.get("host") or "localhost",
            "port": int(self.config.get("port") or 3306),
            "user": self.config.get("username") or self.config.get("user") or "",
            "passwd": self.config.get("password") or "",
        }

    def open(self):
        connect = self._connect
        if connect is None:
            try:
                import pymysql  # type: ignore
            except ImportError:
                from revoluzen_flow.connections.addons import driver_message

                raise ValueError(f"{WHO}: {driver_message('MySQL', 'PyMySQL')}") from None
            connect = pymysql.connect
        s = self.settings()
        try:
            conn = connect(host=s["host"], port=s["port"], user=s["user"], password=s["passwd"],
                           connect_timeout=10, autocommit=True, charset="utf8mb4")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{WHO}: could not connect to {self.where}: {exc}") from None
        return conn

    def check_server(self, cur) -> None:
        cur.execute("SELECT VERSION()")
        self.is_mariadb = "mariadb" in str((cur.fetchone() or [""])[0]).lower()
        cur.execute("SHOW VARIABLES WHERE Variable_name IN ('log_bin', 'binlog_format', 'binlog_row_image')")
        found = {str(k).lower(): str(v) for k, v in cur.fetchall()}
        fixes = []
        if found.get("log_bin", "").upper() != "ON":
            fixes.append("turn the binary log on (log_bin)")
        if found.get("binlog_format", "").upper() != "ROW":
            fixes.append(f"set binlog_format = ROW (it is {found.get('binlog_format') or 'not set'})")
        if found.get("binlog_row_image", "FULL").upper() != "FULL":
            fixes.append(f"set binlog_row_image = FULL (it is {found.get('binlog_row_image')})")
        if fixes:
            raise ValueError(
                f"{WHO}: the server at {self.where} does not keep a log Flow can read changes from. "
                f"Ask the database administrator to {' and '.join(fixes)}. On Amazon RDS and Aurora "
                f"these are in the parameter group, and backups must be on."
            )

    def current_position(self, cur) -> tuple[str, int]:
        for sql in ("SHOW BINARY LOG STATUS", "SHOW MASTER STATUS"):
            try:
                cur.execute(sql)
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001 — MySQL 8.4 renamed it; older ones lack the new name
                text = str(exc)
                if "denied" in text.lower() or "privilege" in text.lower():
                    raise self._privileges() from None
                continue
            if row:
                return str(row[0]), int(row[1])
        raise self._privileges()

    def _privileges(self) -> ValueError:
        user = self.settings()["user"] or "this user"
        return ValueError(
            f"{WHO}: the user '{user}' may not read the binary log at {self.where}. Ask the database "
            f"administrator to run: GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{user}';"
            f"{' (MariaDB: also BINLOG MONITOR)' if self.is_mariadb else ''}"
        )

    def log_files(self, cur) -> list[str]:
        try:
            cur.execute("SHOW BINARY LOGS")
            return [str(r[0]) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            return []

    def table_columns(self, cur) -> dict[tuple[str, str], list[tuple[str, str]]]:
        out: dict[tuple[str, str], list[tuple[str, str]]] = {}
        named_members: list[str] = []
        for database, table in self.tables:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (database, table),
            )
            rows = cur.fetchall()
            if not rows:
                raise ValueError(f"{WHO}: table {database}.{table} was not found at {self.where}.")
            out[(database, table)] = [(r[0], duck_type(r[1], r[2], r[3], r[4])) for r in rows]
            named_members += [f"{database}.{table}.{r[0]}" for r in rows if str(r[1]).lower() in ("set", "enum")]
        if named_members:
            # The log holds an ENUM or SET value as a number; its names come
            # only from the column metadata the server writes with FULL.
            cur.execute("SHOW VARIABLES LIKE 'binlog_row_metadata'")
            found = cur.fetchone()
            if found and str(found[1]).upper() != "FULL":
                raise ValueError(
                    f"{WHO}: {', '.join(named_members[:3])} {'is an ENUM or SET column' if len(named_members) == 1 else 'are ENUM or SET columns'}, "
                    f"and the server at {self.where} does not write their value names into its log "
                    f"(binlog_row_metadata is {found[1]}). Ask the database administrator to set "
                    f"binlog_row_metadata = FULL; otherwise these values would arrive empty."
                )
        return out

    @staticmethod
    def types(columns: dict) -> dict[str, str]:
        types: dict[str, str] = {}
        for _table, cols in columns.items():
            for name, kind in cols:
                if types.setdefault(name, kind) != kind:
                    types[name] = "VARCHAR"
        types.update(META_TYPES)
        return types

    def read(self, start: tuple[str, int], max_changes: int, types: dict[str, str]) -> tuple[list[dict], tuple[str, int]]:
        """Changes after ``start``, whole transactions only, and where the read stopped."""
        stream_class = self._stream_class
        try:
            if stream_class is None:
                from pymysqlreplication import BinLogStreamReader as stream_class  # type: ignore
            from pymysqlreplication.event import QueryEvent, XidEvent  # type: ignore
            from pymysqlreplication.row_event import (  # type: ignore
                DeleteRowsEvent, UpdateRowsEvent, WriteRowsEvent,
            )
        except ImportError:
            from revoluzen_flow.connections.addons import addon_message

            raise ValueError(f"{WHO}: {addon_message('mysql_cdc')}") from None

        # A new replica id for every run, so two runs never push each other off
        # the server. ``is_mariadb`` stays off even on MariaDB: with it on, the
        # reader starts at the oldest log the server keeps instead of the
        # step's place (seen on MariaDB 11.4, mysql-replication 1.0.17); the
        # classic replica protocol positions correctly on MariaDB too.
        stream = stream_class(
            connection_settings=self.settings(),
            server_id=random.randint(1_000_000, 4_000_000_000),
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent, XidEvent, QueryEvent],
            only_schemas=sorted({d for d, _t in self.tables}),
            log_file=start[0], log_pos=int(start[1]), resume_stream=True, blocking=False,
            is_mariadb=False, use_column_name_cache=True,
        )
        wanted = {f"{d}.{t}" for d, t in self.tables}
        rows: list[dict] = []
        pending: list[dict] = []
        end = (start[0], int(start[1]))
        deadline = time.monotonic() + self.timeout_seconds
        try:
            for event in stream:
                if isinstance(event, (WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent)):
                    name = f"{event.schema}.{event.table}"
                    if name not in wanted:
                        continue
                    op = ("insert" if isinstance(event, WriteRowsEvent)
                          else "update" if isinstance(event, UpdateRowsEvent) else "delete")
                    at = _dt.datetime.fromtimestamp(int(event.timestamp), tz=_dt.timezone.utc)
                    # The event's own end position when the reader has it,
                    # else the reader's place (both point just past the event).
                    where = _position(stream.log_file,
                                      getattr(getattr(event, "packet", None), "log_pos", 0) or stream.log_pos or 0)
                    for item in event.rows:
                        values = item.get("after_values") if op == "update" else item.get("values")
                        row = {k: _value(v, types.get(k, "VARCHAR")) for k, v in (values or {}).items()}
                        row.update({"_op": op, "_table": name, "_position": where, "_committed_at": at})
                        pending.append(row)
                    continue
                boundary = isinstance(event, XidEvent) or (
                    isinstance(event, QueryEvent) and str(getattr(event, "query", "")).strip().upper() != "BEGIN"
                )
                if not boundary:
                    continue
                rows.extend(pending)
                pending = []
                end = (stream.log_file, int(stream.log_pos))
                if len(rows) >= max_changes or time.monotonic() > deadline:
                    break
        except Exception as exc:  # noqa: BLE001 — the reader raises pymysql's types
            text = str(exc)
            if "denied" in text.lower() or "privilege" in text.lower():
                raise self._privileges() from None
            if "could not find first log file" in text.lower() or "1236" in text:
                raise ValueError(
                    f"{WHO}: the server at {self.where} no longer keeps the log file {start[0]} that "
                    f"holds this step's place (it removes old logs after binlog_expire_logs_seconds). "
                    f"Changes made since then cannot be read. Reset the step's read position to start again."
                ) from None
            raise ValueError(f"{WHO}: reading the binary log at {self.where} failed: {text}") from None
        finally:
            stream.close()
        return rows, end

    def sample_rows(self, cur, columns: dict, limit: int) -> list[dict]:
        out: list[dict] = []
        for (database, table), cols in columns.items():
            kinds = dict(cols)
            cur.execute(f"SELECT * FROM `{database}`.`{table}` LIMIT %s", (int(limit),))
            names = [d[0] for d in cur.description]
            for values in cur.fetchall():
                row = {n: _value(v, kinds.get(n, "VARCHAR")) for n, v in zip(names, values)}
                row.update({"_op": "snapshot", "_table": f"{database}.{table}"})
                out.append(row)
        return out

    def copy_tables(self, conn, columns: dict, add) -> int:
        """Every row of every table, streamed to ``add`` as snapshot rows."""
        import pymysql  # type: ignore

        total = 0
        for (database, table), cols in columns.items():
            kinds = dict(cols)
            cur = conn.cursor(pymysql.cursors.SSCursor)
            try:
                cur.execute(f"SELECT * FROM `{database}`.`{table}`")
                names = [d[0] for d in cur.description]
                for values in cur:
                    row = {n: _value(v, kinds.get(n, "VARCHAR")) for n, v in zip(names, values)}
                    row.update({"_op": "snapshot", "_table": f"{database}.{table}"})
                    add(row)
                    total += 1
            finally:
                cur.close()
        return total


def position_key(tables: list[tuple[str, str]]) -> str:
    return "mysqlcdc:" + ",".join(f"{d}.{t}" for d, t in tables)


def read_mysql(ctx: Any, params: dict, config: dict, step_id: str, connect=None, stream_class=None) -> Any:
    """Run one Change Data Capture step against MySQL or MariaDB; returns a relation."""
    from revoluzen_flow.streams import positions
    from revoluzen_flow.streams.rows import RowsWriter, rows_to_relation

    tables = parse_tables(params.get("tables"), str(config.get("database") or ""))
    max_changes = _positive(params.get("max_changes"), 10000)
    timeout = _positive(params.get("timeout_seconds"), 120)
    start_from = str(params.get("start_from") or "new").strip().lower()
    if start_from not in ("new", "copy"):
        raise ValueError(f"{WHO}: 'First run starts at' is '{start_from}'. Choose new or copy.")
    preview = bool(getattr(ctx, "preview_mode", False) or getattr(ctx, "sandboxed", False))
    sample = _positive(getattr(ctx, "sample_row_limit", None), 100)
    in_pipeline = positions.owner(ctx, step_id) is not None

    reader = MysqlChangeReader(config, tables, timeout, connect, stream_class)
    saved = positions.load(ctx, step_id, position_key(tables), label=WHO)
    conn = reader.open()
    try:
        cur = conn.cursor()
        reader.check_server(cur)
        columns = reader.table_columns(cur)
        types = reader.types(columns)
        if not in_pipeline or (preview and not saved.value):
            # Test Node, or a Sample run before the first real run: current rows.
            return rows_to_relation(ctx.conn, reader.sample_rows(cur, columns, sample), "__cdc_rows", fixed=types)
        files = reader.log_files(cur)
        if saved.value:
            start = (str(saved.value.get("file")), int(saved.value.get("pos") or 4))
            if files and start[0] not in files:
                raise ValueError(
                    f"{WHO}: the server at {reader.where} no longer keeps the log file {start[0]} that "
                    f"holds this step's place. Changes made since then cannot be read. Reset the "
                    f"step's read position to start again."
                )
        else:
            start = reader.current_position(cur)
        first_run = not saved.value
        writer = RowsWriter(ctx.conn, types, "__cdc_rows")
        seq = 0
        try:
            copied = 0
            whole_copy = True
            if first_run and start_from == "copy" and not preview:
                def add(row: dict) -> None:
                    nonlocal seq
                    row["_seq"] = seq
                    seq += 1
                    writer.add(row)

                if getattr(ctx, "full_run", True):
                    copied = reader.copy_tables(conn, columns, add)
                else:
                    for row in reader.sample_rows(cur, columns, sample):
                        add(row)
                    whole_copy = False
            changes, end = reader.read(start, min(max_changes, sample) if preview else max_changes, types)
            for row in changes:
                row["_seq"] = seq
                seq += 1
                writer.add(row)
            relation = writer.relation()
        except BaseException:
            writer.discard()
            raise
    finally:
        conn.close()

    if preview or not whole_copy:
        return relation
    value = {"file": end[0], "pos": int(end[1]), "copied": bool(saved.value.get("copied")) or copied > 0}
    positions.keep(ctx, step_id, saved, value, len(changes) + copied, label=WHO)
    return relation


def _positive(value: Any, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
