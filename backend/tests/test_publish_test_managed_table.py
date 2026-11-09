"""The test run behind Publish, on a pipeline that uses Managed Tables.

Publish failed on any such pipeline: the test run built its executor without
app_state, so Managed Table nodes could not find their datastore. The test also
runs on capped sample rows, so it must not write them over the real table.
"""

from __future__ import annotations

import pytest

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)

ROWS = 2500


@pytest.fixture
def orders_csv(data_dir):
    path = f"{data_dir}/orders.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("id,amount\n")
        f.writelines(f"{i},{i * 2}\n" for i in range(ROWS))
    return path


def _save(client, name, steps, connections):
    r = client.post("/api/workflows", json={
        "name": name, "steps": steps, "connections": connections,
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("id") or body["workflow"]["id"]


@pytest.fixture
def writer_id(authed_client, orders_csv):
    return _save(authed_client, "write-orders", [
        {"id": "src", "type": "csv_source", "label": "orders",
         "params": {"file_path": orders_csv}, "position": {"x": 0, "y": 0}},
        {"id": "out", "type": "local_table_sink", "label": "write",
         "params": {"schema_name": "default", "table_name": "orders_tbl", "mode": "replace"},
         "position": {"x": 1, "y": 0}},
    ], [{"from_step": "src", "to_step": "out"}])


def _table(client):
    r = client.get("/api/storage/tables")
    assert r.status_code == 200, r.text
    body = r.json()
    tables = body if isinstance(body, list) else body.get("tables", body.get("items", []))
    return next((t for t in tables if t["name"] == "orders_tbl"), None)


def _receipts(client, workflow_id):
    body = client.get(f"/api/execution-contracts/{workflow_id}/receipts").json()
    return body if isinstance(body, list) else body["receipts"]


def test_publish_test_passes_with_a_managed_table_sink(authed_client, writer_id):
    r = authed_client.post(f"/api/workflows/{writer_id}/test")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success", r.json()["test_results"]["error"]
    assert r.json()["test_results"]["steps_passed"] == 2
    # The Publish dialog lists these; they were never sent, so the list stayed
    # empty and a failed test claimed it "never reached step execution".
    logs = r.json()["execution"]["step_logs"]
    assert [s["step_id"] for s in logs] == ["src", "out"]


def test_publish_test_does_not_touch_the_real_table(authed_client, writer_id, orders_csv):
    run = authed_client.post(f"/api/execute/workflow/{writer_id}?full_run=true")
    assert run.json()["status"] == "success", run.text
    assert _table(authed_client)["row_count"] == ROWS

    # Shrink the source: a test that wrote would leave 1 row in the table.
    with open(orders_csv, "w", encoding="utf-8") as f:
        f.write("id,amount\n1,2\n")
    r = authed_client.post(f"/api/workflows/{writer_id}/test")
    assert r.json()["status"] == "success", r.text
    assert r.json()["execution"]["step_results"]["out"]["skipped_reason"] == "sample"

    assert _table(authed_client)["row_count"] == ROWS


def test_sampled_output_is_not_reused_by_a_rerun(authed_client, writer_id):
    """A sampled step output has the same cache key as a full one, so "Rerun
    from here" after a Publish test wrote 1,000 rows to the real table."""
    run = authed_client.post(f"/api/execute/workflow/{writer_id}?full_run=true")
    assert run.json()["status"] == "success", run.text

    assert authed_client.post(f"/api/workflows/{writer_id}/test").json()["status"] == "success"

    r = authed_client.post(f"/api/execute/workflow/{writer_id}/step/out/resume")
    assert r.json()["status"] == "success", r.text
    assert _table(authed_client)["row_count"] == ROWS


def test_contract_auto_create_does_not_touch_the_real_table(authed_client, writer_id, orders_csv):
    run = authed_client.post(f"/api/execute/workflow/{writer_id}?full_run=true")
    assert run.json()["status"] == "success", run.text

    # Shrink the source: a real write would leave 1 row in the table.
    with open(orders_csv, "w", encoding="utf-8") as f:
        f.write("id,amount\n1,2\n")
    r = authed_client.post(f"/api/contracts/auto-create/{writer_id}")
    assert r.status_code == 200, r.text
    assert _table(authed_client)["row_count"] == ROWS

    made = {c["step_id"]: c for c in r.json()["contracts"]}
    assert set(made) == {"src", "out"}, r.json()
    assert [c["name"] for c in made["out"]["expected_columns"]] == ["id", "amount"]

    # Schema contracts and execution contracts shared one app_state key, so
    # every /api/contracts route reached the wrong store.
    listed = authed_client.get(f"/api/contracts/{writer_id}")
    assert listed.status_code == 200, listed.text
    assert {c["step_id"] for c in listed.json()} == {"src", "out"}
    assert authed_client.get(f"/api/execution-contracts/{writer_id}").status_code != 500


def test_contract_auto_create_skips_steps_with_no_real_output(authed_client, orders_csv):
    """A skipped side effect outputs a placeholder row. A contract built from
    that, or from anything fed by it, would describe the placeholder."""
    wid = _save(authed_client, "with-action", [
        {"id": "src", "type": "csv_source", "label": "orders",
         "params": {"file_path": orders_csv}, "position": {"x": 0, "y": 0}},
        {"id": "act", "type": "http_request", "label": "call",
         "params": {"url": "https://example.invalid/hook", "method": "POST"},
         "position": {"x": 1, "y": 0}},
        {"id": "after", "type": "filter", "label": "after",
         "params": {"mode": "expression", "condition": "1=1"},
         "position": {"x": 2, "y": 0}},
    ], [{"from_step": "src", "to_step": "act"}, {"from_step": "act", "to_step": "after"}])

    r = authed_client.post(f"/api/contracts/auto-create/{wid}")
    assert r.status_code == 200, r.text
    assert [c["step_id"] for c in r.json()["contracts"]] == ["src"], r.json()
    assert {s["step_id"] for s in r.json()["skipped"]} == {"act", "after"}


def test_a_no_write_run_is_not_judged_by_the_execution_contract(authed_client, writer_id):
    """A sample can't meet a row-count promise. Judging it would fail Publish
    and add a false receipt to the pipeline's audit chain."""
    put = authed_client.put(f"/api/execution-contracts/{writer_id}", json={
        "id": "c1",
        "default_output_step": "out",
        "guarantees": [{"type": "row_count_min", "min_rows": 2000}],
    })
    assert put.status_code == 200, put.text

    r = authed_client.post(f"/api/workflows/{writer_id}/test")
    assert r.json()["status"] == "success", r.json()["test_results"]["error"]
    assert _receipts(authed_client, writer_id) == []

    # A real run is still judged, and it meets the promise.
    run = authed_client.post(f"/api/execute/workflow/{writer_id}?full_run=true")
    assert run.json()["status"] == "success", run.text
    assert len(_receipts(authed_client, writer_id)) == 1


def test_a_run_that_writes_reads_everything(authed_client, writer_id, monkeypatch):
    """Scheduled runs, backfills and Replay never asked for a full run, so they
    wrote capped data to real destinations."""
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "DEV_SAMPLE_ROWS", 100)

    run = authed_client.post(f"/api/execute/workflow/{writer_id}")  # no full_run
    assert run.json()["status"] == "success", run.text
    assert _table(authed_client)["row_count"] == ROWS


def test_a_run_that_does_not_write_stays_capped(authed_client, writer_id, monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "DEV_SAMPLE_ROWS", 100)

    r = authed_client.post(f"/api/execute/workflow/{writer_id}/step/src")
    assert r.json()["row_count"] == 100, r.text


def test_test_node_output_is_not_reused_by_a_rerun(authed_client, writer_id, monkeypatch):
    from revoluzen_flow import runtime_config
    monkeypatch.setattr(runtime_config, "DEV_SAMPLE_ROWS", 100)

    assert authed_client.post(f"/api/execute/workflow/{writer_id}/step/src").json()["row_count"] == 100
    r = authed_client.post(f"/api/execute/workflow/{writer_id}/step/out/resume")
    assert r.json()["status"] == "success", r.text
    assert _table(authed_client)["row_count"] == ROWS


def test_a_full_run_is_not_silently_truncated_by_default():
    """A 10,000,000 default ceiling cut any larger source short with no error."""
    from revoluzen_flow import runtime_config
    assert runtime_config.MAX_SOURCE_ROWS == 0


def test_cached_output_from_older_versions_is_ignored(data_dir):
    """Older versions cached sample rows under the same key as full results, so
    nothing written before the `complete` marker can be trusted."""
    import json
    import os
    from revoluzen_flow.engine.step_cache import StepCache

    cache = StepCache(str(data_dir), "wf-old")
    with open(cache.parquet_path("s1"), "wb") as f:
        f.write(b"x")
    entry = {"effective_hash": "h1", "parquet_path": os.path.relpath(cache.parquet_path("s1"), str(data_dir))}
    with open(cache.manifest_path, "w", encoding="utf-8") as f:
        json.dump({"s1": entry}, f)
    assert cache.hit("s1", "h1") is None

    with open(cache.manifest_path, "w", encoding="utf-8") as f:
        json.dump({"s1": {**entry, "complete": True}}, f)
    assert cache.hit("s1", "h1") == cache.parquet_path("s1")


def test_publish_test_caps_a_large_source(authed_client, writer_id):
    r = authed_client.post(f"/api/workflows/{writer_id}/test")
    assert r.json()["execution"]["step_results"]["src"]["row_count"] == 1000


def test_publish_test_reads_a_managed_table_source(authed_client, writer_id):
    assert authed_client.post(
        f"/api/execute/workflow/{writer_id}?full_run=true"
    ).json()["status"] == "success"

    reader = _save(authed_client, "read-orders", [
        {"id": "src", "type": "local_table_source", "label": "read",
         "params": {"schema_name": "default", "table_name": "orders_tbl"},
         "position": {"x": 0, "y": 0}},
        {"id": "f", "type": "filter", "label": "big",
         "params": {"mode": "expression", "condition": "amount > 10"},
         "position": {"x": 1, "y": 0}},
    ], [{"from_step": "src", "to_step": "f"}])
    r = authed_client.post(f"/api/workflows/{reader}/test")
    assert r.json()["status"] == "success", r.json()["test_results"]["error"]


def test_a_real_failure_still_blocks_publish(authed_client, orders_csv):
    broken = _save(authed_client, "broken", [
        {"id": "src", "type": "csv_source", "label": "orders",
         "params": {"file_path": orders_csv}, "position": {"x": 0, "y": 0}},
        {"id": "f", "type": "filter", "label": "bad",
         "params": {"mode": "expression", "condition": "no_such_column > 1"},
         "position": {"x": 1, "y": 0}},
    ], [{"from_step": "src", "to_step": "f"}])
    r = authed_client.post(f"/api/workflows/{broken}/test")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error"
    assert authed_client.post(f"/api/workflows/{broken}/publish").status_code == 400
