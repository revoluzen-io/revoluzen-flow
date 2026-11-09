"""The File System step reads a relative path as the data folder, like every file sink."""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest

from revoluzen_flow.nodes.control_extras import FileSystemNode


@pytest.fixture
def ctx(tmp_path):
    return SimpleNamespace(data_dir=str(tmp_path), conn=duckdb.connect(), scoped_name=lambda name: name)


def run(ctx, **params):
    return FileSystemNode(params).execute(ctx).fetchall()


def test_relative_paths_are_inside_the_data_folder(ctx, tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "a.csv").write_text("x\n1\n")
    rows = run(ctx, operation="copy_file", source="outputs/a.csv", destination="outbox/a.csv")
    assert (tmp_path / "outbox" / "a.csv").read_text() == "x\n1\n"
    assert rows[0][3] == "success"


def test_the_result_row_shows_the_path_as_given_not_the_server_folder(ctx, tmp_path):
    (tmp_path / "a.csv").write_text("x\n")
    rows = run(ctx, operation="copy_file", source="a.csv", destination="b.csv")
    assert rows[0][1:3] == ("a.csv", "b.csv")


def test_an_absolute_path_is_left_alone(ctx, tmp_path, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "a.csv").write_text("x\n")
    run(ctx, operation="move_file", source=str(elsewhere / "a.csv"), destination=str(elsewhere / "b.csv"))
    assert (elsewhere / "b.csv").exists()
    assert not (tmp_path / "b.csv").exists()


def test_copy_still_refuses_to_overwrite_unless_asked(ctx, tmp_path):
    (tmp_path / "a.csv").write_text("new\n")
    (tmp_path / "b.csv").write_text("old\n")
    with pytest.raises(FileExistsError):
        run(ctx, operation="copy_file", source="a.csv", destination="b.csv")
    assert (tmp_path / "b.csv").read_text() == "old\n"
    run(ctx, operation="copy_file", source="a.csv", destination="b.csv", overwrite=True)
    assert (tmp_path / "b.csv").read_text() == "new\n"
