"""Regression tests for the 2026-10-23 upload path-traversal fix.

`POST /api/upload` joined the caller-supplied `UploadFile.filename` straight
onto the data directory and had no auth dependency. An unauthenticated
request with `filename="../escaped.csv"` wrote a file into the data
directory's PARENT -- reproduced on disk before the fix.

Two separate properties are covered here, because the fix has two halves
and either one regressing is a vulnerability on its own:

  1. The posture gate. Anonymous callers are refused under
     REVOLUZEN_FLOW_SECURITY_MODE=server.
  2. The path handling. Even for a legitimate, authenticated caller a
     traversal filename must not escape the data directory.

Property 2 is the one that is easy to lose: it would be invisible in any
test that only checks the 401, since auth short-circuits before the path
is ever built. The unit tests below exercise it directly.
"""

from __future__ import annotations

import os

import pytest

from revoluzen_flow.common.safe_paths import (
    UnsafePathError,
    resolve_under,
    safe_basename,
)


# ── safe_basename ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.csv", "report.csv"),
        ("../escaped.csv", "escaped.csv"),
        ("..\\escaped.csv", "escaped.csv"),
        ("../../../../etc/passwd", "passwd"),
        ("a/../../escaped.csv", "escaped.csv"),
        ("dir/sub/file.json", "file.json"),
        ("dir\\sub\\file.json", "file.json"),
        (r"C:\Windows\Temp\x.csv", "x.csv"),
        ("C:x.csv", "x.csv"),            # drive-relative, no separator
        ("//server/share/y.csv", "y.csv"),
        ("  spaced.csv  ", "spaced.csv"),
        ("weird\x00name.csv", "weirdname.csv"),
    ],
)
def test_safe_basename_reduces_to_one_component(raw, expected):
    out = safe_basename(raw)
    assert out == expected
    assert os.sep not in out and "/" not in out and "\\" not in out
    assert out not in (".", "..")


@pytest.mark.parametrize("raw", ["", None])
def test_safe_basename_empty_uses_default(raw):
    assert safe_basename(raw, default="upload.csv") == "upload.csv"


@pytest.mark.parametrize("raw", ["..", ".", "../", "../..", "/", "\\"])
def test_safe_basename_rejects_pure_traversal(raw):
    with pytest.raises(UnsafePathError):
        safe_basename(raw)


@pytest.mark.parametrize("raw", ["CON.csv", "nul.txt", "LPT1.json", "aux"])
def test_safe_basename_rejects_windows_device_names(raw):
    """These open a device rather than a file, on any Windows host."""
    with pytest.raises(UnsafePathError):
        safe_basename(raw)


# ── resolve_under ───────────────────────────────────────────────────────

def test_resolve_under_accepts_a_plain_name(tmp_path):
    out = resolve_under(str(tmp_path), "ok.csv")
    assert out == os.path.realpath(str(tmp_path / "ok.csv"))


def test_resolve_under_accepts_the_root_itself(tmp_path):
    assert resolve_under(str(tmp_path)) == os.path.realpath(str(tmp_path))


@pytest.mark.parametrize("escape", ["../out.csv", "../../out.csv", "sub/../../out.csv"])
def test_resolve_under_rejects_escapes(tmp_path, escape):
    with pytest.raises(UnsafePathError):
        resolve_under(str(tmp_path), escape)


def test_resolve_under_rejects_a_sibling_with_a_shared_prefix(tmp_path):
    """`/data-evil` must not pass a containment check against `/data`."""
    root = tmp_path / "data"
    root.mkdir()
    (tmp_path / "data-evil").mkdir()
    with pytest.raises(UnsafePathError):
        resolve_under(str(root), "../data-evil/x.csv")


# ── the two together, as the endpoint uses them ─────────────────────────

@pytest.mark.parametrize(
    "hostile",
    [
        "../escaped.csv",
        "..\\escaped.csv",
        "../../escaped.csv",
        "a/../../escaped.csv",
        r"C:\Windows\Temp\escaped.csv",
        "/abs-escaped.csv",
    ],
)
def test_endpoint_pipeline_keeps_every_hostile_name_inside(tmp_path, hostile):
    """safe_basename then resolve_under -- the exact order in upload_file.

    Each of these previously produced a write outside the data directory
    (or, for the absolute POSIX form, a 500).
    """
    dest = resolve_under(str(tmp_path), safe_basename(hostile))
    root = os.path.realpath(str(tmp_path))
    assert dest.startswith(root + os.sep)
    assert os.path.dirname(dest) == root
