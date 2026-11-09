"""A draft that names an uploaded file by its plain name must run.

Storage keeps uploads under a stamped name (tickets-20261103-071815.csv). The
Copilot, like a person, says "tickets.csv", so the applied pipeline failed with
"no files found" although the file was on the Storage page.
"""
from revoluzen_flow.api.planner import _point_at_uploaded_files
from revoluzen_flow.ir.schema import Step


class _Obj:
    def __init__(self, name, path):
        self.name, self.path = name, path


class _Store:
    def __init__(self, *objects):
        self.objects = list(objects)

    def list_objects(self, workspace_id, *, kind=None):
        return self.objects


def _source(**params):
    return Step(id="src", type="source", params=params)


def _patch(monkeypatch, *objects):
    monkeypatch.setattr("revoluzen_flow.datastore.store.get_store", lambda: _Store(*objects))


def test_a_plain_file_name_is_pointed_at_the_upload(monkeypatch):
    _patch(monkeypatch, _Obj("tickets.csv", "uploads/default/tickets-20261103-071815.csv"))
    step = _source(file_path="tickets.csv", path="tickets.csv")
    _point_at_uploaded_files([step], "default")
    assert step.params["file_path"] == "uploads/default/tickets-20261103-071815.csv"
    assert step.params["path"] == "uploads/default/tickets-20261103-071815.csv"


def test_the_newest_upload_of_that_name_wins(monkeypatch):
    # list_objects returns newest first.
    _patch(monkeypatch, _Obj("orders.csv", "uploads/default/orders-new.csv"), _Obj("orders.csv", "uploads/default/orders-old.csv"))
    step = _source(file_path="orders.csv")
    _point_at_uploaded_files([step], "default")
    assert step.params["file_path"] == "uploads/default/orders-new.csv"


def test_a_name_storage_does_not_know_is_left_alone(monkeypatch):
    _patch(monkeypatch, _Obj("tickets.csv", "uploads/default/tickets-1.csv"))
    step = _source(file_path="samples/orders.csv")
    _point_at_uploaded_files([step], "default")
    assert step.params["file_path"] == "samples/orders.csv"


def test_a_step_without_a_file_is_untouched(monkeypatch):
    _patch(monkeypatch, _Obj("tickets.csv", "uploads/default/tickets-1.csv"))
    step = Step(id="flt", type="filter", params={"condition": "a = 1"})
    _point_at_uploaded_files([step], "default")
    assert step.params == {"condition": "a = 1"}


def test_a_storage_failure_does_not_stop_the_apply(monkeypatch):
    def boom():
        raise RuntimeError("no database")
    monkeypatch.setattr("revoluzen_flow.datastore.store.get_store", boom)
    step = _source(file_path="tickets.csv")
    _point_at_uploaded_files([step], "default")
    assert step.params["file_path"] == "tickets.csv"
