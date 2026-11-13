"""Reading Db2, SAP HANA and Teradata in pipelines (2026-11-11).

These connections could be saved and browsed, but the Database Source
refused them ("unsupported connection type") and Test connection said "No
tester implemented". The vendor drivers are add-ons and no server is
available here, so stand-in drivers record exactly what Flow hands them: the
connection settings, the SQL in each database's own words, and the rows.
"""

from __future__ import annotations

import datetime
import decimal
import sys
import types

import duckdb
import pytest

from revoluzen_flow.connections import addons, vendor_db
from revoluzen_flow.nodes.base import ExecutionContext
from revoluzen_flow.nodes.db_source import DbSourceNode


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.fail:
            raise RuntimeError(self.conn.fail)
        self.description = [(c, None) for c in self.conn.columns]

    def fetchall(self):
        return list(self.conn.rows)


class _Conn:
    def __init__(self, columns, rows, fail=None):
        self.columns, self.rows, self.fail = columns, rows, fail
        self.executed: list = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class _Lob:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


@pytest.fixture
def drivers(monkeypatch):
    seen: dict = {"columns": ["ID", "NAME"], "rows": [(1, "a"), (2, "b")], "fail": None}

    def make(kind):
        def connect(*args, **kwargs):
            conn = _Conn(seen["columns"], seen["rows"], seen["fail"])
            seen[kind] = {"args": args, "kwargs": kwargs, "conn": conn}
            return conn
        return connect

    # Db2: ibm_db.connect gives a handle, which ibm_db_dbi.Connection wraps.
    ibm = types.ModuleType("ibm_db")
    ibm.connect = make("db2")
    ibm_dbi = types.ModuleType("ibm_db_dbi")
    ibm_dbi.Connection = lambda handle: handle
    hana = types.ModuleType("hdbcli")
    hana_dbapi = types.ModuleType("hdbcli.dbapi")
    hana_dbapi.connect = make("sap_hana")
    hana.dbapi = hana_dbapi
    tera = types.ModuleType("teradatasql")
    tera.connect = make("teradata")
    monkeypatch.setitem(sys.modules, "ibm_db", ibm)
    monkeypatch.setitem(sys.modules, "ibm_db_dbi", ibm_dbi)
    monkeypatch.setitem(sys.modules, "hdbcli", hana)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", hana_dbapi)
    monkeypatch.setitem(sys.modules, "teradatasql", tera)
    monkeypatch.setattr(addons, "_importable", lambda module: True)
    return seen


CONFIG = {"host": "db.example", "port": "50000", "database": "SALES", "username": "u1", "password": "s3cret;pw"}


# ── Connecting ────────────────────────────────────────────────────────────

def test_db2_keeps_the_password_out_of_the_connection_string(drivers):
    vendor_db.connect("db2", {**CONFIG, "ssl": "true"})
    dsn, user, password = drivers["db2"]["args"]
    assert dsn.startswith("DATABASE=SALES;HOSTNAME=db.example;PORT=50000;PROTOCOL=TCPIP;")
    assert "SECURITY=SSL;" in dsn
    assert "PWD" not in dsn and "s3cret" not in dsn
    assert (user, password) == ("u1", "s3cret;pw")


def test_db2_refuses_a_host_that_would_break_the_connection_string(drivers):
    with pytest.raises(ValueError, match="cannot contain"):
        vendor_db.connect("db2", {**CONFIG, "host": "h;PROTOCOL=IPC"})


def test_sap_hana_encrypts_unless_told_not_to(drivers):
    vendor_db.connect("sap_hana", {**CONFIG, "port": "443"})
    kw = drivers["sap_hana"]["kwargs"]
    assert kw["address"] == "db.example" and kw["port"] == 443
    assert kw["encrypt"] is True and kw["sslValidateCertificate"] is True
    assert kw["databaseName"] == "SALES" and kw["password"] == "s3cret;pw"
    vendor_db.connect("sap_hana", {**CONFIG, "encrypt": "false"})
    assert "encrypt" not in drivers["sap_hana"]["kwargs"]


def test_teradata_gets_its_settings_as_text(drivers):
    vendor_db.connect("teradata", {"host": "td.example", "username": "u1", "password": "p", "port": 1025})
    kw = drivers["teradata"]["kwargs"]
    assert kw == {"host": "td.example", "user": "u1", "password": "p", "logmech": "TD2",
                  "connect_timeout": "15000", "dbs_port": "1025"}


# ── Reading in a pipeline ─────────────────────────────────────────────────

def _read(monkeypatch, conn_type, *, full_run=True, **params):
    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda connection_id, *a, **k: (dict(CONFIG), conn_type),
    )
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=full_run)
    return DbSourceNode({"connection_id": "c1", **params}).execute(ctx)


@pytest.mark.parametrize("conn_type,capped", [
    ("teradata", "SELECT TOP 5 * FROM (SELECT * FROM orders) AS __sample"),
    ("db2", "SELECT * FROM (SELECT * FROM orders) AS __sample FETCH FIRST 5 ROWS ONLY"),
    ("sap_hana", "SELECT * FROM (SELECT * FROM orders) AS __sample LIMIT 5"),
])
def test_a_sample_is_capped_in_each_databases_own_words(drivers, monkeypatch, conn_type, capped):
    rel = _read(monkeypatch, conn_type, full_run=False, query="SELECT * FROM orders", sample_rows=5)
    assert drivers[conn_type]["conn"].executed == [(capped, None)]
    assert rel.fetchall() == [(1, "a"), (2, "b")]
    assert drivers[conn_type]["conn"].closed


def test_table_mode_and_an_incremental_cursor(drivers, monkeypatch):
    _read(monkeypatch, "db2", source_mode="table", schema="SALES", table="ORDERS",
          watermark_column="UPDATED_AT", watermark_value="2026-01-01")
    sql, _ = drivers["db2"]["conn"].executed[0]
    assert sql == ('SELECT * FROM (SELECT * FROM "SALES"."ORDERS") AS __incr '
                   "WHERE \"UPDATED_AT\" > '2026-01-01'")


def test_values_arrive_with_their_types(drivers, monkeypatch):
    drivers["columns"] = ["AMOUNT", "DAY", "NOTE", "BLOB"]
    drivers["rows"] = [
        (decimal.Decimal("10.50"), datetime.date(2026, 1, 2), _Lob("long text"), b"\x01\xff"),
        (decimal.Decimal("4.25"), datetime.date(2026, 1, 3), "short", memoryview(b"\x00")),
    ]
    rel = _read(monkeypatch, "sap_hana", query="SELECT * FROM T")
    rows = rel.fetchall()
    assert rows[0][2] == "long text" and rows[0][3] == "01ff" and rows[1][3] == "00"
    # Numbers and dates keep their types, so sums and date comparisons work.
    total = rel.aggregate("SUM(AMOUNT), MAX(DAY)").fetchall()[0]
    assert total == (decimal.Decimal("14.75"), datetime.date(2026, 1, 3))


def test_without_the_driver_the_step_says_so(monkeypatch):
    monkeypatch.setattr(addons, "_importable", lambda module: False)
    with pytest.raises(ValueError, match="Teradata connector needs an add-on"):
        _read(monkeypatch, "teradata", query="SELECT 1")


def test_copy_data_reads_through_the_same_path(drivers, monkeypatch):
    from revoluzen_flow.nodes.flow_control import CopyDataNode

    monkeypatch.setattr(
        "revoluzen_flow.nodes.db_source._get_connection_config",
        lambda connection_id, *a, **k: (dict(CONFIG), "teradata"),
    )
    ctx = ExecutionContext(conn=duckdb.connect(), full_run=True)
    rel = CopyDataNode({"source_kind": "table", "source_table": "SALES.ORDERS"})._read_from_source(ctx, "c1")
    assert drivers["teradata"]["conn"].executed[0][0] == "SELECT * FROM SALES.ORDERS"
    assert rel.fetchall() == [(1, "a"), (2, "b")]


# ── Test connection and what the app says it can do ───────────────────────

def test_test_connection_runs_each_databases_cheapest_query(drivers):
    from revoluzen_flow.connections.tester import ConnectionTester

    for conn_type, probe in vendor_db.PROBE_SQL.items():
        result = ConnectionTester().test_connection(conn_type, dict(CONFIG))
        assert result["success"] is True, result
        assert result["message"] == f"Connected to {vendor_db.LABELS[conn_type]} at db.example"
        assert drivers[conn_type]["conn"].executed == [(probe, None)]
        assert "s3cret" not in str(result)


def test_test_connection_reports_a_failed_query(drivers):
    from revoluzen_flow.connections.tester import ConnectionTester

    drivers["fail"] = "SQL30082N Security processing failed"
    result = ConnectionTester().test_connection("db2", dict(CONFIG))
    assert result["success"] is False
    assert "test query failed" in result["message"]


def test_the_three_are_now_sources():
    from revoluzen_flow.connections.capability_levels import _source_runtime_types

    assert {"db2", "sap_hana", "teradata"} <= _source_runtime_types()
