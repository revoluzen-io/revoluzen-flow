"""Database Sink merge — against REAL Postgres and MySQL.

`test_db_sink_merge.py` covers the merge contract against SQLite and the
in-process DuckDB target. That exercises the shared code path but not the
servers people actually point this sink at, and the differences between
engines are exactly where an upsert goes wrong: quoting, autocommit,
``CREATE TABLE IF NOT EXISTS`` support, and how each one treats a DELETE
that matches nothing.

Marked `external`, which `pytest.ini` deselects by default, AND guarded by a
reachability check — so a normal run neither executes nor fails on them, and
`-m external` without servers skips rather than errors. To run them:

    docker run -d --name rflow-pg-test    -e POSTGRES_PASSWORD=flowtest \\
        -e POSTGRES_DB=flowtest -p 55432:5432 postgres:16-alpine
    docker run -d --name rflow-mysql-test -e MYSQL_ROOT_PASSWORD=flowtest \\
        -e MYSQL_DATABASE=flowtest -p 53306:3306 mysql:8
    docker run -d --name rflow-mssql-test -e 'ACCEPT_EULA=1' \\
        -e 'MSSQL_SA_PASSWORD=FlowTest!2026x' -p 51433:1433 \\
        mcr.microsoft.com/azure-sql-edge:latest

    REVOLUZEN_FLOW_TEST_PG=postgresql://postgres:flowtest@127.0.0.1:55432/flowtest
    REVOLUZEN_FLOW_TEST_MYSQL=mysql://root:flowtest@127.0.0.1:53306/flowtest
    REVOLUZEN_FLOW_TEST_MSSQL=mssql://sa:FlowTest!2026x@127.0.0.1:51433/master

    pytest tests/test_db_sink_merge_dialects.py -m external

The `-m external` is required: pytest.ini's addopts deselect that marker, so
without it the file collects and then runs nothing.

SQL Server note: `mcr.microsoft.com/mssql/server` refuses to start on a
Docker VM with under 2000 MB — common on Windows, where WSL2 is often capped
at 2 GB — so the recipe above uses `azure-sql-edge`, which speaks the same
T-SQL for everything this suite touches in roughly half the memory. CI has
the headroom and runs the full `mssql/server:2022-latest` image. Either way
the Microsoft ODBC driver must be installed at OS level; pyodbc alone is not
enough.

Each test writes to its own uniquely-named table and drops it afterwards,
so the engines can be exercised in parallel against a shared server.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from revoluzen_flow.nodes.activities import DbSinkNode  # noqa: E402

pytestmark = pytest.mark.external

PG_URL = os.environ.get("REVOLUZEN_FLOW_TEST_PG", "")
MYSQL_URL = os.environ.get("REVOLUZEN_FLOW_TEST_MYSQL", "")
MSSQL_URL = os.environ.get("REVOLUZEN_FLOW_TEST_MSSQL", "")

COLUMNS = ["id", "name", "amount"]


def _parse(url: str) -> dict:
    """postgresql://user:pass@host:port/db → the sink's config dict."""
    from urllib.parse import urlparse
    u = urlparse(url)
    return {
        "host": u.hostname, "port": u.port, "database": (u.path or "/").lstrip("/"),
        "user": u.username, "password": u.password,
    }


def _pg_conn():
    import psycopg2  # type: ignore
    c = _parse(PG_URL)
    return psycopg2.connect(
        host=c["host"], port=c["port"], dbname=c["database"],
        user=c["user"], password=c["password"], connect_timeout=5,
    )


def _mysql_conn():
    import pymysql  # type: ignore
    c = _parse(MYSQL_URL)
    return pymysql.connect(
        host=c["host"], port=int(c["port"]), database=c["database"],
        user=c["user"], password=c["password"], connect_timeout=5,
    )


def _mssql_conn():
    import pyodbc  # type: ignore
    from revoluzen_flow.connections.mssql_odbc import build_mssql_odbc_conn_str
    c = _parse(MSSQL_URL)
    # Built the same way the sink builds it, so a driver/TLS mismatch shows
    # up as a test failure rather than only in production. Driver 18
    # defaults to Encrypt=yes and rejects a container's self-signed cert;
    # the shared builder is what sets TrustServerCertificate.
    return pyodbc.connect(build_mssql_odbc_conn_str({
        "host": c["host"], "port": c["port"], "database": c["database"],
        "user": c["user"], "password": c["password"],
    }, pyodbc))


def _reachable(factory) -> bool:
    try:
        factory().close()
        return True
    except Exception:
        return False


pg = pytest.mark.skipif(
    not PG_URL or not _reachable(_pg_conn),
    reason="set REVOLUZEN_FLOW_TEST_PG to a reachable Postgres (see module docstring)",
)
mysql = pytest.mark.skipif(
    not MYSQL_URL or not _reachable(_mysql_conn),
    reason="set REVOLUZEN_FLOW_TEST_MYSQL to a reachable MySQL (see module docstring)",
)
mssql = pytest.mark.skipif(
    not MSSQL_URL or not _reachable(_mssql_conn),
    reason="set REVOLUZEN_FLOW_TEST_MSSQL to a reachable SQL Server (see module docstring)",
)


@pytest.fixture
def table():
    # Unique per test so a failed run never poisons the next one.
    return "merge_" + uuid.uuid4().hex[:10]


_URL = {"postgresql": lambda: PG_URL, "mysql": lambda: MYSQL_URL, "mssql": lambda: MSSQL_URL}
_CONN = {"postgresql": _pg_conn, "mysql": _mysql_conn, "mssql": _mssql_conn}


def _write(engine: str, table: str, rows, mode="merge", merge_on=("id",)):
    """Call the sink exactly as the engine does, including the commit."""
    node = DbSinkNode({})
    cfg = _parse(_URL[engine]())
    keys = list(merge_on) if merge_on else None
    if engine == "postgresql":
        node._write_postgresql(
            cfg["host"], cfg["port"], cfg["database"], cfg["user"], cfg["password"],
            None, table, COLUMNS, rows, mode, None, keys,
        )
    elif engine == "mysql":
        node._write_mysql(
            cfg["host"], cfg["port"], cfg["database"], cfg["user"], cfg["password"],
            table, COLUMNS, rows, mode, None, keys,
        )
    else:
        node._write_mssql(
            cfg["host"], cfg["port"], cfg["database"], cfg["user"], cfg["password"],
            table, COLUMNS, rows, mode, None, keys,
        )


def _q(ident: str, engine: str) -> str:
    """Quote for the engine. MySQL reads "x" as a string, not an identifier —
    the same trap the sink itself had."""
    return f"`{ident}`" if engine == "mysql" else f'"{ident}"'


def _read(engine: str, table: str):
    conn = _CONN[engine]()
    try:
        cur = conn.cursor()
        cols = ", ".join(_q(c, engine) for c in COLUMNS)
        cur.execute(
            f"SELECT {cols} FROM {_q(table, engine)} ORDER BY {_q('id', engine)}"
        )
        return [tuple(str(v) for v in r) for r in cur.fetchall()]
    finally:
        conn.close()


def _drop(engine: str, table: str):
    conn = _CONN[engine]()
    try:
        cur = conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {_q(table, engine)}")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


class _MergeContract:
    """One suite, run against each engine by the subclasses below."""
    engine: str

    def test_first_run_creates_the_table(self, table):
        try:
            _write(self.engine, table, [("1", "a", "10")])
            assert _read(self.engine, table) == [("1", "a", "10")]
        finally:
            _drop(self.engine, table)

    def test_second_run_updates_instead_of_duplicating(self, table):
        try:
            _write(self.engine, table, [("1", "a", "10"), ("2", "b", "20")])
            _write(self.engine, table, [("1", "a-updated", "11")])
            assert _read(self.engine, table) == [
                ("1", "a-updated", "11"), ("2", "b", "20"),
            ]
        finally:
            _drop(self.engine, table)

    def test_merge_is_idempotent(self, table):
        try:
            batch = [("1", "a", "10"), ("2", "b", "20")]
            _write(self.engine, table, batch)
            first = _read(self.engine, table)
            _write(self.engine, table, batch)
            assert _read(self.engine, table) == first
        finally:
            _drop(self.engine, table)

    def test_new_keys_insert_and_untouched_rows_survive(self, table):
        try:
            _write(self.engine, table, [("1", "a", "10"), ("2", "b", "20")])
            _write(self.engine, table, [("2", "b2", "22"), ("3", "c", "30")])
            assert _read(self.engine, table) == [
                ("1", "a", "10"), ("2", "b2", "22"), ("3", "c", "30"),
            ]
        finally:
            _drop(self.engine, table)

    def test_merge_needs_no_unique_index(self, table):
        """The reason this is delete-then-insert rather than a native upsert.

        Nothing here creates a constraint. ON CONFLICT would raise on
        Postgres and ON DUPLICATE KEY would silently insert on MySQL; both
        are wrong, and the second is wrong *quietly*.
        """
        try:
            _write(self.engine, table, [("1", "a", "10")])
            _write(self.engine, table, [("1", "a", "99")])
            assert _read(self.engine, table) == [("1", "a", "99")]
        finally:
            _drop(self.engine, table)

    def test_composite_keys(self, table):
        try:
            _write(self.engine, table, [("1", "x", "10"), ("1", "y", "20")],
                   merge_on=("id", "name"))
            _write(self.engine, table, [("1", "y", "99")], merge_on=("id", "name"))
            assert _read(self.engine, table) == [("1", "x", "10"), ("1", "y", "99")]
        finally:
            _drop(self.engine, table)


@pg
class TestPostgresMerge(_MergeContract):
    engine = "postgresql"


@mysql
class TestMysqlMerge(_MergeContract):
    engine = "mysql"


@mssql
class TestMssqlMerge(_MergeContract):
    """SQL Server is the dialect most likely to diverge: no
    `CREATE TABLE IF NOT EXISTS` (the sink uses `IF OBJECT_ID(...) IS NULL`),
    and its deprecated `TEXT` type cannot be used with `=` at all, so a
    table created with TEXT columns makes the merge's keyed DELETE fail.
    """
    engine = "mssql"
