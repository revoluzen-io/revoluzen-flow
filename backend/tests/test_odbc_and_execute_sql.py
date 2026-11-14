"""ODBC database connection, Synapse and Oracle in pipelines (2026-11-11).

The ODBC tests at the end run for real through the Microsoft Access Text
Driver, which Windows has, against a CSV file; they are skipped where that
driver is missing. Everything else uses stand-ins.
"""

from __future__ import annotations

import sys
import types

import duckdb
import pytest

from revoluzen_flow.connections import odbc
from revoluzen_flow.nodes import control_extras as ce
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.db_source import DbSourceNode


# ── Connection strings ──────────────────────────────────────────────────────


def test_values_with_special_characters_go_in_braces():
    assert odbc.odbc_value("plain") == "plain"
    assert odbc.odbc_value("pa;ss") == "{pa;ss}"
    assert odbc.odbc_value("a}b") == "{a}}b}"
    assert odbc.odbc_value(" lead") == "{ lead}"


def test_more_settings_are_parsed_with_braces():
    assert odbc.parse_settings("Server=db1; Port=9088;Database={a;b}};c};") == [
        ("Server", "db1"), ("Port", "9088"), ("Database", "a;b};c"),
    ]
    with pytest.raises(ValueError, match="has no '='"):
        odbc.parse_settings("Server=db1;justtext")
    with pytest.raises(ValueError, match="no closing"):
        odbc.parse_settings("Database={open")


def test_the_form_fields_win_over_more_settings():
    text = odbc.connection_string({
        "driver": "IBM INFORMIX ODBC DRIVER",
        "settings": "Server=ids;PWD=typed-here;Database=stores",
        "username": "informix", "password": "p;w{d}",
    })
    assert text == "DRIVER={IBM INFORMIX ODBC DRIVER};Server=ids;Database=stores;UID=informix;PWD={p;w{d}}};"


def test_a_driver_or_dsn_is_required():
    with pytest.raises(ValueError, match="Name the ODBC driver"):
        odbc.connection_string({"settings": "Server=x"})
    assert odbc.connection_string({"dsn": "Sales DSN"}) == "DSN=Sales DSN;"


def test_a_missing_driver_names_the_ones_installed(monkeypatch):
    fake = types.ModuleType("pyodbc")

    class Error(Exception):
        pass

    def connect(text, **kwargs):
        raise Error("IM002", "[IM002] Data source name not found and no default driver specified")

    fake.Error = Error
    fake.connect = connect
    fake.drivers = lambda: ["SQL Server", "PostgreSQL Unicode"]
    monkeypatch.setitem(sys.modules, "pyodbc", fake)
    with pytest.raises(ValueError, match="ODBC drivers it has: SQL Server, PostgreSQL Unicode"):
        odbc.connect({"driver": "Nope Driver"})


def test_sql_server_passwords_are_escaped():
    from revoluzen_flow.connections.mssql_odbc import build_mssql_odbc_conn_str

    class FakePyodbc:
        @staticmethod
        def drivers():
            return ["ODBC Driver 18 for SQL Server"]

    text = build_mssql_odbc_conn_str({"host": "h", "user": "sa", "password": "a;b}c", "database": "d"}, FakePyodbc)
    assert "PWD={a;b}}c};" in text and "UID=sa;" in text and "DATABASE=d;" in text
    assert "PWD=plain;" in build_mssql_odbc_conn_str({"host": "h", "user": "sa", "password": "plain"}, FakePyodbc)


# ── Execute SQL ─────────────────────────────────────────────────────────────


def test_execute_sql_takes_the_saved_connection_as_it_really_comes(tmp_path, monkeypatch):
    """The lookup returns (settings, type); this used to fail on every saved connection."""
    db = tmp_path / "x.db"
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda _id: ({"file_path": str(db)}, "sqlite"),
    )
    ce._run_connection_sql("c1", "CREATE TABLE t (n INTEGER)")
    ce._run_connection_sql("c1", "INSERT INTO t VALUES (7)")
    rows, affected = ce._run_connection_sql("c1", "SELECT n FROM t")
    assert rows == [{"n": 7}] and affected == -1


@pytest.mark.parametrize("sql, sent", [
    ("SELECT 1 FROM DUAL;", "SELECT 1 FROM DUAL"),
    ("UPDATE t SET a = 1;\n/", "UPDATE t SET a = 1"),
    ("BEGIN NULL; END;", "BEGIN NULL; END;"),
    ("CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;\n/", "CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;"),
])
def test_oracle_statements_lose_sql_plus_endings(sql, sent):
    assert ce._oracle_statement(sql) == sent


def test_execute_sql_runs_on_oracle(monkeypatch):
    from revoluzen_flow.connections import warehouse_clients as wc

    class Lob:
        def read(self):
            return "long text"

    class Cursor:
        description = None
        rowcount = 0

        def execute(self, sql):
            self.sql = sql
            if sql.startswith("SELECT"):
                self.description = [("ID",), ("NOTE",)]

        def fetchall(self):
            return [(1, Lob())]

    class Conn:
        committed = False

        def __init__(self):
            self.cur = Cursor()

        def cursor(self):
            return self.cur

        def commit(self):
            Conn.committed = True

        def close(self):
            pass

    conn = Conn()
    monkeypatch.setattr(wc, "oracle_connect", lambda cfg, timeout=None: conn)
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda _id: ({"host": "ora", "service_name": "ORCL"}, "oracle"),
    )
    rows, affected = ce._run_connection_sql("c1", "SELECT id, note FROM notes;")
    assert conn.cur.sql == "SELECT id, note FROM notes"
    assert rows == [{"ID": 1, "NOTE": "long text"}]
    assert Conn.committed


def test_an_odbc_database_cannot_be_a_destination(monkeypatch):
    from revoluzen_flow.nodes.activities import DbSinkNode

    with pytest.raises(ValueError, match="read but not written"):
        DbSinkNode({})._write_real("odbc", {}, ("t",), ["a"], [(1,)], "append")


# ── Synapse ─────────────────────────────────────────────────────────────────


def test_synapse_reads_through_the_sql_server_path(monkeypatch):
    seen: list[str] = []

    def fake_mssql(self, config, host, port, database, user, password, query, **kwargs):
        seen.append(query)
        return [(1,)], ["n"]

    monkeypatch.setattr(DbSourceNode, "_query_mssql", fake_mssql)
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda connection_id, *a, **k: ({"host": "ws.sql.azuresynapse.net"}, "synapse"),
    )
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=False)
    rel = DbSourceNode({"connection_id": "c1", "query": "SELECT n FROM dbo.t", "sample_rows": 5}).execute(ctx)
    assert rel.fetchall() == [(1,)]
    assert seen == ["SELECT TOP 5 * FROM (SELECT n FROM dbo.t) AS __sample"]


def test_synapse_writes_through_the_sql_server_path(monkeypatch):
    from revoluzen_flow.nodes.activities import DbSinkNode

    calls: list = []
    monkeypatch.setattr(DbSinkNode, "_write_mssql", lambda self, *args: calls.append(args))
    DbSinkNode({})._write_real("synapse", {"host": "ws"}, ("dbo", "t"), ["a"], [(1,)], "append")
    assert calls and calls[0][0] == "ws"


# ── ODBC for real, through the Microsoft Access Text Driver ────────────────


TEXT_DRIVER = "Microsoft Access Text Driver (*.txt, *.csv)"


def _has_text_driver() -> bool:
    try:
        import pyodbc
        return TEXT_DRIVER in pyodbc.drivers()
    except Exception:  # noqa: BLE001
        return False


needs_text_driver = pytest.mark.skipif(not _has_text_driver(), reason="no ODBC text driver on this machine")


@pytest.fixture
def csv_folder(tmp_path):
    (tmp_path / "orders.csv").write_text("id,amount\n1,10.5\n2,20\n3,30\n", encoding="utf-8")
    return tmp_path


@needs_text_driver
def test_odbc_reads_a_real_driver(csv_folder):
    cfg = {"driver": TEXT_DRIVER, "settings": f"DBQ={csv_folder}"}
    rows, columns = odbc.read(cfg, "SELECT id, amount FROM [orders.csv]")
    assert columns == ["id", "amount"]
    assert [r[0] for r in rows] == [1, 2, 3]
    rows, _ = odbc.read(cfg, "SELECT id FROM [orders.csv]", max_rows=2)
    assert len(rows) == 2


@needs_text_driver
def test_odbc_test_connection_and_browsing(csv_folder):
    from revoluzen_flow.connections.catalog_extensions import _odbc_catalog
    from revoluzen_flow.connections.tester import ConnectionTester

    cfg = {"driver": TEXT_DRIVER, "settings": f"DBQ={csv_folder}"}
    result = ConnectionTester().test_connection("odbc", cfg)
    assert result["success"], result
    assert TEXT_DRIVER in result["message"]
    assert "orders.csv" in [item.name for item in _odbc_catalog(cfg).items]


@needs_text_driver
def test_a_database_source_reads_an_odbc_connection(csv_folder, monkeypatch):
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda connection_id, *a, **k: ({"driver": TEXT_DRIVER, "settings": f"DBQ={csv_folder}"}, "odbc"),
    )
    sample = DbSourceNode({"connection_id": "c1", "query": "SELECT id FROM [orders.csv]", "sample_rows": 2})
    assert len(sample.execute(ExecutionContext(conn=duckdb.connect(), full_run=False)).fetchall()) == 2
    full = DbSourceNode({"connection_id": "c1", "query": "SELECT id FROM [orders.csv]"})
    assert len(full.execute(ExecutionContext(conn=duckdb.connect(), full_run=True)).fetchall()) == 3


@needs_text_driver
def test_execute_sql_runs_on_odbc(csv_folder, monkeypatch):
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda _id: ({"driver": TEXT_DRIVER, "settings": f"DBQ={csv_folder}"}, "odbc"),
    )
    rows, _ = ce._run_connection_sql("c1", "SELECT COUNT(*) AS n FROM [orders.csv]")
    assert rows == [{"n": 3}]
