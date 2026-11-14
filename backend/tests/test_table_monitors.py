"""Table monitors: managed-table tests that run after a load or on a schedule."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from revoluzen_flow.datastore.table_monitor import due_tables
from revoluzen_flow.datastore.table_tests import last_loaded_at, previous_load_rows

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


class _T:
    def __init__(self, history, updated):
        self.load_history = history
        self.updated_at = updated


def test_freshness_reads_the_last_load_not_the_last_metadata_edit():
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    table = _T([{"at": old, "rows": 10}], datetime.now(timezone.utc))
    assert last_loaded_at(table).isoformat() == old
    assert last_loaded_at(_T([], datetime(2026, 1, 1, tzinfo=timezone.utc))).year == 2026


def test_drift_compares_with_the_load_before_the_newest():
    assert previous_load_rows(_T([{"rows": 100}, {"rows": 50}], None)) == 50
    assert previous_load_rows(_T([{"rows": 100}], None)) is None


def test_a_warn_test_stays_on_the_table_and_a_fail_test_is_reported(monkeypatch):
    from types import SimpleNamespace

    from revoluzen_flow.datastore.table_monitor import record_test_run

    reported = []
    monkeypatch.setattr("revoluzen_flow.api.steward._get_quality_finding_store", lambda ws: "store")
    monkeypatch.setattr(
        "revoluzen_flow.steward.quality.record_quality_report",
        lambda store, report, workspace_id: reported.append([a.check for a in report.assertions]),
    )
    table = SimpleNamespace(id="tbl_1", workspace_id="default", schema_name="default", name="t", row_count=10)

    def run(severity):
        return {"ran_at": "2026-11-11T00:00:00+00:00", "passed": 0, "failed": 1, "results": [
            {"test_type": "not_null", "columns": ["id"], "ok": False, "violations": 3, "severity": severity},
        ]}

    record_test_run({}, table, run("warn"), trigger="manual")
    assert reported == []
    record_test_run({}, table, run("fail"), trigger="manual")
    assert reported == [["not_null"]]


def test_only_the_scheduler_lease_holder_checks_tables():
    from types import SimpleNamespace

    from revoluzen_flow.datastore.table_monitor import TableMonitorLoop

    class _Db:
        def fetchall(self, sql, params=()):
            raise AssertionError("a server without the lease must not read the tables")

    standby = {"scheduler": SimpleNamespace(_holds_lease=lambda: False), "db": _Db()}
    assert TableMonitorLoop(standby).tick() == 0


def _orders(data_dir, n, dupes=False):
    path = f"{data_dir}/mon_orders.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("order_id,region\n")
        f.writelines(f"{1 if dupes and i < 3 else i},{'north' if i % 2 else 'south'}\n" for i in range(1, n + 1))
    return path


def _pipeline(client, path, table="monitored_orders"):
    r = client.post("/api/workflows", json={
        "name": f"load-{table}",
        "steps": [
            {"id": "src", "type": "csv_source", "label": "orders", "params": {"file_path": path}, "position": {"x": 0, "y": 0}},
            {"id": "out", "type": "local_table_sink", "label": "write",
             "params": {"schema_name": "default", "table_name": table, "mode": "replace"},
             "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "src", "to_step": "out"}],
    })
    assert r.status_code in (200, 201), r.text
    return r.json().get("id") or r.json()["workflow"]["id"]


def _table(client, name="monitored_orders"):
    body = client.get("/api/storage/tables").json()
    tables = body if isinstance(body, list) else body.get("tables", body.get("items", []))
    return next(t for t in tables if t["name"] == name)


def test_tests_run_after_a_load_and_failures_reach_the_steward(authed_client, data_dir):
    path = _orders(data_dir, 20)
    wid = _pipeline(authed_client, path)
    assert authed_client.post(f"/api/execute/workflow/{wid}?full_run=true").json()["status"] == "success"
    table_id = _table(authed_client)["id"]

    put = authed_client.put(f"/api/storage/tables/{table_id}/tests", json={"tests": [
        {"test_type": "not_null", "columns": ["order_id"]},
        {"test_type": "unique", "columns": ["order_id"]},
        {"test_type": "freshness", "max_age_minutes": 60},
        {"test_type": "row_count_anomaly", "drift_pct": 50},
    ]})
    assert put.status_code == 200, put.text

    # Several probes in one run: the old runner's half-read results broke the second.
    manual = authed_client.post(f"/api/storage/tables/{table_id}/tests/run").json()
    assert manual["failed"] == 0 and manual["passed"] == 4, manual

    mon = authed_client.put(f"/api/storage/tables/{table_id}/monitor", json={"after_load": True, "every_minutes": 30})
    assert mon.status_code == 200 and mon.json()["every_minutes"] == 30
    assert authed_client.put(f"/api/storage/tables/{table_id}/monitor", json={"every_minutes": 1}).status_code == 400

    # Load duplicates and 5 rows instead of 20: unique and drift must fail.
    _orders(data_dir, 5, dupes=True)
    assert authed_client.post(f"/api/execute/workflow/{wid}?full_run=true").json()["status"] == "success"
    deadline = time.time() + 15
    last = None
    while time.time() < deadline:
        last = _table(authed_client).get("last_test_run")
        if last and last.get("trigger") == "after_load":
            break
        time.sleep(0.25)
    assert last and last["trigger"] == "after_load", last
    failed = {r["test_type"] for r in last["results"] if not r["ok"]}
    assert failed == {"unique", "row_count_anomaly"}, last

    findings = authed_client.get("/api/steward/findings").json()["findings"]
    ours = [f for f in findings if (f.get("evidence") or {}).get("source_label") == "default.monitored_orders"]
    by_check = {f["evidence"]["check"]: f for f in ours}
    assert set(by_check) == {"unique", "row_count_min"}, [f["title"] for f in ours]
    # The volume finding prints the rows the table now holds, not a count of problems.
    assert by_check["row_count_min"]["title"] == "Row count below minimum: 5 rows in default.monitored_orders"
    assert "default.monitored_orders" in by_check["unique"]["body"]

    # The admin hears about it in the bell, with a link to Managed Tables.
    bell = authed_client.get("/api/notifications/").json()
    bell = bell if isinstance(bell, list) else bell.get("notifications", bell.get("items", []))
    notice = [n for n in bell if n.get("link_type") == "storage" and n.get("link_id") == table_id]
    assert notice and notice[0]["title"] == "default.monitored_orders failed 2 tests", bell[:3]


def test_a_periodic_monitor_is_due_once_its_interval_passes(authed_client, data_dir):
    from revoluzen_flow.main import app_state

    wid = _pipeline(authed_client, _orders(data_dir, 10), table="periodic_orders")
    assert authed_client.post(f"/api/execute/workflow/{wid}?full_run=true").json()["status"] == "success"
    table_id = _table(authed_client, "periodic_orders")["id"]
    r = authed_client.put(f"/api/storage/tables/{table_id}/tests", json={"tests": [{"test_type": "not_null", "columns": ["order_id"]}]})
    assert r.status_code == 200, r.text
    r = authed_client.put(f"/api/storage/tables/{table_id}/monitor", json={"every_minutes": 10})
    assert r.status_code == 200, r.text

    now = datetime.now(timezone.utc)
    rows = app_state["db"].fetchall("SELECT id, data FROM storage_tables")
    assert table_id in {t.id for t in due_tables(app_state, now)}, rows          # never run yet
    authed_client.post(f"/api/storage/tables/{table_id}/tests/run")
    assert table_id not in {t.id for t in due_tables(app_state, now)}      # just ran
    assert table_id in {t.id for t in due_tables(app_state, now + timedelta(minutes=11))}
