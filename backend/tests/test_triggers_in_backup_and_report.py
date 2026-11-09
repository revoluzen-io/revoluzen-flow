"""What the Triggers page holds also leaves through the backup export and the report."""

from __future__ import annotations

from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def _endpoint_and_key(authed_client, name):
    created = authed_client.post("/api/workflows", json={
        "name": name,
        "steps": [{"id": "src", "type": "source", "params": {"file_path": "orders.csv"}}],
        "connections": [],
    })
    assert created.status_code in (200, 201), created.text
    body = created.json()
    workflow_id = body.get("id") or body["workflow"]["id"]
    endpoint = authed_client.post("/api/gateway/endpoints", json={
        "workflow_id": workflow_id, "path": name, "name": name,
    })
    assert endpoint.status_code == 200, endpoint.text
    key = authed_client.post("/api/gateway/keys", json={"name": f"{name}-key"})
    assert key.status_code == 200, key.text
    return workflow_id, endpoint.json(), key.json()


def _collect(**kwargs):
    from revoluzen_flow.main import app_state
    from revoluzen_flow.reports.inventory import InventoryCollector

    return InventoryCollector(app_state, workspace_id="default", tier="free", **kwargs).collect()


def test_the_json_export_carries_endpoints_and_keys_as_hashes(authed_client):
    _, endpoint, key = _endpoint_and_key(authed_client, "export-probe")
    exported = authed_client.get("/api/backup/export")
    assert exported.status_code == 200, exported.text
    tables = exported.json()["_tables"]
    assert endpoint["id"] in {row["id"] for row in tables["published_endpoints"]}
    assert key["id"] in {row["id"] for row in tables["api_keys"]}
    assert key["api_key"] not in exported.text


def test_the_export_works_before_the_gateway_tables_exist(tmp_path):
    from revoluzen_flow.storage.database import Database

    exported = Database(str(tmp_path / "fresh.db")).export_all()
    assert "api_keys" not in exported["_tables"]


def test_the_report_lists_endpoints_and_masks_keys(authed_client):
    workflow_id, endpoint, key = _endpoint_and_key(authed_client, "report-probe")
    report = _collect(scope="admin")

    listed = next(e for e in report.trigger_endpoints if e.id == endpoint["id"])
    assert listed.path == "report-probe"
    assert listed.workflow_id == workflow_id
    assert listed.require_api_key is True
    assert listed.enabled is True

    masked = next(k for k in report.api_keys if k.id == key["id"])
    assert masked.key_masked.endswith("••••••••")
    assert key["api_key"] not in str(report.to_dict())

    assert report.totals["endpoints"] >= 1
    assert report.totals["api_keys_active"] >= 1


def test_a_pipeline_report_keeps_its_own_endpoints_and_no_keys(authed_client):
    from revoluzen_flow.ir.schema import PipelineStatus
    from revoluzen_flow.main import app_state

    workflow_id, endpoint, _ = _endpoint_and_key(authed_client, "report-scoped")
    # The report covers published pipelines only.
    app_state["store"].update_status(workflow_id, PipelineStatus.PUBLISHED, published_by="test")
    _, other, _ = _endpoint_and_key(authed_client, "report-other")

    report = _collect(scope="pipeline", scope_id_filter=workflow_id)
    ids = {e.id for e in report.trigger_endpoints}
    assert endpoint["id"] in ids
    assert other["id"] not in ids
    assert report.api_keys == []


def test_both_report_formats_render_the_section(authed_client):
    from revoluzen_flow.reports.inventory_docx import render_docx
    from revoluzen_flow.reports.inventory_pdf import render_pdf

    _endpoint_and_key(authed_client, "report-render")
    report = _collect(scope="admin")
    assert render_pdf(report).startswith(b"%PDF")
    assert render_docx(report).startswith(b"PK")
