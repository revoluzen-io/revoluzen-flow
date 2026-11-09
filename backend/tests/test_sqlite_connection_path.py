"""A SQLite connection made in the form has to actually connect.

The form saves the file under ``file_path``, and every reader looked for
``database`` or ``file``, so the connection test always failed and no pipeline
could read from it.
"""

from __future__ import annotations

import sqlite3

import pytest

from revoluzen_flow.connections.catalog import _sqlite_catalog
from revoluzen_flow.connections.sqlite_path import sqlite_db_path
from revoluzen_flow.connections.tester import ConnectionTester


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_DATA_DIR", str(tmp_path))
    monkeypatch.delitem(__import__("sys").modules, "revoluzen_flow.main", raising=False)
    con = sqlite3.connect(tmp_path / "warehouse.db")
    con.execute("create table returns (id integer)")
    con.commit()
    con.close()
    return tmp_path


@pytest.mark.parametrize("key", ["database", "file_path", "file", "path"])
def test_every_spelling_of_the_path_is_read(key, data_dir):
    assert sqlite_db_path({key: str(data_dir / "warehouse.db")}) == str(data_dir / "warehouse.db")


def test_no_path_is_none():
    assert sqlite_db_path({}) is None
    assert sqlite_db_path({"file_path": ""}) is None


def test_memory_and_absolute_paths_are_left_alone(data_dir):
    assert sqlite_db_path({"database": ":memory:"}) == ":memory:"
    assert sqlite_db_path({"file_path": str(data_dir / "other.db")}) == str(data_dir / "other.db")


def test_a_relative_path_means_the_data_folder(data_dir):
    assert sqlite_db_path({"file_path": "warehouse.db"}) == str(data_dir / "warehouse.db")


def test_a_new_relative_file_is_created_in_the_data_folder(data_dir):
    assert sqlite_db_path({"file_path": "new.db"}) == str(data_dir / "new.db")


def test_a_file_only_under_the_working_directory_still_opens(data_dir, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("cwd")
    (other / "old.db").touch()
    monkeypatch.chdir(other)
    assert sqlite_db_path({"file_path": "old.db"}) == "old.db"


def test_the_connection_test_passes_with_what_the_form_saves(data_dir):
    result = ConnectionTester().test_connection("sqlite", {"file_path": "warehouse.db"})
    assert result["success"] is True, result
    assert result["message"].endswith(": warehouse.db")  # the file's name, not the server's folders


def test_the_connection_test_explains_a_missing_path(data_dir):
    result = ConnectionTester().test_connection("sqlite", {})
    assert result["success"] is False
    assert "path" in result["message"]


def test_the_table_browser_reads_what_the_form_saves(data_dir):
    catalog = _sqlite_catalog({"file_path": "warehouse.db"})
    assert "returns" in str(catalog)
