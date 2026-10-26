"""The duplicate-source rule gets a tunable minimum.

Firing at exactly two pipelines is right for most workspaces and wrong for
the ones that fan out on purpose — a raw table read by a daily job and a
monthly rollup is deliberate, not waste. Before this knob the only way to
quiet that was to dismiss each signature individually.

The default is 2, so this changes nothing unless an operator opts in. That
is the first thing these tests pin.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.steward.archeologist import (
    THRESHOLD_SPEC,
    _DUP_SOURCE_MIN_PIPELINES,
    detect_duplicate_sources,
)
from revoluzen_flow.steward.models import FindingKind

pytestmark = pytest.mark.unit

KIND = FindingKind.DUPLICATE_SOURCE.value


def wf(wf_id: str, path: str = "/data/raw.csv"):
    """A workflow with one CSV source — same path means same signature.

    Uses the Revoluzen Flow step format (top-level `type` + `params`), which
    is what WorkflowStore writes. Mixing it with the React Flow shape
    (`data.params`) yields empty params and no signature at all — which is
    exactly the 2026-06-06 bug `_step_type_and_params` was added to fix.
    """
    return {
        "id": wf_id,
        "name": f"pipeline {wf_id}",
        "nodes": [{
            "id": f"n-{wf_id}",
            "type": "csv_source",
            "label": "Read raw",
            "params": {"file_path": path, "connector_type": "csv"},
        }],
    }


def run(workflows, **kw):
    return detect_duplicate_sources(workflows, **kw)


def dup_findings(workflows, **kw):
    return [f for f in run(workflows, **kw) if f.kind == FindingKind.DUPLICATE_SOURCE]


# ── The default must not change behaviour ────────────────────────────

def test_default_is_two():
    assert _DUP_SOURCE_MIN_PIPELINES == 2
    spec = THRESHOLD_SPEC[KIND][0]
    assert spec["key"] == "min_pipelines"
    assert spec["default"] == 2


def test_two_pipelines_still_flag_by_default():
    """Unchanged behaviour — the whole point of defaulting to 2."""
    assert len(dup_findings([wf("a"), wf("b")])) == 1


def test_one_pipeline_never_flags():
    assert dup_findings([wf("a")]) == []


def test_same_source_twice_in_one_workflow_is_not_a_duplicate():
    """Distinct WORKFLOWS, not distinct nodes — that is the validator's job."""
    one = wf("a")
    one["nodes"].append({
        "id": "n2", "type": "csv_source", "label": "Read raw again",
        "params": {"file_path": "/data/raw.csv", "connector_type": "csv"},
    })
    assert dup_findings([one]) == []


# ── The override ─────────────────────────────────────────────────────

def test_raising_the_minimum_silences_a_two_way_share():
    thr = {KIND: {"min_pipelines": 3}}
    assert dup_findings([wf("a"), wf("b")], thresholds=thr) == []


def test_raising_the_minimum_still_catches_real_sprawl():
    thr = {KIND: {"min_pipelines": 3}}
    found = dup_findings([wf("a"), wf("b"), wf("c")], thresholds=thr)
    assert len(found) == 1
    assert "3 pipelines" in found[0].title


def test_minimum_can_never_drop_below_two():
    """1 would make every single source a 'duplicate' of itself."""
    for bad in (0, 1, -5):
        thr = {KIND: {"min_pipelines": bad}}
        assert dup_findings([wf("a")], thresholds=thr) == [], (
            f"min_pipelines={bad} flagged a source used by ONE pipeline"
        )
        # And 2 still behaves normally rather than being disabled.
        assert len(dup_findings([wf("a"), wf("b")], thresholds=thr)) == 1


def test_garbage_override_falls_back_to_the_default():
    thr = {KIND: {"min_pipelines": "lots"}}
    assert len(dup_findings([wf("a"), wf("b")], thresholds=thr)) == 1


def test_unrelated_detector_override_is_ignored():
    thr = {"orphaned_table": {"min_age_hours": 1}}
    assert len(dup_findings([wf("a"), wf("b")], thresholds=thr)) == 1


# ── The finding explains the knob ────────────────────────────────────

def test_body_points_at_the_threshold_instead_of_only_dismissal():
    f = dup_findings([wf("a"), wf("b")])[0]
    assert "Coverage page" in f.body
    assert f.evidence["min_pipelines"] == 2


def test_evidence_records_the_effective_minimum():
    thr = {KIND: {"min_pipelines": 4}}
    f = dup_findings([wf("a"), wf("b"), wf("c"), wf("d")], thresholds=thr)[0]
    assert f.evidence["min_pipelines"] == 4


# ── Suppression still wins ───────────────────────────────────────────

def test_suppressed_signature_beats_a_lowered_threshold():
    found = dup_findings([wf("a"), wf("b")])
    sig = found[0].evidence["source_signature"]
    assert dup_findings([wf("a"), wf("b")], suppressed_signatures={sig}) == []
