"""Repeated logic: the same step written again in several pipelines (2026-11-11).

The Archeologist finds pipelines that read the same source or are copies of
each other. This finds the smaller, commoner thing: one piece of logic (a SQL
transform, a filter, a join, a set of Data Quality rules) pasted into several
pipelines. Each copy then has to be fixed separately, and they drift apart.

A step counts as the same logic when its type and its settings are the same
once the settings that only place or name it are left out (labels, positions,
ids) and SQL is compared without case or spacing differences. Trivial steps
(a step whose settings are only a few characters) are not reported: a
``LIMIT 100`` in five pipelines is not a problem.

Pure code over the pipeline definitions the Steward scan already reads: no
data, no model. One finding per repeated piece of logic, which says what to
make it into: a shared rule set for Data Quality rules, one pipeline the
others call (Run Pipeline step) or a managed table they read for the rest.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from .models import FindingKind, FindingSeverity, FindingStatus, StewardFinding, level_for_kind

LOGIC_TYPES: frozenset[str] = frozenset({
    "transform", "filter", "data_quality", "validate", "validation", "join", "aggregate",
    "derived_column", "data_wrangler", "window", "pivot", "unpivot", "deduplicate",
    "conditional_split", "schema_mapper", "reconcile", "flatten_explode", "scd2", "upsert",
    "lookup", "typecast", "rename", "code_script", "execute_sql_task", "sort", "switch_case",
})

#: Settings that place or name a step, or that the engine adds while running.
#: ``rule_sets`` points at shared rules: using the same set in many
#: pipelines is the fix this finding suggests, not a copy.
_IGNORED_KEYS = frozenset({
    "_input_step_ids", "_node_labels", "_settings", "_step_id", "_rule_set_versions",
    "label", "name", "description", "notes", "position", "rule_sets",
})
_RULE_STEPS = frozenset({"data_quality", "validate", "validation"})

MIN_PIPELINES = 2
MIN_LOGIC_CHARS = 40
_SQL_KEYS = ("sql", "query", "expression", "condition", "where", "filter", "code")


def _normal(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _normal(v, k) for k, v in sorted(value.items()) if k not in _IGNORED_KEYS}
    if isinstance(value, list):
        return [_normal(v, key) for v in value]
    if isinstance(value, str) and key.lower() in _SQL_KEYS:
        return re.sub(r"\s+", " ", value.strip().rstrip(";")).lower()
    return value


def logic_fingerprint(step_type: str, params: dict[str, Any]) -> tuple[str, str] | None:
    """(fingerprint, canonical text) for a logic step; None for a step that
    is not logic or is too small to matter."""
    if step_type not in LOGIC_TYPES or not isinstance(params, dict):
        return None
    if step_type in _RULE_STEPS and not params.get("rules"):
        # Only shared rule sets, no rules of its own: nothing was copied.
        return None
    canonical = json.dumps(_normal(params), sort_keys=True, default=str, separators=(",", ":"))
    if len(canonical) < MIN_LOGIC_CHARS:
        return None
    digest = hashlib.sha256(f"{step_type}|{canonical}".encode("utf-8")).hexdigest()[:16]
    return digest, canonical


def _what_to_do(step_type: str) -> str:
    if step_type in ("data_quality", "validate", "validation"):
        return ("Save these rules once as a shared rule set (the Data Quality step's settings, "
                "Save this step's rules as a shared set) and pick the set in each pipeline, so a "
                "fix reaches all of them.")
    return ("Keep this logic in one pipeline and have the others call it with a Run Pipeline step, "
            "or write its result once to a managed table that the others read.")


def _summary(step_type: str, params: dict[str, Any]) -> str:
    for key in _SQL_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            text = re.sub(r"\s+", " ", value.strip())
            return text[:160] + ("…" if len(text) > 160 else "")
    rules = params.get("rules")
    if isinstance(rules, list) and rules:
        parts = [f"{r.get('column')} {r.get('op') or 'not_null'}" for r in rules[:4] if isinstance(r, dict)]
        return "; ".join(parts) + (f" and {len(rules) - 4} more" if len(rules) > 4 else "")
    keys = [k for k in params if k not in _IGNORED_KEYS and not k.startswith("_")]
    return f"{step_type} with {', '.join(sorted(keys)[:5])}"


def detect_repeated_logic(
    workflows: Iterable[dict[str, Any]],
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
    min_pipelines: int = MIN_PIPELINES,
) -> list[StewardFinding]:
    """One finding per piece of logic found in ``min_pipelines`` or more
    pipelines. ``workflows`` are the Steward scan's ``{id, name, nodes}``."""
    from .archeologist import _step_type_and_params, _utc_now_iso

    groups: dict[str, dict[str, Any]] = {}
    for wf in workflows:
        for node in wf.get("nodes") or []:
            step_type, params, label = _step_type_and_params(node)
            fp = logic_fingerprint(step_type, params)
            if fp is None:
                continue
            digest, _canonical = fp
            g = groups.setdefault(digest, {"type": step_type, "params": params, "uses": []})
            g["uses"].append({"pipeline_id": wf.get("id") or "", "pipeline": wf.get("name") or "",
                              "step_id": node.get("id") or "", "step": label})

    now = _utc_now_iso()
    suppressed = suppressed_signatures or set()
    findings: list[StewardFinding] = []
    for digest, g in sorted(groups.items()):
        pipelines = sorted({u["pipeline_id"] for u in g["uses"]})
        if len(pipelines) < max(2, int(min_pipelines)):
            continue
        signature = f"repeated-logic:{digest}"
        if signature in suppressed:
            continue
        names = sorted({u["pipeline"] for u in g["uses"]})
        what = _summary(g["type"], g["params"])
        findings.append(StewardFinding(
            id=f"rep-logic-{digest}",
            workspace_id=workspace_id,
            kind=FindingKind.REPEATED_LOGIC,
            level=level_for_kind(FindingKind.REPEATED_LOGIC),
            severity=FindingSeverity.P3,
            status=FindingStatus.OPEN,
            title=f"The same {g['type'].replace('_', ' ')} step is in {len(pipelines)} pipelines",
            body="\n".join([
                f"**Logic:** {what}",
                f"**Pipelines:** {', '.join(names)}",
                "",
                _what_to_do(g["type"]),
                "",
                "If the copies are meant to stay separate, dismiss this finding.",
            ]),
            evidence={
                "source_signature": signature,
                "step_type": g["type"],
                "logic": what,
                "uses": g["uses"],
                "pipeline_count": len(pipelines),
            },
            proposed_actions=[{
                "label": "Dismiss (the copies are meant to stay separate)",
                "action": "suppress_finding",
                "params": {"finding_id": f"rep-logic-{digest}", "scope": "signature"},
            }],
            first_seen=now,
            last_seen=now,
            occurrences=1,
            confidence="high",
            confidence_score=1.0,
            evidence_count=len(g["uses"]),
            baseline_window="pipeline_definitions",
        ))
    return findings


__all__ = ["LOGIC_TYPES", "detect_repeated_logic", "logic_fingerprint"]
