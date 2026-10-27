"""Shared resolution of operator-tunable detector thresholds.

A detector that exposes numeric knobs declares a ``THRESHOLD_SPEC``::

    THRESHOLD_SPEC = {
        FindingKind.ORPHANED_TABLE.value: [
            {"key": "min_age_hours", "label": "...", "default": 168.0,
             "min": 0, "max": 8760, "step": 24},
        ],
    }

The operator overrides a knob through
``StewardSettings.detectors[<kind>].thresholds[<key>]``, and the Coverage
page renders the spec so the knob is discoverable rather than a constant
only a reader of the source knows about.

This module exists because the resolution logic — "override if present and
parseable, otherwise the declared default" — was copy-pasted into
node_cardinality, then storage_intel, then archeologist. Three copies of a
function whose whole job is to behave identically everywhere is how the
fallback semantics quietly diverge: one copy starts treating ``0`` as
missing, or stops catching ``ValueError``, and two detectors disagree about
what an operator's setting means.
"""

from __future__ import annotations

from typing import Any, Callable

ThresholdSpec = dict[str, list[dict[str, Any]]]


def effective_threshold(
    spec: ThresholdSpec,
    thresholds: dict | None,
    kind_value: str,
    key: str,
) -> float:
    """The operator's override if present and parseable, else the default.

    Deliberately total: an unknown kind, an unknown key, a missing override
    map or an unparseable value all fall back rather than raise. A detector
    must keep detecting when its configuration is junk — the alternative is
    a scan that dies on a typo in a settings file.

    Note that a *present* override of ``0`` is honoured, not treated as
    missing. Only ``None`` falls through to the default, so an operator can
    legitimately disable an age or floor gate by setting it to zero.
    """
    default = next(
        (s["default"] for s in spec.get(kind_value, []) if s["key"] == key),
        0,
    )
    ov = (thresholds or {}).get(kind_value) or {}
    try:
        v = ov.get(key)
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def threshold_resolver(
    spec: ThresholdSpec,
) -> Callable[[dict | None, str, str], float]:
    """Bind a spec, returning the ``_eff(thresholds, kind, key)`` callable
    each detector module uses. Keeps call sites free of the spec argument —
    node_cardinality alone resolves seven knobs."""

    def _eff(thresholds: dict | None, kind_value: str, key: str) -> float:
        return effective_threshold(spec, thresholds, kind_value, key)

    return _eff


def merge_threshold_specs(*specs: ThresholdSpec) -> ThresholdSpec:
    """Combine every detector's spec for the Coverage page.

    Raises on a duplicate kind rather than letting the last writer win: two
    modules claiming the same finding kind means one detector's knobs vanish
    from the UI silently, and the operator sees settings that do nothing.
    """
    merged: ThresholdSpec = {}
    for spec in specs:
        clash = merged.keys() & spec.keys()
        if clash:
            raise ValueError(
                f"Two detectors declare thresholds for the same kind(s): "
                f"{sorted(clash)}. Each finding kind must own its knobs."
            )
        merged.update(spec)
    return merged


__all__ = ["effective_threshold", "threshold_resolver", "merge_threshold_specs"]
