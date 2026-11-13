"""
JDBC dialect registry for warehouse connectors.

Each dialect is a tiny adapter: build a connection (or DuckDB attach string),
read a table or run a query, and return rows. Drivers are imported lazily so
Revoluzen Flow never crashes if a vendor driver isn't installed — instead, the user
gets a plain message that names the missing package.

Dialects supported:
  snowflake, bigquery, redshift, databricks, mssql, oracle, mongodb, clickhouse
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class DialectInfo:
    name: str
    label: str
    driver_pkg: str
    install_hint: str
    reader: Callable[[dict[str, Any], str | None, str | None, int | None], tuple[list[str], list[tuple]]]
    writer: Callable[[dict[str, Any], str, list[str], list[tuple]], int] | None = None


_REGISTRY: dict[str, DialectInfo] = {}


def register_dialect(info: DialectInfo) -> None:
    _REGISTRY[info.name] = info


def get_dialect(name: str) -> DialectInfo:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown JDBC dialect '{name}'. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_dialects() -> list[dict]:
    return [{"name": d.name, "label": d.label, "install": d.install_hint} for d in _REGISTRY.values()]


_DRIVER_LABELS = {
    "snowflake.connector": "Snowflake", "google.cloud.bigquery": "BigQuery",
    "redshift_connector": "Redshift", "databricks.sql": "Databricks",
    "pyodbc": "SQL Server", "oracledb": "Oracle", "pymongo": "MongoDB",
    "clickhouse_connect": "ClickHouse",
}


def _import_or_raise(pkg: str, install_hint: str):
    try:
        return importlib.import_module(pkg)
    except ImportError as e:
        # 2026-11-11: plain wording, as in Test connection. "pip install" is a
        # dead end for someone using a packaged install.
        from revoluzen_flow.connections.addons import driver_message
        raise RuntimeError(driver_message(_DRIVER_LABELS.get(pkg, pkg), install_hint)) from e


# ─────────────────────────── Snowflake ───────────────────────────

# 2026-11-11: every reader opens its connection through
# connections/warehouse_clients.py, the same code Test connection and browsing
# use. Each reader used to have its own, and they had drifted: BigQuery ignored
# the saved service account key, ClickHouse the SSL box, Databricks the Catalog
# and Schema, and Redshift needed a driver no install ships.

def _snowflake_reader(cfg, query, table, limit):
    from revoluzen_flow.connections.warehouse_clients import snowflake_connect
    conn = snowflake_connect(cfg)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT * FROM ({sql}) LIMIT {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "snowflake", "Snowflake", "snowflake.connector", "snowflake-connector-python", _snowflake_reader,
))


# ─────────────────────────── BigQuery ───────────────────────────

def _bigquery_reader(cfg, query, table, limit):
    from revoluzen_flow.connections.warehouse_clients import bigquery_client
    client = bigquery_client(cfg)
    project = cfg.get("project") or cfg.get("project_id") or client.project
    sql = query or f"SELECT * FROM `{project}.{cfg.get('dataset')}.{table}`"
    if limit:
        sql = f"SELECT * FROM ({sql}) LIMIT {int(limit)}"
    job = client.query(sql)
    rows = list(job.result())
    cols = [f.name for f in job.schema] if job.schema else (list(rows[0].keys()) if rows else [])
    return cols, [tuple(r[c] for c in cols) for r in rows]

register_dialect(DialectInfo(
    "bigquery", "Google BigQuery", "google.cloud.bigquery", "google-cloud-bigquery", _bigquery_reader,
))


# ─────────────────────────── Redshift ───────────────────────────

def _redshift_reader(cfg, query, table, limit):
    from revoluzen_flow.connections.warehouse_clients import redshift_connect
    conn = redshift_connect(cfg)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"{sql} LIMIT {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "redshift", "Amazon Redshift", "psycopg2", "psycopg2-binary", _redshift_reader,
))


# ─────────────────────────── Databricks ───────────────────────────

def _databricks_reader(cfg, query, table, limit):
    from revoluzen_flow.connections.warehouse_clients import databricks_connect
    conn = databricks_connect(cfg)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"{sql} LIMIT {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "databricks", "Databricks SQL", "databricks.sql", "databricks-sql-connector", _databricks_reader,
))


# ─────────────────────────── MSSQL ───────────────────────────

def _mssql_reader(cfg, query, table, limit):
    pyodbc = _import_or_raise("pyodbc", "pyodbc")
    driver = cfg.get("driver", "ODBC Driver 18 for SQL Server")
    conn_str = (
        f"DRIVER={{{driver}}};SERVER={cfg['host']},{cfg.get('port', 1433)};"
        f"DATABASE={cfg['database']};UID={cfg['user']};PWD={cfg['password']};"
        f"TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(conn_str)
    # Decode SQL Server extended types (DATETIMEOFFSET / TIME) that pyodbc
    # can't handle natively — otherwise fetch aborts with "ODBC SQL type
    # -155 is not yet supported".
    from revoluzen_flow.connectors.odbc_runtime import (
        humanize_odbc_read_error, register_mssql_odbc_converters,
    )
    register_mssql_odbc_converters(conn)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT TOP {int(limit)} * FROM ({sql}) AS sub"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        try:
            rows = cur.fetchall()
        except Exception as exc:  # turn unsupported-type errors into a fix
            hint = humanize_odbc_read_error(exc)
            if hint:
                raise ValueError(hint) from exc
            raise
        return cols, [tuple(r) for r in rows]
    finally:
        conn.close()

register_dialect(DialectInfo(
    "mssql", "Microsoft SQL Server", "pyodbc", "pyodbc", _mssql_reader,
))


# ─────────────────────────── Oracle ───────────────────────────

def _oracle_reader(cfg, query, table, limit):
    from revoluzen_flow.connections.warehouse_clients import oracle_connect
    conn = oracle_connect(cfg)
    try:
        cur = conn.cursor()
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT * FROM ({sql}) WHERE ROWNUM <= {int(limit)}"
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

register_dialect(DialectInfo(
    "oracle", "Oracle", "oracledb", "oracledb", _oracle_reader,
))


# ─────────────────────────── MongoDB ───────────────────────────

def _mongodb_reader(cfg, query, table, limit):
    pm = _import_or_raise("pymongo", "pymongo")
    client = pm.MongoClient(cfg["uri"])
    try:
        db = client[cfg["database"]]
        coll = db[table or cfg.get("collection")]
        cursor = coll.find({}).limit(int(limit) if limit else 1000)
        docs = list(cursor)
        if not docs:
            return [], []
        cols: list[str] = []
        seen = set()
        for d in docs:
            for k in d.keys():
                if k not in seen:
                    cols.append(k)
                    seen.add(k)
        rows = [tuple(str(d.get(c)) if isinstance(d.get(c), (dict, list)) else d.get(c) for c in cols) for d in docs]
        return cols, rows
    finally:
        client.close()

register_dialect(DialectInfo(
    "mongodb", "MongoDB", "pymongo", "pymongo", _mongodb_reader,
))


# ─────────────────────────── ClickHouse ───────────────────────────

def _clickhouse_reader(cfg, query, table, limit):
    from revoluzen_flow.connections.warehouse_clients import clickhouse_client
    client = clickhouse_client(cfg)
    try:
        sql = query or f"SELECT * FROM {table}"
        if limit:
            sql = f"SELECT * FROM ({sql}) LIMIT {int(limit)}"
        result = client.query(sql)
        return list(result.column_names), [tuple(r) for r in result.result_rows]
    finally:
        client.close()

register_dialect(DialectInfo(
    "clickhouse", "ClickHouse", "clickhouse_connect", "clickhouse-connect", _clickhouse_reader,
))
