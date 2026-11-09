"""The Steward's scan must see each pipeline's real steps.

The other Steward tests stub ``_workflows_for_scan``, which is how this stayed
hidden: the real function read the store's summary rows, which carry a step count
but no steps, so every live scan saw empty pipelines and found no duplicates.
"""
from tests.conftest_fixtures_v2 import (  # noqa: F401
    data_dir, db_fixture, app_v2, client, admin_token, authed_client,
)


def _pipeline(authed_client, name, file="orders.csv", table="shared_output"):
    created = authed_client.post("/api/workflows/", json={
        "name": name,
        "steps": [
            {"id": "src", "type": "source", "params": {"file_path": file, "connector_type": "csv"}},
            {"id": "out", "type": "local_table_sink", "params": {"schema_name": "default", "table_name": table}},
        ],
        "connections": [{"from_step": "src", "to_step": "out"}],
    })
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _open_findings(authed_client):
    scanned = authed_client.post("/api/steward/scan")
    assert scanned.status_code == 200, scanned.text
    return authed_client.get("/api/steward/findings", params={"status": "open"}).json()["findings"]


def _named(findings, kind):
    return {w["name"] for f in findings if f["kind"] == kind for w in f["evidence"].get("workflows", [])}


def test_two_pipelines_reading_one_file_are_reported(authed_client):
    _pipeline(authed_client, "Orders by region", file="a.csv", table="by_region")
    _pipeline(authed_client, "Orders by customer", file="a.csv", table="by_customer")
    assert {"Orders by region", "Orders by customer"} <= _named(_open_findings(authed_client), "duplicate_source")


def test_two_pipelines_with_the_same_source_and_target_are_reported(authed_client):
    _pipeline(authed_client, "Revenue rollup", file="b.csv", table="rollup")
    _pipeline(authed_client, "Revenue rollup, again", file="b.csv", table="rollup")
    assert any(
        "Revenue rollup" in f["body"] and "Revenue rollup, again" in f["body"]
        for f in _open_findings(authed_client) if f["kind"] == "duplicate_pipeline"
    )


def test_a_pipeline_with_its_own_source_is_not_reported(authed_client):
    _pipeline(authed_client, "Only one", file="c.csv", table="only_one")
    assert "Only one" not in _named(_open_findings(authed_client), "duplicate_source")
