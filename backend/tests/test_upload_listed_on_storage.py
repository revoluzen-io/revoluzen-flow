"""A file uploaded from a Source node appears on the Storage page straight away.

It used to be written to disk without an index row, so the Files tab stayed
empty until someone pressed Rescan. Replacing a node's file deletes the old one
from disk, so its row has to go too, or Storage lists a file that isn't there.
"""

from __future__ import annotations

import os

import pytest

from revoluzen_flow.api import uploads
from revoluzen_flow.datastore.models import OBJECT_KIND_FILE, StorageObject


class _Store:
    def __init__(self):
        self.rows: dict[str, StorageObject] = {}

    def save_object(self, obj):
        self.rows[obj.id] = obj
        return obj

    def list_objects(self, workspace_id, *, kind=None, include_deleted=False):
        return [r for r in self.rows.values() if r.workspace_id == workspace_id and (kind is None or r.kind == kind)]

    def hard_delete_object(self, object_id):
        return self.rows.pop(object_id, None) is not None


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr("revoluzen_flow.datastore.store.get_store", lambda: s)
    return s


def _csv(tmp_path, name="orders-1.csv"):
    path = tmp_path / name
    path.write_text("order_id,amount\n1,10\n2,20\n", encoding="utf-8")
    return str(path)


def test_upload_gets_a_row_under_its_original_name(store, tmp_path):
    path = _csv(tmp_path)
    uploads._index_upload("default", "orders.csv", "uploads/default/orders-1.csv", path, os.path.getsize(path), None)
    row = next(iter(store.rows.values()))
    assert row.name == "orders.csv"
    assert row.path == "uploads/default/orders-1.csv"
    assert row.kind == OBJECT_KIND_FILE
    assert (row.row_count, row.column_count) == (2, 2)


def test_replacing_a_file_drops_the_row_of_the_deleted_one(store, tmp_path):
    old = StorageObject(workspace_id="default", kind=OBJECT_KIND_FILE, name="orders.csv", path="uploads/default/orders-0.csv")
    other = StorageObject(workspace_id="default", kind=OBJECT_KIND_FILE, name="customers.csv", path="uploads/default/customers-0.csv")
    store.rows = {old.id: old, other.id: other}
    path = _csv(tmp_path)
    uploads._index_upload("default", "orders.csv", "uploads/default/orders-1.csv", path, os.path.getsize(path), "uploads/default/orders-0.csv")
    assert sorted(r.path for r in store.rows.values()) == ["uploads/default/customers-0.csv", "uploads/default/orders-1.csv"]


def test_uploading_the_same_name_again_keeps_one_row(store, tmp_path):
    """The Source panel's upload keeps the file's name and overwrites it."""
    path = _csv(tmp_path, "orders.csv")
    for _ in range(2):
        uploads._index_upload("default", "orders.csv", "orders.csv", path, os.path.getsize(path), None)
    assert [r.path for r in store.rows.values()] == ["orders.csv"]


def test_an_index_failure_never_fails_the_upload(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("no database")
    monkeypatch.setattr("revoluzen_flow.datastore.store.get_store", boom)
    path = _csv(tmp_path)
    uploads._index_upload("default", "orders.csv", "uploads/default/orders-1.csv", path, os.path.getsize(path), None)
