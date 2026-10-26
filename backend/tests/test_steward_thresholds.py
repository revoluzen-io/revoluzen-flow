"""The shared threshold resolver.

This logic used to be copy-pasted into node_cardinality, storage_intel and
archeologist. Testing it once, directly, is the point of factoring it out —
the per-detector tests cover that each detector *uses* a threshold; these
cover what the resolution itself promises.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.steward.thresholds import (
    effective_threshold,
    merge_threshold_specs,
    threshold_resolver,
)

pytestmark = pytest.mark.unit

SPEC = {
    "demo_kind": [
        {"key": "ratio", "label": "Ratio", "default": 2.5, "min": 0, "max": 10, "step": 0.5},
        {"key": "floor", "label": "Floor", "default": 100, "min": 0, "max": 1000, "step": 10},
    ],
}


def eff(thresholds, key="ratio", kind="demo_kind"):
    return effective_threshold(SPEC, thresholds, kind, key)


# ── Defaults ─────────────────────────────────────────────────────────

def test_no_override_uses_the_declared_default():
    assert eff(None) == 2.5
    assert eff({}) == 2.5
    assert eff({"demo_kind": {}}) == 2.5
    assert eff({"other_kind": {"ratio": 9}}) == 2.5


def test_default_is_returned_as_a_float():
    assert isinstance(eff(None, key="floor"), float)
    assert eff(None, key="floor") == 100.0


def test_unknown_kind_or_key_yields_zero_not_an_error():
    """Total by design: a scan must not die on a stale settings key."""
    assert effective_threshold(SPEC, None, "no_such_kind", "ratio") == 0.0
    assert effective_threshold(SPEC, None, "demo_kind", "no_such_key") == 0.0


# ── Overrides ────────────────────────────────────────────────────────

def test_override_wins():
    assert eff({"demo_kind": {"ratio": 7}}) == 7.0


def test_numeric_string_override_is_accepted():
    assert eff({"demo_kind": {"ratio": "7.5"}}) == 7.5


def test_zero_is_honoured_not_treated_as_missing():
    """An operator setting a gate to 0 means 'disable it', not 'use default'.

    `if v is not None` rather than a truthiness check is what makes this
    work; a falsy test here would silently restore the default.
    """
    assert eff({"demo_kind": {"ratio": 0}}) == 0.0


def test_none_override_falls_back():
    assert eff({"demo_kind": {"ratio": None}}) == 2.5


@pytest.mark.parametrize("junk", ["soon", "", [], {}, object()])
def test_unparseable_override_falls_back_to_the_default(junk):
    assert eff({"demo_kind": {"ratio": junk}}) == 2.5


def test_non_dict_override_for_a_kind_falls_back():
    assert eff({"demo_kind": None}) == 2.5


def test_each_key_resolves_independently():
    thr = {"demo_kind": {"ratio": 9}}
    assert eff(thr, key="ratio") == 9.0
    assert eff(thr, key="floor") == 100.0   # untouched


# ── The bound resolver ───────────────────────────────────────────────

def test_resolver_binds_its_spec():
    _eff = threshold_resolver(SPEC)
    assert _eff(None, "demo_kind", "ratio") == 2.5
    assert _eff({"demo_kind": {"ratio": 4}}, "demo_kind", "ratio") == 4.0


def test_two_resolvers_do_not_share_state():
    other = {"demo_kind": [{"key": "ratio", "default": 99}]}
    a, b = threshold_resolver(SPEC), threshold_resolver(other)
    assert a(None, "demo_kind", "ratio") == 2.5
    assert b(None, "demo_kind", "ratio") == 99.0


def test_the_real_detectors_resolve_their_own_defaults():
    """Guards the rewiring: each module must still read ITS spec."""
    from revoluzen_flow.steward.archeologist import _eff as arch_eff
    from revoluzen_flow.steward.node_cardinality import _eff as card_eff
    from revoluzen_flow.steward.storage_intel import _eff as store_eff

    assert store_eff(None, "orphaned_table", "min_age_hours") == 168.0
    assert arch_eff(None, "duplicate_source", "min_pipelines") == 2.0
    assert card_eff(None, "join_explosion", "ratio") > 0
    # And no module can see another's kinds.
    assert arch_eff(None, "orphaned_table", "min_age_hours") == 0.0
    assert store_eff(None, "duplicate_source", "min_pipelines") == 0.0


# ── Merging specs for the Coverage page ──────────────────────────────

def test_merge_combines_disjoint_specs():
    merged = merge_threshold_specs({"a": [{"key": "x", "default": 1}]},
                                   {"b": [{"key": "y", "default": 2}]})
    assert sorted(merged) == ["a", "b"]


def test_merge_rejects_a_duplicate_kind():
    """A silent last-writer-wins would drop a detector's knobs from the UI."""
    with pytest.raises(ValueError, match="same kind"):
        merge_threshold_specs({"a": [{"key": "x", "default": 1}]},
                              {"a": [{"key": "y", "default": 2}]})


def test_the_real_specs_do_not_collide():
    from revoluzen_flow.steward.archeologist import THRESHOLD_SPEC as A
    from revoluzen_flow.steward.node_cardinality import THRESHOLD_SPEC as C
    from revoluzen_flow.steward.storage_intel import THRESHOLD_SPEC as S

    merged = merge_threshold_specs(C, S, A)
    assert "orphaned_table" in merged
    assert "duplicate_source" in merged
    assert "join_explosion" in merged
