"""Profile tab in Storage: column statistics before a pipeline is built."""

from __future__ import annotations

import duckdb
import pytest

from revoluzen_flow.datastore.reader import profile_file, profile_relation

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def test_a_relation_is_profiled_like_the_data_profile_step():
    conn = duckdb.connect(":memory:")
    rel = conn.sql(
        "SELECT * FROM (VALUES (1, 'a', 10.0), (2, 'a', NULL), (3, 'b', 30.0), (4, NULL, 40.0))"
        " AS t(id, region, amount)"
    )
    out = profile_relation(conn, rel)
    assert out["row_count"] == 4 and out["rows_profiled"] == 4 and out["sampled"] is False
    cols = {c["column"]: c for c in out["columns"]}
    assert cols["id"]["distinct_count"] == 4 and cols["id"]["null_count"] == 0
    assert cols["region"]["null_count"] == 1 and cols["region"]["top_value"] == "a"
    assert cols["amount"]["mean_value"] == pytest.approx(80 / 3)


def test_large_data_is_sampled_and_says_so():
    conn = duckdb.connect(":memory:")
    out = profile_relation(conn, conn.sql("SELECT range AS id FROM range(5000)"), sample_rows=1000)
    assert out["sampled"] is True
    assert (out["row_count"], out["rows_profiled"]) == (5000, 1000)


def test_a_json_document_has_nothing_to_profile(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"name": "x", "steps": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="single document"):
        profile_file(str(path), "json")


def test_a_managed_table_can_be_profiled_from_storage(authed_client, data_dir):
    path = f"{data_dir}/profile_orders.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("order_id,region\n")
        f.writelines(f"{i},{'north' if i % 3 else ''}\n" for i in range(1, 31))
    r = authed_client.post("/api/workflows", json={
        "name": "profile-orders",
        "steps": [
            {"id": "src", "type": "csv_source", "label": "orders", "params": {"file_path": path}, "position": {"x": 0, "y": 0}},
            {"id": "out", "type": "local_table_sink", "label": "write",
             "params": {"schema_name": "default", "table_name": "profile_orders", "mode": "replace"},
             "position": {"x": 1, "y": 0}},
        ],
        "connections": [{"from_step": "src", "to_step": "out"}],
    })
    assert r.status_code in (200, 201), r.text
    wid = r.json().get("id") or r.json()["workflow"]["id"]
    assert authed_client.post(f"/api/execute/workflow/{wid}?full_run=true").json()["status"] == "success"
    tables = authed_client.get("/api/storage/tables").json()
    tables = tables if isinstance(tables, list) else tables.get("tables", tables.get("items", []))
    table = next(t for t in tables if t["name"] == "profile_orders")

    prof = authed_client.get(f"/api/storage/tables/{table['id']}/profile")
    assert prof.status_code == 200, prof.text
    cols = {c["column"]: c for c in prof.json()["columns"]}
    assert prof.json()["row_count"] == 30
    assert cols["order_id"]["distinct_count"] == 30
    assert cols["region"]["null_count"] == 10

    # Another workspace cannot read it.
    other = authed_client.get(f"/api/storage/tables/{table['id']}/profile", headers={"X-Workspace-Id": "someone-else"})
    assert other.status_code in (403, 404)
