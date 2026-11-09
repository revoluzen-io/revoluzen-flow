"""resolve_input_path — the read-side path fallback chain.

Covers the 2026-11-03 addition: a relative path that exists neither under
the data dir nor under the CWD still resolves when it exists under the
source checkout root. That is the `start.ps1` case (uvicorn runs with
CWD=<root>/backend) for the sample pack's
`samples/free-api-pipelines/data/*.csv` inputs.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from revoluzen_flow.nodes import _path_utils
from revoluzen_flow.nodes._path_utils import resolve_input_path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_absolute_path_is_returned_unchanged(tmp_path):
    target = tmp_path / "abs.csv"
    target.write_text("a\n1\n", encoding="utf-8")
    assert resolve_input_path(str(target), str(tmp_path / "data")) == str(target)


def test_data_dir_wins_over_cwd_and_repo_root(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cwd = tmp_path / "cwd"
    for d in (data_dir, cwd):
        (d / "x").mkdir(parents=True)
        (d / "x" / "f.csv").write_text("a\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    assert resolve_input_path("x/f.csv", str(data_dir)) == os.path.join(str(data_dir), "x/f.csv")


def test_cwd_fallback(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cwd = tmp_path / "cwd"
    (cwd / "x").mkdir(parents=True)
    (cwd / "x" / "f.csv").write_text("a\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    assert resolve_input_path("x/f.csv", str(data_dir)) == os.path.abspath("x/f.csv")


def test_repo_root_fallback_when_cwd_is_backend(tmp_path, monkeypatch):
    """The start.ps1 layout: CWD is <root>/backend, data dir is elsewhere,
    the file only exists under <root>."""
    rel = "samples/free-api-pipelines/data/orders.csv"
    assert (REPO_ROOT / rel).is_file(), "sample pack data must be checked in"
    monkeypatch.chdir(REPO_ROOT / "backend")
    resolved = resolve_input_path(rel, str(tmp_path / "data"))
    assert Path(resolved) == REPO_ROOT / rel


def test_repo_root_constant_points_at_checkout():
    assert Path(_path_utils._REPO_ROOT) == REPO_ROOT


def test_missing_everywhere_returns_data_dir_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = str(tmp_path / "data")
    assert resolve_input_path("nope/missing.csv", data_dir) == os.path.join(data_dir, "nope/missing.csv")


@pytest.mark.parametrize(
    "rel",
    [
        "samples/free-api-pipelines/data/orders.csv",
        "samples/free-api-pipelines/data/customers.csv",
        "samples/free-api-pipelines/data/daily_sales.csv",
        "samples/free-api-pipelines/data/baseline_sales.csv",
        "samples/free-api-pipelines/data/signups.csv",
        "samples/free-api-pipelines/data/products.xml",
    ],
)
def test_sample_pack_inputs_are_shipped(rel):
    assert (REPO_ROOT / rel).stat().st_size > 100
