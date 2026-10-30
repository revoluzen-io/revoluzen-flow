"""Tests for the pre-build packaging preflight (tools/package_preflight.py).

Asserts the current source tree is packageable (no failures) and that the
checker actually catches a missing package-data glob.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import package_preflight as pp  # noqa: E402


def test_reliable_source_assets_are_detected():
    # These are tracked source (present on any branch), so the checker must see
    # them regardless of which branch is checked out. (Whether package-data
    # globs / staged artifacts are present is branch-dependent, so we don't
    # assert fail == [] here — that's exactly the kind of drift the tool exists
    # to catch.)
    checks = pp.collect_checks()
    joined = " ".join(checks["ok"])
    # Matched by target, not binary name — the binary has been renamed before.
    assert "entry point: " in joined
    assert "revoluzen_flow.cli" in joined
    assert "connector manifests" in joined
    assert "vendored Swagger" in joined
    for bucket in ("ok", "warn", "fail"):
        assert all(isinstance(x, str) and x for x in checks[bucket])


def test_required_globs_are_the_ones_that_broke_before():
    # frontend_dist + manifests + swagger + docs are exactly the categories the
    # 1.0.0 "wheel with no UI/connectors" bug was missing.
    assert "frontend_dist/**/*" in pp._REQUIRED_GLOBS
    assert "connectors/manifests/*.json" in pp._REQUIRED_GLOBS
    assert "static/**/*" in pp._REQUIRED_GLOBS


def test_missing_glob_is_a_failure(monkeypatch):
    real = pp._REQUIRED_GLOBS[:]
    monkeypatch.setattr(pp, "_REQUIRED_GLOBS", real + ["connectors/nonexistent/*.json"])
    checks = pp.collect_checks()
    assert any("connectors/nonexistent/*.json" in f for f in checks["fail"])


# ── staged-but-stale (2026-10-30) ──────────────────────────────────────
#
# The staged-UI check tested presence only, and presence is not
# freshness. It reported "ok ... UI will ship" over a bundle two days
# older than the source it was staged from, so a local build shipped a
# UI missing that day's features while calling itself safe to build.


def _stage_tree(root, *, built_at, staged_at):
    """A source build and a staged copy with controlled timestamps."""
    import os

    src = root / "frontend" / "dist"
    staged = root / "backend" / "revoluzen_flow" / "frontend_dist"
    for d in (src, staged):
        (d / "assets").mkdir(parents=True)
        (d / "index.html").write_text("<html></html>", encoding="utf-8")
        (d / "assets" / "index.js").write_text("//", encoding="utf-8")
    for d, ts in ((src, built_at), (staged, staged_at)):
        for p in d.rglob("*"):
            os.utime(p, (ts, ts))
    return src, staged


def test_a_staged_bundle_older_than_the_build_is_reported(tmp_path, monkeypatch):
    _stage_tree(tmp_path, built_at=2_000_000_000, staged_at=1_999_000_000)
    monkeypatch.setattr(pp, "REPO", tmp_path)
    monkeypatch.setattr(pp, "PKG", tmp_path / "backend" / "revoluzen_flow")

    msg = pp._stale_against_source()
    assert msg and "STALE" in msg
    # Must name the fix, not just the problem.
    assert "stage_frontend.py" in msg


def test_a_freshly_staged_bundle_is_not_reported(tmp_path, monkeypatch):
    # Staged a few seconds AFTER the build, which is the normal case.
    _stage_tree(tmp_path, built_at=2_000_000_000, staged_at=2_000_000_005)
    monkeypatch.setattr(pp, "REPO", tmp_path)
    monkeypatch.setattr(pp, "PKG", tmp_path / "backend" / "revoluzen_flow")
    assert pp._stale_against_source() is None


def test_copy_skew_is_not_mistaken_for_staleness(tmp_path, monkeypatch):
    # Staging copies files, so the trees are never written at the same
    # instant. A sub-minute gap the WRONG way must stay silent, or the
    # check cries wolf on every correct build.
    _stage_tree(tmp_path, built_at=2_000_000_000, staged_at=1_999_999_970)
    monkeypatch.setattr(pp, "REPO", tmp_path)
    monkeypatch.setattr(pp, "PKG", tmp_path / "backend" / "revoluzen_flow")
    assert pp._stale_against_source() is None


def test_no_source_build_to_compare_is_not_staleness(tmp_path, monkeypatch):
    # A release checkout can legitimately have no frontend/dist. Calling
    # that "stale" would fail every such build for no reason.
    staged = tmp_path / "backend" / "revoluzen_flow" / "frontend_dist"
    staged.mkdir(parents=True)
    (staged / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(pp, "REPO", tmp_path)
    monkeypatch.setattr(pp, "PKG", tmp_path / "backend" / "revoluzen_flow")
    assert pp._stale_against_source() is None


def test_strict_turns_staleness_into_a_build_failure(tmp_path, monkeypatch):
    # release.yml runs --strict, so this is the gate that actually stops
    # a stale UI reaching PyPI.
    import shutil

    _stage_tree(tmp_path, built_at=2_000_000_000, staged_at=1_999_000_000)
    # collect_checks reads pyproject.toml first and returns early if it
    # cannot, so the temp tree needs the real one to reach the staged-UI
    # check at all.
    shutil.copy(pp.REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    monkeypatch.setattr(pp, "REPO", tmp_path)
    monkeypatch.setattr(pp, "PKG", tmp_path / "backend" / "revoluzen_flow")

    checks = pp.collect_checks(strict=True)
    assert any("STALE" in f for f in checks["fail"]), checks
