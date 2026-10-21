"""Execute SQL's per-dialect driver matrix.

`execute_sql_task` is what makes ELT possible in Revoluzen Flow: load raw into
a warehouse, then transform inside it. Until 2026-10-21 the matrix covered
only Postgres, MySQL and SQLite, so Snowflake, BigQuery and Redshift could be
loaded into but not transformed in — the half of ELT that people actually mean.

Every driver here is faked. Real warehouses need credentials and cost money
per query, and the behaviour under test is ours, not theirs: which driver gets
picked, what config keys reach it, and — the part that bites — whether the
statement is committed.

**Commit semantics are the reason most of these tests exist.** A write that is
rolled back on close is indistinguishable from a successful one at the call
site: the cursor still reports a rowcount, the node still returns "ok", and the
data is simply not there. The drivers disagree with each other — psycopg2
commits when its context manager exits, pymysql and pyodbc default to manual
commit, Snowflake autocommits, BigQuery has no interactive transactions — so
each one is pinned individually.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from revoluzen_flow.nodes import control_extras as ce


# ── Fake driver plumbing ─────────────────────────────────────────────────────


class FakeCursor:
    """Minimal DB-API cursor: enough for `_capture` to read it."""

    def __init__(self, description=None, rows=(), rowcount=0):
        self.description = description
        self._rows = list(rows)
        self.rowcount = rowcount
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql):
        self.executed.append(sql)

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def conn_cfg(monkeypatch):
    """Patch the saved-connection lookup; each test supplies its own config."""

    def _set(cfg):
        monkeypatch.setattr(
            "revoluzen_flow.nodes.db_source._get_connection_config", lambda _id: cfg
        )

    return _set


@pytest.fixture
def fake_module(monkeypatch):
    """Install a fake importable module so `_require_driver` finds it."""

    def _install(name: str, mod: types.ModuleType):
        monkeypatch.setitem(sys.modules, name, mod)
        # A dotted name must also have its parents importable.
        parts = name.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent not in sys.modules:
                monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
        return mod

    return _install


def driver_with(cursor) -> tuple[types.ModuleType, FakeConnection]:
    conn = FakeConnection(cursor)
    mod = types.ModuleType("fake_driver")
    mod.connect = MagicMock(return_value=conn)  # type: ignore[attr-defined]
    return mod, conn


# ── Dispatch ─────────────────────────────────────────────────────────────────


def test_an_unknown_dialect_names_the_supported_ones(conn_cfg):
    """The error has to be actionable — a bare 'unsupported' is not."""
    conn_cfg({"type": "cassandra"})
    with pytest.raises(ValueError) as exc:
        ce._run_connection_sql("c1", "SELECT 1")
    msg = str(exc.value)
    assert "cassandra" in msg
    for dialect in ("postgres", "mysql", "sqlite", "redshift",
                    "mssql", "snowflake", "bigquery", "databricks"):
        assert dialect in msg


def test_a_missing_connection_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config", lambda _id: None
    )
    with pytest.raises(ValueError, match="not found"):
        ce._run_connection_sql("nope", "SELECT 1")


def test_a_missing_driver_gives_an_install_command(conn_cfg, monkeypatch):
    """An operator should not read a traceback to learn what to install."""
    conn_cfg({"type": "snowflake", "account": "a", "username": "u"})
    import builtins

    real = builtins.__import__

    def boom(name, *a, **kw):
        if name.startswith("snowflake"):
            raise ImportError("no snowflake")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    with pytest.raises(ValueError) as exc:
        ce._run_connection_sql("c1", "SELECT 1")
    assert "pip install revoluzen-flow[snowflake]" in str(exc.value)
    assert "Snowflake" in str(exc.value)


@pytest.mark.parametrize(
    "extra", ["postgres", "mssql", "snowflake", "bigquery", "databricks"]
)
def test_every_extra_we_recommend_actually_exists(extra):
    """An install command that fails is worse than no install command.

    `pyproject.toml` records this having happened before: error messages
    recommended `pip install revoluzen-flow[X]` for extras that were never
    declared, so following the advice failed. Every extra named by
    `_require_driver` is checked against the real declaration here.
    """
    import re
    from pathlib import Path

    pyproject = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert re.search(rf"^\s*{re.escape(extra)}\s*=\s*\[", pyproject, re.MULTILINE), (
        f"Execute SQL recommends `pip install revoluzen-flow[{extra}]` but that "
        f"extra is not declared in pyproject.toml"
    )


# ── Commit semantics, per driver ─────────────────────────────────────────────


def test_mysql_commits_its_writes(conn_cfg, monkeypatch):
    """REGRESSION: pymysql opens with autocommit=False.

    Without an explicit commit the INSERT was discarded when the connection
    closed, while the node still reported the rowcount and looked successful.
    """
    cur = FakeCursor(description=None, rowcount=3)
    mod, conn = driver_with(cur)
    monkeypatch.setitem(sys.modules, "pymysql", mod)
    conn_cfg({"type": "mysql", "host": "h", "database": "d", "username": "u"})

    rows, affected = ce._run_connection_sql("c1", "INSERT INTO t VALUES (1)")

    assert (rows, affected) == ([], 3)
    assert conn.committed, "a write that is never committed is silently lost"
    assert conn.closed


def test_mssql_commits_its_writes(conn_cfg, fake_module):
    """pyodbc also defaults to manual commit."""
    cur = FakeCursor(description=None, rowcount=7)
    mod, conn = driver_with(cur)
    fake_module("pyodbc", mod)
    conn_cfg({"type": "mssql", "host": "h", "database": "d", "username": "u"})

    rows, affected = ce._run_connection_sql("c1", "UPDATE t SET x = 1")

    assert affected == 7
    assert conn.committed
    assert conn.closed


def test_redshift_commits_its_writes(conn_cfg, fake_module):
    cur = FakeCursor(description=None, rowcount=1)
    mod, conn = driver_with(cur)
    fake_module("psycopg2", mod)
    conn_cfg({"type": "redshift", "host": "h", "database": "d", "username": "u"})

    ce._run_connection_sql("c1", "DELETE FROM t")

    assert conn.committed
    assert conn.closed


def test_snowflake_does_not_double_commit(conn_cfg, fake_module):
    """Snowflake autocommits; an explicit commit would be a no-op at best."""
    cur = FakeCursor(description=None, rowcount=2)
    mod, conn = driver_with(cur)
    fake_module("snowflake.connector", mod)
    conn_cfg({"type": "snowflake", "account": "acct", "username": "u", "password": "p"})

    ce._run_connection_sql("c1", "INSERT INTO t VALUES (1)")

    assert not conn.committed
    assert cur.closed and conn.closed


def test_bigquery_has_no_transaction_to_commit(conn_cfg, fake_module):
    cur = FakeCursor(description=None, rowcount=5)
    mod, conn = driver_with(cur)
    fake_module("google.cloud.bigquery.dbapi", mod)
    conn_cfg({"type": "bigquery", "project": "proj"})

    ce._run_connection_sql("c1", "DELETE FROM d.t WHERE TRUE")

    assert not conn.committed
    assert cur.closed and conn.closed


# ── Connection parameters reaching the driver ────────────────────────────────


def test_redshift_defaults_to_its_own_port_not_postgres(conn_cfg, fake_module):
    """Redshift is Postgres-wire but listens on 5439, not 5432."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("psycopg2", mod)
    conn_cfg({"type": "redshift", "host": "h", "database": "d", "username": "u"})

    ce._run_connection_sql("c1", "SELECT 1")

    assert mod.connect.call_args.kwargs["port"] == 5439


def test_snowflake_reuses_the_bulk_loader_config_keys(conn_cfg, fake_module):
    """One saved connection must drive both the loader and Execute SQL."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("snowflake.connector", mod)
    conn_cfg({
        "type": "snowflake", "account": "acct", "user": "u", "password": "p",
        "warehouse": "WH", "database": "DB", "role": "SYSADMIN",
    })

    ce._run_connection_sql("c1", "SELECT 1")

    kwargs = mod.connect.call_args.kwargs
    assert kwargs["account"] == "acct"
    assert kwargs["user"] == "u"          # `user`, not just `username`
    assert kwargs["warehouse"] == "WH"
    assert kwargs["role"] == "SYSADMIN"
    assert kwargs["schema"] == "PUBLIC"   # driver default when unset


def test_snowflake_drops_empty_values_so_driver_defaults_apply(conn_cfg, fake_module):
    """Passing role="" is not the same as not passing role."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("snowflake.connector", mod)
    conn_cfg({"type": "snowflake", "account": "a", "username": "u", "role": ""})

    ce._run_connection_sql("c1", "SELECT 1")

    assert "role" not in mod.connect.call_args.kwargs
    assert "private_key" not in mod.connect.call_args.kwargs


def test_either_user_or_username_is_accepted(conn_cfg, fake_module):
    """Saved connections use both spellings across the codebase."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("psycopg2", mod)
    conn_cfg({"type": "redshift", "host": "h", "database": "d", "user": "via_user"})

    ce._run_connection_sql("c1", "SELECT 1")

    assert mod.connect.call_args.kwargs["user"] == "via_user"


# ── Result capture ───────────────────────────────────────────────────────────


def test_a_result_set_comes_back_as_dicts(conn_cfg, fake_module):
    cur = FakeCursor(
        description=[("id", None), ("name", None)],
        rows=[(1, "alice"), (2, "bob")],
    )
    mod, _ = driver_with(cur)
    fake_module("snowflake.connector", mod)
    conn_cfg({"type": "snowflake", "account": "a", "username": "u"})

    rows, affected = ce._run_connection_sql("c1", "SELECT id, name FROM t")

    assert rows == [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
    assert affected == -1, "a result set reports -1, not a rowcount"


def test_the_statement_reaches_the_driver_unaltered(conn_cfg, fake_module):
    cur = FakeCursor(rowcount=0)
    mod, _ = driver_with(cur)
    fake_module("pyodbc", mod)
    conn_cfg({"type": "mssql", "host": "h", "database": "d", "username": "u"})

    sql = "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = s.x;"
    ce._run_connection_sql("c1", sql)

    assert cur.executed == [sql]


# ── Databricks (the lakehouse transform step in a Medallion pipeline) ────────


def _databricks_cfg(**over):
    cfg = {
        "type": "databricks",
        "host": "https://adb-123.4.azuredatabricks.net",
        "http_path": "/sql/1.0/warehouses/abc123",
        "access_token": "dapi-secret",
    }
    cfg.update(over)
    return cfg


def test_databricks_runs_sql_on_the_lakehouse(conn_cfg, fake_module):
    cur = FakeCursor(description=None, rowcount=42)
    mod, conn = driver_with(cur)
    fake_module("databricks.sql", mod)
    conn_cfg(_databricks_cfg())

    rows, affected = ce._run_connection_sql(
        "c1", "CREATE TABLE silver.orders AS SELECT * FROM bronze.orders"
    )

    assert affected == 42
    kwargs = mod.connect.call_args.kwargs
    assert kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
    assert kwargs["access_token"] == "dapi-secret"
    # Delta statements apply as they run; there is no transaction to commit.
    assert not conn.committed
    assert cur.closed and conn.closed


def test_databricks_hostname_is_stripped_of_its_scheme(conn_cfg, fake_module):
    """Credentials store the workspace as a URL; the driver rejects a scheme."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("databricks.sql", mod)
    conn_cfg(_databricks_cfg())

    ce._run_connection_sql("c1", "SELECT 1")

    assert mod.connect.call_args.kwargs["server_hostname"] == "adb-123.4.azuredatabricks.net"


def test_databricks_accepts_a_bare_hostname_too(conn_cfg, fake_module):
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("databricks.sql", mod)
    conn_cfg(_databricks_cfg(host="adb-123.4.azuredatabricks.net"))

    ce._run_connection_sql("c1", "SELECT 1")

    assert mod.connect.call_args.kwargs["server_hostname"] == "adb-123.4.azuredatabricks.net"


def test_databricks_reads_the_workspace_url_alias(conn_cfg, fake_module):
    """`credentials.py` accepts host / workspace_url / endpoint."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("databricks.sql", mod)
    cfg = _databricks_cfg()
    del cfg["host"]
    cfg["workspace_url"] = "https://adb-999.9.azuredatabricks.net"
    conn_cfg(cfg)

    ce._run_connection_sql("c1", "SELECT 1")

    assert mod.connect.call_args.kwargs["server_hostname"] == "adb-999.9.azuredatabricks.net"


@pytest.mark.parametrize("missing", ["host", "http_path", "access_token"])
def test_databricks_says_which_field_is_missing(conn_cfg, fake_module, missing):
    """http_path in particular is non-obvious — the error must name it."""
    mod, _ = driver_with(FakeCursor(rowcount=0))
    fake_module("databricks.sql", mod)
    cfg = _databricks_cfg()
    del cfg[missing]
    conn_cfg(cfg)

    with pytest.raises(ValueError) as exc:
        ce._run_connection_sql("c1", "SELECT 1")
    assert "http_path" in str(exc.value)
    assert "warehouses" in str(exc.value), "the error should show the path format"


def test_synapse_uses_the_sql_server_driver(conn_cfg, fake_module):
    """Azure Synapse speaks TDS, so pyodbc and the same connection string."""
    cur = FakeCursor(description=None, rowcount=1)
    mod, conn = driver_with(cur)
    fake_module("pyodbc", mod)
    conn_cfg({"type": "synapse", "host": "h", "database": "d", "username": "u"})

    ce._run_connection_sql("c1", "CREATE TABLE gold.t AS SELECT 1")

    assert conn.committed
    assert mod.connect.called


# ── MSSQL connection string ──────────────────────────────────────────────────


def test_mssql_connection_string_defaults_match_db_source():
    cs = ce._mssql_connection_string(
        {"host": "h", "database": "d", "username": "u", "password": "p"}, 30
    )
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in cs
    assert "SERVER=h,1433" in cs
    assert "Connection Timeout=30" in cs


def test_mssql_odbc_driver_version_is_overridable():
    """Hosts ship different ODBC versions; 18-only containers must work."""
    cs = ce._mssql_connection_string(
        {"host": "h", "odbc_driver": "ODBC Driver 18 for SQL Server"}, 10
    )
    assert "ODBC Driver 18 for SQL Server" in cs


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({"encrypt": True}, "Encrypt=yes"),
        ({"encrypt": False}, "Encrypt=no"),
        ({"trust_server_certificate": True}, "TrustServerCertificate=yes"),
    ],
)
def test_mssql_tls_options_are_passed_through(cfg, expected):
    assert expected in ce._mssql_connection_string({"host": "h", **cfg}, 10)


def test_mssql_omits_tls_options_that_were_not_set():
    """An unset option must not become an explicit `Encrypt=no`."""
    cs = ce._mssql_connection_string({"host": "h"}, 10)
    assert "Encrypt=" not in cs
    assert "TrustServerCertificate" not in cs
