"""Change Data Capture from a real MySQL or MariaDB binary log (2026-11-13).

Needs a server with ``log_bin`` on, ``binlog_format = ROW`` and
``binlog_row_image = FULL``, the ``mysql-replication`` package, and two
connection strings: a user with REPLICATION SLAVE, REPLICATION CLIENT (and on
MariaDB BINLOG MONITOR) and all rights on one database, and an administrator
that may purge binary logs::

    REVOLUZEN_FLOW_TEST_CDC_MYSQL="host=127.0.0.1 port=33306 user=flow password=... database=shop"
    REVOLUZEN_FLOW_TEST_CDC_MYSQL_ADMIN="host=127.0.0.1 port=33306 user=root password=..."

Without them every test is skipped. Each test uses its own tables.
"""
from __future__ import annotations

import csv
import os
import uuid

import pytest

pymysql = pytest.importorskip("pymysql")

from revoluzen_flow.connections.models import Connection  # noqa: E402
from revoluzen_flow.engine import sync_state_store as position_store  # noqa: E402
from revoluzen_flow.engine.executor import WorkflowExecutor  # noqa: E402
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow  # noqa: E402


def _parse(text: str) -> dict:
    return dict(part.split("=", 1) for part in text.split() if "=" in part)


USER = _parse(os.environ.get("REVOLUZEN_FLOW_TEST_CDC_MYSQL", ""))
ADMIN = _parse(os.environ.get("REVOLUZEN_FLOW_TEST_CDC_MYSQL_ADMIN", ""))
pytestmark = [pytest.mark.external,
              pytest.mark.skipif(not USER, reason="REVOLUZEN_FLOW_TEST_CDC_MYSQL is not set")]


def connect(dsn: dict, **extra):
    return pymysql.connect(host=dsn["host"], port=int(dsn["port"]), user=dsn["user"], password=dsn.get("password", ""),
                           autocommit=True, **extra)


@pytest.fixture
def my(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch):
    pytest.importorskip("pymysqlreplication")
    from revoluzen_flow.connectors import cdc
    from revoluzen_flow.main import app_state

    monkeypatch.setattr(cdc, "require_plus", lambda feature: None)
    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(position_store.sync_state_store, "_db", _revoluzen_flow_test_db)
    suffix = uuid.uuid4().hex[:6]
    conn = connect(USER, database=USER["database"])
    cur = conn.cursor()
    orders, people = f"orders_{suffix}", f"people_{suffix}"
    cur.execute(f"CREATE TABLE {orders} (id INT PRIMARY KEY, amount DECIMAL(10,2), placed DATETIME(3), "
                f"stamped TIMESTAMP NULL, tags SET('a','b','c'), doc JSON, took TIME, note VARCHAR(50))")
    cur.execute(f"CREATE TABLE {people} (id INT PRIMARY KEY, name VARCHAR(40))")
    connection_store.create(Connection(
        id="c_my", name="shop", type="mariadb", workspace_id="default",
        config={"host": USER["host"], "port": USER["port"], "database": USER["database"],
                "username": USER["user"], "password": USER.get("password", "")},
    ))
    state = {"root": tmp_path, "cur": cur, "orders": orders, "people": people}
    yield state
    cur.execute(f"DROP TABLE IF EXISTS {orders}")
    cur.execute(f"DROP TABLE IF EXISTS {people}")
    conn.close()


def pipeline(my, tables: str | None = None, fail_after_read: bool = False, **params) -> Workflow:
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    steps = [Step(id="src", type=StepType.CDC_SOURCE,
                  params={"connection_id": "c_my", "tables": tables or my["orders"], **params})]
    links, last = [], "src"
    if fail_after_read:
        steps.append(Step(id="bad", type=StepType.FILTER, params={"condition": "no_such_column > 1"}))
        links.append(StepConnection(from_step="src", to_step="bad"))
        last = "bad"
    steps.append(Step(id="out", type=StepType.DESTINATION,
                      params={"connector_type": "csv", "file_path": str(my["root"] / f"{wf_id}.csv")}))
    links.append(StepConnection(from_step=last, to_step="out"))
    return Workflow(id=wf_id, name=wf_id, steps=steps, connections=links)


def run(my, wf, **kwargs):
    return WorkflowExecutor(data_dir=str(my["root"]), app_state={"data_dir": str(my["root"])}).execute_workflow(wf, **kwargs)


def rows(my, wf) -> list[dict]:
    path = my["root"] / f"{wf.id}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def errors(result) -> dict:
    return {k: v.error for k, v in result.step_results.items() if v.error}


def test_changes_arrive_once_in_order_with_their_values(my):
    wf = pipeline(my)
    first = run(my, wf)
    assert first.status == "success", errors(first)
    assert rows(my, wf) == []
    t = my["orders"]
    my["cur"].execute(f"INSERT INTO {t} VALUES (1, 10.50, '2026-11-13 09:30:00.250', '2026-11-13 04:00:00', "
                      f"'a,c', JSON_OBJECT('k', 1), '26:15:00', 'first'), (2, 3.00, NULL, NULL, '', NULL, NULL, 'second')")
    my["cur"].execute(f"UPDATE {t} SET amount = 11.25 WHERE id = 1")
    my["cur"].execute(f"DELETE FROM {t} WHERE id = 2")
    second = run(my, wf)
    assert second.status == "success", errors(second)
    got = rows(my, wf)
    assert [(r["_op"], r["id"]) for r in got] == [("insert", "1"), ("insert", "2"), ("update", "1"), ("delete", "2")]
    first_row = got[0]
    assert first_row["amount"] == "10.50" and first_row["tags"] == "a,c" and first_row["took"] == "26:15:00"
    assert first_row["placed"].startswith("2026-11-13 09:30:00.25")
    assert first_row["_table"].endswith(f".{t}") and first_row["_position"] < got[3]["_position"]
    assert '"k": 1' in first_row["doc"]
    assert run(my, wf).status == "success" and rows(my, wf) == []


def test_a_failed_run_leaves_the_changes_for_the_next_run(my):
    wf = pipeline(my)
    run(my, wf)
    my["cur"].execute(f"INSERT INTO {my['orders']} (id, note) VALUES (5, 'x')")
    failing = pipeline(my, fail_after_read=True)
    failing.id = wf.id
    assert run(my, failing).status == "error"
    again = run(my, wf)
    assert again.status == "success", errors(again)
    assert [r["id"] for r in rows(my, wf)] == ["5"]


def test_copy_first_then_changes_and_two_tables(my):
    my["cur"].execute(f"INSERT INTO {my['orders']} (id, note) VALUES (1, 'old'), (2, 'old')")
    my["cur"].execute(f"INSERT INTO {my['people']} VALUES (1, 'Ada')")
    wf = pipeline(my, tables=f"{my['orders']}, {my['people']}", start_from="copy")
    first = run(my, wf)
    assert first.status == "success", errors(first)
    assert sorted((r["_table"].split(".")[1], r["_op"]) for r in rows(my, wf)) == sorted(
        [(my["orders"], "snapshot"), (my["orders"], "snapshot"), (my["people"], "snapshot")])
    my["cur"].execute(f"UPDATE {my['people']} SET name = 'Grace' WHERE id = 1")
    second = run(my, wf)
    assert second.status == "success", errors(second)
    assert [(r["_op"], r["name"]) for r in rows(my, wf)] == [("update", "Grace")]


def test_sample_runs_do_not_move_the_place(my):
    wf = pipeline(my)
    my["cur"].execute(f"INSERT INTO {my['orders']} (id, note) VALUES (1, 'there before')")
    sample = run(my, wf, preview_mode=True)
    assert sample.status == "success", errors(sample)
    run(my, wf)
    my["cur"].execute(f"INSERT INTO {my['orders']} (id, note) VALUES (2, 'new')")
    assert run(my, wf, preview_mode=True).status == "success"
    assert run(my, wf).status == "success"
    assert [r["id"] for r in rows(my, wf)] == ["2"]


def test_a_missing_table_says_so(my):
    result = run(my, pipeline(my, tables="no_such_table"))
    assert result.status == "error" and "was not found" in errors(result)["src"]


@pytest.mark.skipif(not ADMIN, reason="REVOLUZEN_FLOW_TEST_CDC_MYSQL_ADMIN is not set")
def test_enum_and_set_columns_need_full_column_metadata(my):
    admin = connect(ADMIN)
    cur = admin.cursor()
    cur.execute("SHOW GLOBAL VARIABLES LIKE 'binlog_row_metadata'")
    before = cur.fetchone()[1]
    try:
        cur.execute("SET GLOBAL binlog_row_metadata = 'MINIMAL'")
        result = run(my, pipeline(my))
        assert result.status == "error"
        assert "binlog_row_metadata = FULL" in errors(result)["src"]
        assert run(my, pipeline(my, tables=my["people"])).status == "success", "a table without them still works"
    finally:
        cur.execute(f"SET GLOBAL binlog_row_metadata = '{before}'")
        admin.close()


@pytest.mark.skipif(not ADMIN, reason="REVOLUZEN_FLOW_TEST_CDC_MYSQL_ADMIN is not set")
def test_a_removed_log_file_is_reported_and_reset_starts_again(my):
    wf = pipeline(my)
    assert run(my, wf).status == "success"
    admin = connect(ADMIN)
    try:
        cur = admin.cursor()
        cur.execute("FLUSH BINARY LOGS")
        cur.execute("FLUSH BINARY LOGS")
        cur.execute("SHOW BINARY LOGS")
        newest = cur.fetchall()[-1][0]
        cur.execute(f"PURGE BINARY LOGS TO '{newest}'")
        cur.execute("SHOW BINARY LOGS")
        if len(cur.fetchall()) > 1:
            pytest.skip("the server kept older log files (a reader still holds them)")
    finally:
        admin.close()
    gone = run(my, wf)
    assert gone.status == "error" and "no longer keeps the log file" in errors(gone)["src"]
    position_store.sync_state_store.reset(wf.id, "src")
    assert run(my, wf).status == "success"
