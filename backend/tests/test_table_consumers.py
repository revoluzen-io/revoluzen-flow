"""A managed table's consumers are the pipelines that read it.

The count used to include the pipeline writing to the table, so a table nobody
read said "consumed by 1 pipeline".
"""

from __future__ import annotations

from types import SimpleNamespace

from revoluzen_flow.api import storage


def _provenance(monkeypatch, usage):
    table = SimpleNamespace(schema_name="default", name="employee_master", prep_source_object_id=None,
                            created_from_object_id=None, prep_workflow_id=None, prep_recipe=None)
    monkeypatch.setattr(storage, "get_datastore", lambda: SimpleNamespace(get_table=lambda *a, **k: table))
    monkeypatch.setattr(storage, "compute_workspace_usage_cached", lambda ws: {"tables": {"t1": usage}})
    return storage.get_table_provenance("t1", workspace_id="default")


def test_the_writer_is_not_a_consumer(monkeypatch):
    result = _provenance(monkeypatch, [
        {"workflow_id": "w0", "name": "Employee Master", "role": "sink"},
        {"workflow_id": "w1", "name": "Payroll", "role": "source"},
        {"workflow_id": "w2", "name": "Headcount", "role": "source"},
    ])
    assert result["consumer_count"] == 2
    assert [c["name"] for c in result["consumers"]] == ["Payroll", "Headcount"]


def test_a_table_only_written_to_has_no_consumers(monkeypatch):
    result = _provenance(monkeypatch, [{"workflow_id": "w0", "name": "Employee Master", "role": "sink"}])
    assert result["consumer_count"] == 0


def test_a_generic_reference_still_counts(monkeypatch):
    result = _provenance(monkeypatch, [{"workflow_id": "w1", "name": "Payroll", "role": "generic"}])
    assert result["consumer_count"] == 1
