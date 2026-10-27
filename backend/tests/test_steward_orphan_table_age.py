"""The orphan-table rule must not fire on a table you just created.

The detector used to emit the instant a table existed and no saved pipeline
referenced it. That made it wrong about its most common case: promoting a
file to a managed table NECESSARILY produces an unreferenced table, because
you cannot build the pipeline that reads it until it exists. A user
following the documented upload -> promote -> build flow was told their
brand-new table was "most likely left over from a deleted or edited
pipeline" — a cause the detector had never checked.

These tests pin the gate and the honesty of the text. The first one is the
one that matters: it fails against the old detector.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from revoluzen_flow.steward.models import FindingKind
from revoluzen_flow.steward.storage_intel import (
    THRESHOLD_SPEC,
    _ORPHAN_MIN_AGE_HOURS,
    detect_orphaned_tables,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def table(name="tbl_people", *, age_hours=0.0, updated_age_hours=None, **kw):
    created = NOW - timedelta(hours=age_hours)
    updated = NOW - timedelta(
        hours=age_hours if updated_age_hours is None else updated_age_hours
    )
    return SimpleNamespace(
        schema_name="default", name=name, row_count=10,
        created_at=created, updated_at=updated, **kw,
    )


def run(tables, workflows=None, **kw):
    return detect_orphaned_tables(workflows or [], tables, now=NOW, **kw)


# ── The regression ───────────────────────────────────────────────────

def test_freshly_promoted_table_is_not_flagged():
    """THE bug. A table created seconds ago is work in progress."""
    assert run([table(age_hours=0.0)]) == []


@pytest.mark.parametrize("age", [0.0, 1.0, 24.0, 167.0])
def test_nothing_fires_inside_the_grace_period(age):
    assert run([table(age_hours=age)]) == [], f"fired at {age}h, under the 168h gate"


def test_fires_once_the_table_is_genuinely_stale():
    found = run([table(age_hours=200.0)])
    assert len(found) == 1
    assert found[0].kind == FindingKind.ORPHANED_TABLE


def test_default_is_seven_days():
    assert _ORPHAN_MIN_AGE_HOURS == 168.0
    spec = THRESHOLD_SPEC[FindingKind.ORPHANED_TABLE.value][0]
    assert spec["key"] == "min_age_hours"
    assert spec["default"] == 168.0


# ── Age is the most recent touch, not just creation ──────────────────

def test_recently_written_old_table_is_not_flagged():
    """Created long ago but rewritten yesterday — that table is in use.

    Taking created_at alone would flag it, which is why the detector uses
    max(created_at, updated_at).
    """
    assert run([table(age_hours=5000.0, updated_age_hours=20.0)]) == []


def test_old_and_untouched_is_flagged():
    assert len(run([table(age_hours=5000.0, updated_age_hours=900.0)])) == 1


# ── Unknown age must not become "old enough" ─────────────────────────

def test_table_without_timestamps_is_not_flagged():
    """Fail closed: the finding asserts an age, so no age means no claim."""
    bare = SimpleNamespace(schema_name="default", name="legacy", row_count=1)
    assert run([bare]) == []


def test_unparseable_timestamp_is_not_flagged():
    junk = SimpleNamespace(schema_name="default", name="junk", row_count=1,
                           created_at="not a date", updated_at=None)
    assert run([junk]) == []


def test_iso_string_timestamps_are_understood():
    """Rows that have been through JSON carry strings, not datetimes."""
    iso = SimpleNamespace(
        schema_name="default", name="viajson", row_count=1,
        created_at=(NOW - timedelta(hours=400)).isoformat(),
        updated_at=(NOW - timedelta(hours=400)).isoformat().replace("+00:00", "Z"),
    )
    assert len(run([iso])) == 1


def test_naive_datetime_is_read_as_utc():
    naive = SimpleNamespace(
        schema_name="default", name="naive", row_count=1,
        created_at=(NOW - timedelta(hours=400)).replace(tzinfo=None),
        updated_at=(NOW - timedelta(hours=400)).replace(tzinfo=None),
    )
    assert len(run([naive])) == 1


# ── The threshold override ───────────────────────────────────────────

def test_operator_can_lower_the_gate():
    thr = {FindingKind.ORPHANED_TABLE.value: {"min_age_hours": 1}}
    assert len(run([table(age_hours=2.0)], thresholds=thr)) == 1


def test_operator_can_raise_the_gate():
    thr = {FindingKind.ORPHANED_TABLE.value: {"min_age_hours": 8760}}
    assert run([table(age_hours=200.0)], thresholds=thr) == []


def test_garbage_override_falls_back_to_the_default():
    """An unparseable override must restore 168h, not disable the gate."""
    thr = {FindingKind.ORPHANED_TABLE.value: {"min_age_hours": "soon"}}
    assert run([table(age_hours=100.0)], thresholds=thr) == []      # under 168h
    assert len(run([table(age_hours=200.0)], thresholds=thr)) == 1  # over 168h


# ── Still honours the original conditions ────────────────────────────

def test_referenced_table_is_never_flagged_however_old():
    wf = [{"nodes": [{"type": "local_table_source",
                      "params": {"schema_name": "default", "table_name": "tbl_people"}}]}]
    assert run([table(age_hours=9000.0)], workflows=wf) == []


def test_suppressed_signature_is_respected():
    sig = {"orphan-table::default.tbl_people"}
    assert run([table(age_hours=9000.0)], suppressed_signatures=sig) == []


# ── The text must not assert a cause it did not check ────────────────

def test_body_states_the_observed_age_and_does_not_guess():
    f = run([table(age_hours=24 * 30)])[0]
    body = f.body.lower()
    assert "most likely left over" not in body, (
        "the detector is asserting a cause it never checked"
    )
    assert "has not been written to for" in body
    assert "saved pipeline" in body, "must disclose it only sees saved pipelines"
    assert "weeks" in f.title or "days" in f.title


def test_evidence_carries_the_age_it_judged_on():
    f = run([table(age_hours=300.0)])[0]
    assert f.evidence["age_hours"] == pytest.approx(300.0, abs=0.1)
    assert f.evidence["min_age_hours"] == 168.0
