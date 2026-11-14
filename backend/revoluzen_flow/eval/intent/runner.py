"""Score request-to-pipeline accuracy (2026-11-11).

For each case (cases.py): draft a pipeline from the request, run it on the
case's files, and compare the rows it returns with the expected rows. Four
results per case, each a yes or no:

* **drafted**: the planner returned a pipeline;
* **valid**: the pipeline passes the same checks a saved pipeline does;
* **ran**: the step that feeds the destination ran without an error;
* **correct**: its rows are the expected rows.

Accuracy is the share of cases that are correct. The others say where the
rest fell short.

Three ways to draft:

* ``rule``: the rule planner, with no model. Deterministic and free, so it
  runs on every release (``.github/workflows/release.yml``).
* ``live``: the configured provider, through the same planner call the
  Editor and the Copilot use (``ai_generate_pipeline``). Costs money; run on
  demand. ``--record`` keeps each raw draft.
* ``replay``: drafts recorded by a live run, converted and run again. Scores
  everything after the model (conversion, steps, engine) on every release
  without a model.

The pipeline runs in preview mode on a temporary folder: destinations write
nothing. File names in the draft are pointed at the case's files.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .cases import CASES, IntentCase

_SINK_SUFFIXES = ("_sink",)
_SINK_TYPES = {"output", "destination", "pipeline_output"}
_PREVIEW_LIMIT = 10_000
_TOLERANCE = 1e-6


@dataclass
class CaseResult:
    id: str
    request: str
    drafted: bool = False
    valid: bool = False
    ran: bool = False
    correct: bool = False
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return 1.0 if self.correct else 0.25 * (self.drafted + self.valid + self.ran)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["score"] = round(self.score, 4)
        return out


# ── Comparing rows ───────────────────────────────────────────────────────

def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    return None


def _same(a: Any, b: Any) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE)
    return str(a).strip() == str(b).strip()


def _column_values_match(expected: list[Any], got: list[Any]) -> bool:
    if len(expected) != len(got):
        return False
    remaining = list(got)
    for e in expected:
        hit = next((i for i, g in enumerate(remaining) if _same(e, g)), None)
        if hit is None:
            return False
        remaining.pop(hit)
    return True


def compare_rows(expected: list[dict[str, Any]], got: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Whether ``got`` holds the expected rows (any order, exactly as many).

    Each expected column is read from the result column of the same name
    (any case); when there is none, from a result column holding the same
    values, since a request seldom names the column it computes.
    """
    notes: list[str] = []
    if len(got) != len(expected):
        return False, [f"{len(got)} rows, expected {len(expected)}"]
    if not expected:
        return True, notes
    result_cols = list(got[0].keys()) if got else []
    by_lower = {c.lower(): c for c in result_cols}
    mapping: dict[str, str] = {}
    for col in expected[0].keys():
        if col.lower() in by_lower:
            mapping[col] = by_lower[col.lower()]
            continue
        wanted = [r.get(col) for r in expected]
        match = next((c for c in result_cols if c not in mapping.values()
                      and _column_values_match(wanted, [r.get(c) for r in got])), None)
        if match is None:
            return False, [f"no column holds the expected {col!r} values (columns: {', '.join(result_cols)})"]
        mapping[col] = match
        notes.append(f"{col!r} read from {match!r}")
    remaining = [dict(r) for r in got]
    for e in expected:
        hit = next((i for i, r in enumerate(remaining)
                    if all(_same(e[c], r.get(mapping[c])) for c in e)), None)
        if hit is None:
            return False, notes + [f"no row matches {e}"]
        remaining.pop(hit)
    return True, notes


# ── Running a draft ──────────────────────────────────────────────────────

def _result_step(workflow: Any) -> str | None:
    """The step whose rows are the answer: the one feeding the first
    destination, or the last step when there is no destination."""
    types = {s.id: (s.type.value if hasattr(s.type, "value") else str(s.type)) for s in workflow.steps}
    sinks = [sid for sid, t in types.items() if t in _SINK_TYPES or t.endswith(_SINK_SUFFIXES)]
    for sink in sinks:
        feeding = [c.from_step for c in workflow.connections if c.to_step == sink]
        if feeding:
            return feeding[-1]
    outgoing = {c.from_step for c in workflow.connections}
    leaves = [s.id for s in workflow.steps if s.id not in outgoing and s.id not in sinks]
    return leaves[-1] if leaves else (workflow.steps[-1].id if workflow.steps else None)


def _point_at_files(ir: dict[str, Any], folder: str) -> None:
    for step in ir.get("steps") or []:
        params = step.get("params") or {}
        for key in ("file_path", "path"):
            value = params.get(key)
            if isinstance(value, str) and value:
                name = os.path.basename(value.replace("\\", "/"))
                candidate = os.path.join(folder, name)
                if os.path.exists(candidate):
                    params[key] = candidate


def score_draft(case: IntentCase, ir: dict[str, Any] | None) -> CaseResult:
    """Run one drafted pipeline (an IR dict) on the case's files and score it."""
    from revoluzen_flow.engine.executor import WorkflowExecutor
    from revoluzen_flow.ir.schema import Workflow
    from revoluzen_flow.ir.validator import validate_workflow

    res = CaseResult(id=case.id, request=case.request)
    if not ir or not ir.get("steps"):
        res.notes.append("the planner returned no pipeline")
        return res
    res.drafted = True
    with tempfile.TemporaryDirectory(prefix="flow-intent-") as folder:
        for name, text in case.files.items():
            with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        ir = json.loads(json.dumps(ir))
        _point_at_files(ir, folder)
        try:
            workflow = Workflow(**ir)
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"not a pipeline: {type(exc).__name__}: {exc}"[:300])
            return res
        res.steps = [s.type.value if hasattr(s.type, "value") else str(s.type) for s in workflow.steps]
        problems = [e for e in validate_workflow(workflow) if getattr(e, "severity", "error") == "error"]
        res.valid = not problems
        if problems:
            res.notes.append("checks: " + "; ".join(p.message for p in problems[:3]))
        target = _result_step(workflow)
        if target is None:
            res.notes.append("no step to read the result from")
            return res
        try:
            trace = WorkflowExecutor(data_dir=folder, app_state={}).execute_step_trace(
                workflow, target, preview_limit=_PREVIEW_LIMIT,
            )
            step = trace.step_results.get(target)
        except Exception as exc:  # noqa: BLE001
            res.notes.append(f"run failed: {type(exc).__name__}: {exc}"[:300])
            return res
        if step is None or step.status != "success":
            res.notes.append(f"run failed: {(step.error if step is not None else 'no result')}"[:300])
            return res
        res.ran = True
        res.correct, notes = compare_rows(case.expected, list(step.sample_data or []))
        res.notes.extend(notes)
    return res


# ── Drafting ─────────────────────────────────────────────────────────────

def draft_rule(case: IntentCase) -> dict[str, Any] | None:
    from revoluzen_flow.planner.rule_planner import RulePlanner

    result = RulePlanner().plan(case.request)
    return result.workflow.model_dump(mode="json") if result.workflow else None


def draft_live(case: IntentCase, recorded: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from revoluzen_flow.planner.ai_client import ai_generate_pipeline
    from revoluzen_flow.planner.ir_from_json import planner_json_to_ir

    raw = asyncio.run(ai_generate_pipeline([{"role": "user", "content": case.request}]))
    if recorded is not None:
        recorded[case.id] = raw
    return planner_json_to_ir(raw, name=case.id) if raw else None


def draft_replay(recorded: dict[str, Any]) -> Callable[[IntentCase], dict[str, Any] | None]:
    from revoluzen_flow.planner.ir_from_json import planner_json_to_ir

    def _draft(case: IntentCase) -> dict[str, Any] | None:
        raw = recorded.get(case.id)
        return planner_json_to_ir(raw, name=case.id) if raw else None
    return _draft


def run(drafter: Callable[[IntentCase], dict[str, Any] | None], cases: list[IntentCase] | None = None) -> dict[str, Any]:
    """Draft and score every case; returns the report."""
    results: list[CaseResult] = []
    for case in cases or CASES:
        try:
            ir = drafter(case)
        except Exception as exc:  # noqa: BLE001 — a drafter failure is a result
            r = CaseResult(id=case.id, request=case.request)
            r.notes.append(f"drafting failed: {type(exc).__name__}: {exc}"[:300])
            results.append(r)
            continue
        results.append(score_draft(case, ir))
    total = len(results)

    def rate(attr: str) -> float:
        return round(sum(1 for r in results if getattr(r, attr)) / max(total, 1), 4)

    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "cases": total,
        "accuracy": rate("correct"),
        "drafted": rate("drafted"),
        "valid": rate("valid"),
        "ran": rate("ran"),
        "avg_score": round(sum(r.score for r in results) / max(total, 1), 4),
        "results": [r.to_dict() for r in results],
    }


def to_markdown(report: dict[str, Any], title: str) -> str:
    lines = [
        f"## {title}",
        "",
        f"**Accuracy {report['accuracy']:.0%}** ({sum(1 for r in report['results'] if r['correct'])} of "
        f"{report['cases']} requests gave the expected rows). Drafted {report['drafted']:.0%}, "
        f"valid {report['valid']:.0%}, ran {report['ran']:.0%}.",
        "",
        "| Case | Correct | Ran | Valid | Drafted | Steps | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    yn = {True: "yes", False: "no"}
    for r in report["results"]:
        notes = "; ".join(r["notes"])[:160].replace("|", "/")
        lines.append(f"| {r['id']} | {yn[r['correct']]} | {yn[r['ran']]} | {yn[r['valid']]} | "
                     f"{yn[r['drafted']]} | {' → '.join(r['steps'])} | {notes} |")
    return "\n".join(lines) + "\n"


__all__ = ["CaseResult", "compare_rows", "draft_live", "draft_replay", "draft_rule", "run", "score_draft", "to_markdown"]
