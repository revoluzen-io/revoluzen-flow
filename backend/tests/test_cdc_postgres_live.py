"""Change Data Capture from a real PostgreSQL, through the real executor (2026-11-13).

Needs a PostgreSQL 11+ started with ``wal_level = logical`` and a user that may
create replication slots and publications. Point the tests at it with a libpq
connection string, for example::

    REVOLUZEN_FLOW_TEST_CDC_PG="host=127.0.0.1 port=55433 user=flow dbname=shop"

Without it, or when the server cannot be reached, every test here is skipped.
Each test works in its own schema and removes its slots afterwards.
"""
from __future__ import annotations

import csv
import decimal
import os
import secrets
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import parse_dsn  # noqa: E402

from revoluzen_flow.connections.models import Connection  # noqa: E402
from revoluzen_flow.engine import sync_state_store as position_store  # noqa: E402
from revoluzen_flow.engine.executor import WorkflowExecutor  # noqa: E402
from revoluzen_flow.ir.schema import Step, StepConnection, StepType, Workflow  # noqa: E402
from revoluzen_flow.streams.pg_cdc import default_name  # noqa: E402

DSN = os.environ.get("REVOLUZEN_FLOW_TEST_CDC_PG", "").strip()


def _reachable() -> str:
    if not DSN:
        return "REVOLUZEN_FLOW_TEST_CDC_PG is not set"
    try:
        conn = psycopg2.connect(DSN, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001
        return f"cannot reach the test PostgreSQL: {exc}"
    try:
        cur = conn.cursor()
        cur.execute("SHOW wal_level")
        level = cur.fetchone()[0]
        return "" if level == "logical" else f"wal_level is {level}, not logical"
    finally:
        conn.close()


_WHY = _reachable()
pytestmark = [pytest.mark.external, pytest.mark.skipif(bool(_WHY), reason=_WHY or "ok")]


def admin():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    return conn


@pytest.fixture
def pg(tmp_path, connection_store, _revoluzen_flow_test_db, monkeypatch):
    """A fresh schema with two tables, a saved connection to it, and clean-up."""
    from revoluzen_flow.connectors import cdc
    from revoluzen_flow.main import app_state

    monkeypatch.setattr(cdc, "require_plus", lambda feature: None)
    monkeypatch.setitem(app_state, "connection_store", connection_store)
    monkeypatch.setitem(app_state, "credential_store", None)
    monkeypatch.setattr(position_store.sync_state_store, "_db", _revoluzen_flow_test_db)

    schema = f"cdc_{uuid.uuid4().hex[:8]}"
    conn = admin()
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")
    cur.execute(f"CREATE TABLE {schema}.orders (id int PRIMARY KEY, amount numeric(10,2), "
                f"note text, paid boolean, placed timestamptz, big text)")
    cur.execute(f"CREATE TABLE {schema}.customers (id int PRIMARY KEY, name text, vip boolean)")
    params = parse_dsn(DSN)
    connection_store.create(Connection(
        id="c_pg", name="shop", type="postgresql", workspace_id="default",
        config={"host": params.get("host", "127.0.0.1"), "port": params.get("port", "5432"),
                "database": params.get("dbname"), "username": params.get("user"),
                "password": params.get("password", "")},
    ))
    state = {"schema": schema, "root": tmp_path, "workflows": [], "cur": cur}
    yield state
    for wf_id in state["workflows"]:
        name = default_name(wf_id, "src")
        cur.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
                    "WHERE slot_name = %s AND NOT active", (name,))
        cur.execute(f"DROP PUBLICATION IF EXISTS {name}")
    cur.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


def pipeline(pg, *, tables: str = "orders", fail_after_read: bool = False, **params) -> Workflow:
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    pg["workflows"].append(wf_id)
    qualified = ", ".join(f"{pg['schema']}.{t.strip()}" for t in tables.split(","))
    steps = [Step(id="src", type=StepType.CDC_SOURCE,
                  params={"connection_id": "c_pg", "tables": qualified, **params})]
    links = []
    last = "src"
    if fail_after_read:
        steps.append(Step(id="bad", type=StepType.FILTER, params={"condition": "no_such_column > 1"}))
        links.append(StepConnection(from_step="src", to_step="bad"))
        last = "bad"
    steps.append(Step(id="out", type=StepType.DESTINATION,
                      params={"connector_type": "csv", "file_path": str(pg["root"] / f"{wf_id}.csv")}))
    links.append(StepConnection(from_step=last, to_step="out"))
    return Workflow(id=wf_id, name=wf_id, steps=steps, connections=links)


def run(pg, wf: Workflow, **kwargs):
    executor = WorkflowExecutor(data_dir=str(pg["root"]), app_state={"data_dir": str(pg["root"])})
    result = executor.execute_workflow(wf, **kwargs)
    return result


def rows(pg, wf: Workflow) -> list[dict]:
    path = pg["root"] / f"{wf.id}.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def errors(result) -> dict:
    return {k: v.error for k, v in result.step_results.items() if v.error}


def sql(pg, statement: str, *args):
    pg["cur"].execute(statement.replace("{s}", pg["schema"]), args or None)


def slot_exists(name: str) -> bool:
    conn = admin()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (name,))
        return cur.fetchone() is not None
    finally:
        conn.close()


# ── Changes, in order, once each ────────────────────────────────────────────

def test_inserts_updates_and_deletes_arrive_once_with_their_types(pg):
    wf = pipeline(pg)
    first = run(pg, wf)
    assert first.status == "success", errors(first)
    assert rows(pg, wf) == [], "a first run that starts at new changes reads nothing"
    assert slot_exists(default_name(wf.id, "src"))

    sql(pg, "INSERT INTO {s}.orders VALUES (1, 10.50, 'first', true, '2026-11-13 09:30:00+00', NULL), "
            "(2, 20.00, 'second', false, NULL, NULL)")
    sql(pg, "UPDATE {s}.orders SET amount = 11.25 WHERE id = 1")
    sql(pg, "DELETE FROM {s}.orders WHERE id = 2")

    second = run(pg, wf)
    assert second.status == "success", errors(second)
    got = rows(pg, wf)
    assert [(r["_op"], r["id"]) for r in got] == [("insert", "1"), ("insert", "2"), ("update", "1"), ("delete", "2")]
    assert got[2]["amount"] == "11.25" and got[0]["note"] == "first"
    assert got[0]["_table"] == f"{pg['schema']}.orders"
    assert got[3]["note"] == "" and got[3]["id"] == "2", "a delete carries the key"
    assert int(got[0]["_lsn"]) < int(got[3]["_lsn"])
    assert got[0]["_committed_at"]

    third = run(pg, wf)
    assert third.status == "success", errors(third)
    assert rows(pg, wf) == [], "the slot moved past what the second run read"


def test_columns_keep_their_database_types(pg):
    wf = pipeline(pg)
    run(pg, wf)
    sql(pg, "INSERT INTO {s}.orders VALUES (7, 99.95, 'x', true, '2026-01-02 03:04:05.678+00', NULL)")

    from revoluzen_flow.connectors.cdc import CdcSourceNode
    from revoluzen_flow.nodes.base import ExecutionContext
    import duckdb

    ctx = ExecutionContext(conn=duckdb.connect(), data_dir=str(pg["root"]), full_run=True)
    ctx.workflow_id = wf.id
    ctx.workspace_id = "default"
    node = CdcSourceNode({**wf.steps[0].params, "_step_id": "src"})
    rel = node.execute(ctx)
    types = dict(zip(rel.columns, (str(t) for t in rel.types)))
    assert types["id"] == "INTEGER"
    assert types["amount"] == "DECIMAL(10,2)"
    assert types["paid"] == "BOOLEAN"
    assert types["placed"] == "TIMESTAMP"  # timestamptz, in UTC
    assert types["_lsn"] == "BIGINT"
    row = dict(zip(rel.columns, rel.fetchall()[0]))
    assert row["amount"] == decimal.Decimal("99.95") and row["paid"] is True


# ── At least once: nothing moves until the whole run has succeeded ─────────

def test_a_failed_run_leaves_the_changes_for_the_next_run(pg):
    wf = pipeline(pg)
    run(pg, wf)
    sql(pg, "INSERT INTO {s}.orders (id, note) VALUES (1, 'a'), (2, 'b')")

    failing = pipeline(pg, fail_after_read=True)
    failing.id = wf.id  # the same pipeline, now with a step that fails
    failed = run(pg, failing)
    assert failed.status == "error"

    again = run(pg, wf)
    assert again.status == "success", errors(again)
    assert [r["id"] for r in rows(pg, wf)] == ["1", "2"], "the failed run's changes came again"


def test_sample_runs_create_nothing_and_move_nothing(pg):
    wf = pipeline(pg)
    sql(pg, "INSERT INTO {s}.orders (id, note) VALUES (1, 'already there')")
    sample = run(pg, wf, preview_mode=True)
    assert sample.status == "success", errors(sample)
    assert not slot_exists(default_name(wf.id, "src")), "a Sample run made a slot"

    run(pg, wf)  # the first real run makes the slot
    sql(pg, "INSERT INTO {s}.orders (id, note) VALUES (2, 'new')")
    assert run(pg, wf, preview_mode=True).status == "success"
    real = run(pg, wf)
    assert [r["id"] for r in rows(pg, wf)] == ["2"], "the Sample run moved the slot"
    assert real.status == "success"


# ── First run with a copy of the tables ────────────────────────────────────

def test_copy_first_reads_every_row_then_only_changes(pg):
    sql(pg, "INSERT INTO {s}.orders (id, note) SELECT g, 'old ' || g FROM generate_series(1, 2500) g")
    wf = pipeline(pg, start_from="copy")
    first = run(pg, wf)
    assert first.status == "success", errors(first)
    got = rows(pg, wf)
    assert len(got) == 2500 and {r["_op"] for r in got} == {"snapshot"}

    sql(pg, "UPDATE {s}.orders SET note = 'changed' WHERE id = 5")
    second = run(pg, wf)
    assert second.status == "success", errors(second)
    assert [(r["_op"], r["id"], r["note"]) for r in rows(pg, wf)] == [("update", "5", "changed")]


# ── Several tables, truncate, large unchanged values ──────────────────────

def test_one_step_can_capture_several_tables(pg):
    wf = pipeline(pg, tables="orders, customers")
    run(pg, wf)
    sql(pg, "INSERT INTO {s}.orders (id, note) VALUES (1, 'o')")
    sql(pg, "INSERT INTO {s}.customers VALUES (1, 'Acme', true)")
    sql(pg, "TRUNCATE {s}.customers")
    result = run(pg, wf)
    assert result.status == "success", errors(result)
    got = [(r["_table"].split(".")[1], r["_op"]) for r in rows(pg, wf)]
    assert got == [("orders", "insert"), ("customers", "insert"), ("customers", "truncate")]


def test_a_large_value_the_update_did_not_touch_is_named_not_blanked_silently(pg):
    wf = pipeline(pg)
    run(pg, wf)
    big = secrets.token_hex(40000)  # does not compress, so it is stored out of line (TOAST)
    sql(pg, "INSERT INTO {s}.orders (id, note, big) VALUES (1, 'a', %s)", big)
    sql(pg, "UPDATE {s}.orders SET note = 'b' WHERE id = 1")
    result = run(pg, wf)
    assert result.status == "success", errors(result)
    insert, update = rows(pg, wf)
    assert insert["big"] == big and insert["_unchanged"] == ""
    assert update["big"] == "" and update["_unchanged"] == "big"


# ── Refusals that protect the database, and plain errors ──────────────────

def test_a_table_without_a_primary_key_is_refused_before_anything_is_created(pg):
    sql(pg, "CREATE TABLE {s}.events (at timestamptz, what text)")
    wf = pipeline(pg, tables="events")
    result = run(pg, wf)
    assert result.status == "error"
    message = errors(result)["src"]
    assert "has no primary key" in message and "REPLICA IDENTITY FULL" in message
    assert not slot_exists(default_name(wf.id, "src"))
    sql(pg, "ALTER TABLE {s}.events REPLICA IDENTITY FULL")
    assert run(pg, wf).status == "success"


def test_a_missing_table_says_so(pg):
    result = run(pg, pipeline(pg, tables="no_such_table"))
    assert "was not found" in errors(result)["src"]


def test_a_removed_slot_is_reported_and_reset_starts_again(pg):
    wf = pipeline(pg)
    assert run(pg, wf).status == "success"
    name = default_name(wf.id, "src")
    sql(pg, "SELECT pg_drop_replication_slot(%s)", name)
    gone = run(pg, wf)
    assert gone.status == "error" and "is gone" in errors(gone)["src"]

    position_store.sync_state_store.reset(wf.id, "src")
    assert run(pg, wf).status == "success"
    assert slot_exists(name)


def test_deleting_the_pipeline_removes_its_slot_and_publication(pg):
    from revoluzen_flow.connectors.cdc import cleanup_workflow_cdc_slots

    wf = pipeline(pg)
    assert run(pg, wf).status == "success"
    name = default_name(wf.id, "src")
    assert slot_exists(name)
    assert cleanup_workflow_cdc_slots(wf) == 1
    assert not slot_exists(name)
    sql(pg, "SELECT count(*) FROM pg_publication WHERE pubname = %s", name)
    assert pg["cur"].fetchone()[0] == 0


def test_a_step_can_name_an_existing_publication_instead_of_tables(pg):
    pub = f"pub_{uuid.uuid4().hex[:8]}"
    sql(pg, f"CREATE PUBLICATION {pub} FOR TABLE {{s}}.customers")
    try:
        wf = pipeline(pg, publication=pub)
        wf.steps[0].params["tables"] = ""
        assert run(pg, wf).status == "success"
        sql(pg, "INSERT INTO {s}.customers VALUES (5, 'Globex', false)")
        result = run(pg, wf)
        assert result.status == "success", errors(result)
        assert [(r["_op"], r["name"]) for r in rows(pg, wf)] == [("insert", "Globex")]
    finally:
        sql(pg, f"DROP PUBLICATION IF EXISTS {pub}")


# ── Applying the changes to another table ─────────────────────────────────

def test_applying_the_changes_keeps_a_copy_equal_to_the_source(pg):
    import secrets as _secrets

    sql(pg, "INSERT INTO {s}.orders (id, amount, note, big) VALUES (1, 1.00, 'a', %s), (2, 2.00, 'b', NULL)",
        _secrets.token_hex(40000))
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    pg["workflows"].append(wf_id)
    wf = Workflow(id=wf_id, name=wf_id, steps=[
        Step(id="src", type=StepType.CDC_SOURCE,
             params={"connection_id": "c_pg", "tables": f"{pg['schema']}.orders", "start_from": "copy"}),
        Step(id="copy", type=StepType.DB_SINK,
             params={"connection_id": "c_pg", "schema": pg["schema"], "table": "orders_copy",
                     "mode": "apply_changes", "merge_on": ["id"]}),
    ], connections=[StepConnection(from_step="src", to_step="copy")])

    def same() -> None:
        sql(pg, "SELECT id, amount, note, md5(coalesce(big, '')) FROM {s}.orders ORDER BY id")
        source = pg["cur"].fetchall()
        sql(pg, "SELECT id, amount, note, md5(coalesce(big, '')) FROM {s}.orders_copy ORDER BY id")
        assert pg["cur"].fetchall() == source

    assert run(pg, wf).status == "success"
    same()
    sql(pg, "INSERT INTO {s}.orders (id, amount, note) VALUES (3, 3.00, 'c')")
    sql(pg, "UPDATE {s}.orders SET amount = 1.50 WHERE id = 1")   # the large value is not sent again
    sql(pg, "UPDATE {s}.orders SET note = 'bb' WHERE id = 2")
    sql(pg, "DELETE FROM {s}.orders WHERE id = 3")
    sql(pg, "INSERT INTO {s}.orders (id, amount, note) VALUES (4, 4.00, 'd')")
    result = run(pg, wf)
    assert result.status == "success", errors(result)
    same()
    # Reading everything again (a Reset, or changes delivered twice after a
    # failure) and applying it leaves the copy as it is.
    position_store.sync_state_store.reset(wf.id, "src")
    again = run(pg, wf)
    assert again.status == "success", errors(again)
    same()

def test_reset_removes_the_slot_so_the_step_really_starts_again(pg):
    from revoluzen_flow.streams.pg_cdc import drop_on_reset

    wf = pipeline(pg)
    assert run(pg, wf).status == "success"
    name = default_name(wf.id, "src")
    assert slot_exists(name)

    class Store:
        def get(self, workflow_id, workspace_id=None):
            return wf if workflow_id == wf.id and workspace_id == "default" else None

    assert drop_on_reset(wf.id, "src", "other-workspace", app_state={"store": Store()}) == ""
    assert slot_exists(name), "a pipeline in another workspace was touched"
    note = drop_on_reset(wf.id, "src", "default", app_state={"store": Store()})
    assert "removed" in note and not slot_exists(name)