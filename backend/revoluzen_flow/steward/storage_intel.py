"""Revoluzen Flow Steward — storage intelligence (2026-06-18).

Watches the managed-table datastore for waste. The signal: an **orphaned
managed table** — one that exists on disk, has not been touched for a while,
and that NO pipeline reads or writes (no Managed Table Source or Managed
Table Sink references it).

★ The age gate is the whole rule (added 2026-10-26). Without it this
detector fired the instant a table existed, which made it wrong about the
most common case it saw. Promoting a file to a managed table NECESSARILY
produces an unreferenced table — you cannot build the pipeline that reads
it until it exists — so a user following the documented
upload → promote → build flow got a HIGH architecture finding telling them
their brand-new table was "most likely left over from a deleted or edited
pipeline". It had never looked at a timestamp, so it had no evidence for
that claim at all.

Hard Rule 6 says quantitative alerts compare against an observed baseline
rather than firing on a single absolute reading. "Unreferenced" is a
single reading; "unreferenced AND untouched for 7 days" is a claim the
detector can actually support. The shape matches connector_health, which
requires both a failure streak AND elapsed time before it speaks.

State detector (re-derived from current tables + workflows on every scan,
like the duplicate-source Archeologist), so there is no journal — the read
side IS the detection. Pure function over (workflows, tables); the API layer
feeds it the live DataStore listing.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)
from .thresholds import threshold_resolver

_LOCAL_TABLE_TYPES = {"local_table_source", "local_table_sink"}

#: How long a table must sit unreferenced AND untouched before it counts as
#: a leftover rather than work in progress. 7 days: long enough to cover a
#: user who promotes a file on Friday and builds the pipeline the following
#: week, short enough that real debris still surfaces.
_ORPHAN_MIN_AGE_HOURS = 168.0

# Operator-tunable, same mechanism as the cardinality detectors: set
# StewardSettings.detectors["orphaned_table"].thresholds = {"min_age_hours": N}.
# The Coverage page renders this spec, so the knob is discoverable rather
# than a constant only a reader of this file knows about.
THRESHOLD_SPEC: dict[str, list[dict]] = {
    FindingKind.ORPHANED_TABLE.value: [
        {"key": "min_age_hours",
         "label": "Age before an unused table is flagged (hours)",
         "default": _ORPHAN_MIN_AGE_HOURS,
         "min": 0, "max": 8760, "step": 24},
    ],
}


_eff = threshold_resolver(THRESHOLD_SPEC)


def _as_dt(value: Any) -> datetime | None:
    """Coerce a model field to an aware UTC datetime, or None if unusable.

    Accepts the datetime StorageTable carries and the ISO string it becomes
    once it has been through JSON. A naive datetime is read as UTC, which is
    what the writer meant — every timestamp in this codebase is
    `datetime.now(timezone.utc)`.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _age_hours(table: Any, now: datetime) -> float | None:
    """Hours since the table was LAST touched, or None if that is unknowable.

    Takes the most recent of created_at / updated_at: a table rewritten
    yesterday is in use whatever its creation date says, and a table whose
    updated_at is missing still has a creation date to go on.

    None means "no usable timestamp", and the caller treats that as "do not
    emit" rather than "old enough". The finding asserts an age; if we cannot
    read one we have not earned the claim. In practice this never fires —
    StorageTable sets both fields via default_factory — so failing closed
    costs nothing real and keeps the detector honest if a future provider
    returns rows without them.
    """
    stamps = [
        d for d in (_as_dt(getattr(table, "created_at", None)),
                    _as_dt(getattr(table, "updated_at", None)))
        if d is not None
    ]
    if not stamps:
        return None
    return (now - max(stamps)).total_seconds() / 3600.0


def _humanize_age(hours: float) -> str:
    if hours < 48:
        return f"{int(round(hours))} hours"
    days = hours / 24.0
    if days < 14:
        return f"{int(round(days))} days"
    return f"{int(days // 7)} weeks"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _node_type(node: dict[str, Any]) -> str:
    return str(node.get("type") or node.get("step_type") or "").strip().lower()


def _node_params(node: dict[str, Any]) -> dict[str, Any]:
    # Workflows reach the scan as normalized dicts; params live under a few
    # possible keys depending on whether the row came from the IR or a graph.
    p = node.get("params")
    if isinstance(p, dict):
        return p
    cfg = node.get("config")
    if isinstance(cfg, dict):
        return cfg
    data = node.get("data")
    if isinstance(data, dict):
        inner = data.get("params")
        return inner if isinstance(inner, dict) else data
    return {}


def detect_orphaned_tables(
    workflows: list[dict[str, Any]],
    tables: list[Any],
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
    thresholds: dict | None = None,
    now: datetime | None = None,
) -> list[StewardFinding]:
    """Flag managed tables that no pipeline references AND that are stale.

    `thresholds` is the StewardSettings per-detector override map; `now` is
    injectable so tests can age a table without sleeping.
    """
    suppressed = suppressed_signatures or set()
    now_dt = now or datetime.now(timezone.utc)
    min_age_hours = _eff(thresholds, FindingKind.ORPHANED_TABLE.value, "min_age_hours")

    referenced: set[str] = set()
    for wf in workflows or []:
        for node in (wf.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            if _node_type(node) in _LOCAL_TABLE_TYPES:
                p = _node_params(node)
                schema = str(p.get("schema_name") or "default").strip().lower()
                name = str(p.get("table_name") or "").strip().lower()
                if name:
                    referenced.add(f"{schema}.{name}")

    findings: list[StewardFinding] = []
    for t in tables or []:
        schema_raw = getattr(t, "schema_name", None) or "default"
        name_raw = getattr(t, "name", None) or ""
        name = str(name_raw).strip().lower()
        if not name:
            continue
        key = f"{str(schema_raw).strip().lower()}.{name}"
        if key in referenced:
            continue
        sig = f"orphan-table::{key}"
        if sig in suppressed:
            continue

        # The age gate. A table younger than the threshold is work in
        # progress, not debris — most often one the user promoted minutes
        # ago and is about to build a pipeline around.
        age_hours = _age_hours(t, now_dt)
        if age_hours is None or age_hours < min_age_hours:
            continue

        disp = f"{schema_raw}.{name_raw}"
        age_text = _humanize_age(age_hours)
        fid = "orphan-" + hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]
        findings.append(StewardFinding(
            id=fid,
            workspace_id=workspace_id,
            kind=FindingKind.ORPHANED_TABLE,
            level=FindingLevel.ARCHITECTURE,
            severity=FindingSeverity.P3,
            status=FindingStatus.OPEN,
            title=f"Managed table {disp} has been unused for {age_text}",
            # State what was observed, not what it probably means. The old
            # text asserted "most likely left over from a deleted or edited
            # pipeline" — a cause this detector never checked and, for a
            # freshly promoted table, simply got wrong.
            body=(
                f"No saved pipeline reads or writes the managed table **{disp}** — "
                f"no Managed Table Source or Managed Table Sink references it — and it "
                f"has not been written to for {age_text}.\n\n"
                f"That combination usually means a leftover from a deleted or edited "
                f"pipeline, but this check cannot tell the difference between debris and "
                f"an intentional deliverable (an export someone downloads, a reference "
                f"table read outside Revoluzen Flow). It also only sees SAVED pipelines, "
                f"so a table you are building against in an unsaved editor still counts "
                f"as unused.\n\n"
                f"Review it on the Storage page: keep it, or delete it to reclaim space. "
                f"Dismiss to acknowledge it as intentional."
            ),
            evidence={
                "source_signature": sig,
                "schema": schema_raw,
                "table": name_raw,
                "row_count": getattr(t, "row_count", None),
                "age_hours": round(age_hours, 1),
                "min_age_hours": min_age_hours,
            },
            proposed_actions=[{
                "label": "Dismiss (intentional table)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            }],
            first_seen=_iso_now(),
            last_seen=_iso_now(),
            occurrences=1,
            confidence="high",
            confidence_score=1.0,
            evidence_count=1,
            baseline_window="current_state",
        ))
    return findings


__all__ = ["detect_orphaned_tables", "THRESHOLD_SPEC"]
