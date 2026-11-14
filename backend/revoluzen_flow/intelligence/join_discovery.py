"""Suggest join keys between two datasets, from names, types and shared values.

Picking the key is the part of a join people get wrong: the column names
differ (`customer_id` on orders, `id` on customers), the types differ (a key
read as text on one side and a number on the other), or the obvious pair
shares no values at all. The repair evaluation found the Copilot making the
same mistake. This looks at both sides' samples and ranks the pairs that
actually line up.

Evidence for each candidate pair of columns, one from each side:

* **Shared values**: how many of one side's distinct values appear on the
  other, measured on the samples. The strongest signal; a pair that shares
  nothing is never suggested on its name alone unless the names are the same.
  Two samples under-count a real key: when a column is unique on one side,
  only the sampled part of that side can be found, so with the side's full
  row count the share is scaled to the whole of it and marked as estimated.
  Without that, a category both sides share (a region, a status) matched
  100% on any sample and outranked the key itself.
* **Names**: the same name; the same name once case, punctuation and an
  `_id` ending are set aside; or `<thing>_id` on one side against `id` on a
  side whose step is named for that thing (`customers`).
* **Uniqueness**: a good key is unique on at least one side. Which side, and
  whether both, gives the join's shape: many-to-one, one-to-one, or
  many-to-many, which multiplies rows and is said so. A pair whose values
  each repeat on both sides groups rows rather than naming one, and ranks
  below the pairs that do. Uniqueness counts by degree: a column whose
  sampled values almost never repeat (an export that lists a customer again
  when their tier changes) is still the key, ranks as one, and has its
  matches scaled like a unique side's; the repeats are said in
  ``repeat_note``, because each repeated row is matched more than once.
  Without that, one repeated customer made the customer ID "neither side
  unique" and a region shared by every row outranked it.

Returns column names, percentages and counts only, never a row's values, so
the result can go to the Copilot, which works from names only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Columns sampled per side. Overlap on a sample under-counts a little on very
# large tables; the evidence says it is a sample.
DEFAULT_SAMPLE_ROWS = 2000
MAX_SUGGESTIONS = 5
# Columns past this many per side are not paired; wide tables get their first
# columns, which is where keys usually are.
MAX_COLUMNS = 60
# A side whose sampled values are at least this share distinct is a key with
# a few repeats rather than a group: a customer listed twice in 6,000.
NEAR_UNIQUE = 0.9

_NUMERIC_HINTS = ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "BIGINT", "HUGEINT")


@dataclass
class JoinSuggestion:
    left: str
    right: str
    score: int                      # 0..100
    shape: str                      # many-to-one | one-to-one | one-to-many | many-to-many
    evidence: list[str] = field(default_factory=list)
    left_matched_pct: float = 0.0   # distinct left values found on the right
    right_matched_pct: float = 0.0  # distinct right values found on the left
    # True when a share above was scaled from the samples to the whole side
    # (see the module notes), rather than counted.
    estimated: bool = False
    # When the key side repeats a few of its values: how many sampled rows,
    # in a sentence the canvas shows as it is (names and counts only).
    repeat_note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "same_name": self.left == self.right,
            "score": self.score,
            "shape": self.shape,
            "evidence": list(self.evidence),
            "left_matched_pct": round(self.left_matched_pct, 1),
            "right_matched_pct": round(self.right_matched_pct, 1),
            "estimated": self.estimated,
            "repeat_note": self.repeat_note,
        }


def _norm_value(v: Any) -> str | None:
    """A value as both sides can compare it: 7, 7.0 and '7' are one key."""
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    text = str(v).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]
    return text


def _norm_name(name: str) -> str:
    """customer_ID, CustomerId, customer-id -> customerid; then drop an id ending."""
    flat = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return flat


def _base(name: str) -> str:
    flat = _norm_name(name)
    for suffix in ("id", "key", "code", "no", "num", "number"):
        if flat.endswith(suffix) and len(flat) > len(suffix):
            return flat[: -len(suffix)]
    return flat


def _singular(word: str) -> str:
    w = _norm_name(word)
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ses") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _name_evidence(left: str, right: str, left_label: str, right_label: str) -> tuple[float, str | None]:
    if left == right:
        return 1.0, "same name"
    if left.lower() == right.lower():
        return 0.95, "same name apart from case"
    if _norm_name(left) == _norm_name(right):
        return 0.9, "same name apart from punctuation"
    lb, rb = _base(left), _base(right)
    if lb and lb == rb and (lb != _norm_name(left) or rb != _norm_name(right)):
        return 0.8, f"both name the same thing ({lb})"
    # customer_id on the left, id on a step named "customers" (and the reverse).
    bare = {"id", "key", "code"}
    if _norm_name(right) in bare and lb and lb == _singular(right_label):
        return 0.8, f"'{left}' names the '{right_label}' step's {right}"
    if _norm_name(left) in bare and rb and rb == _singular(left_label):
        return 0.8, f"'{right}' names the '{left_label}' step's {left}"
    if lb and rb and (lb.endswith(rb) or rb.endswith(lb)) and min(len(lb), len(rb)) >= 3:
        return 0.4, "similar names"
    return 0.0, None


def _whose(label: str) -> str:
    """Customers' sample, the bank's sample."""
    return f"{label}'" if str(label).endswith("s") else f"{label}'s"


def _kind(type_name: str) -> str:
    t = (type_name or "").upper()
    if any(h in t for h in _NUMERIC_HINTS):
        return "number"
    if "CHAR" in t or "TEXT" in t or "STRING" in t or t == "UUID":
        return "text"
    if "DATE" in t or "TIME" in t:
        return "time"
    if "BOOL" in t:
        return "bool"
    return "other"


def _column_profile(rows: list[dict], column: str) -> tuple[set[str], int]:
    values = [_norm_value(r.get(column)) for r in rows]
    present = [v for v in values if v is not None]
    return set(present), len(present)


def _types_from(schema: Iterable[dict] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for col in schema or []:
        name = col.get("name") or col.get("column")
        if name:
            out[str(name)] = str(col.get("type") or col.get("data_type") or "")
    return out


def suggest_join_keys(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    left_columns: list[str] | None = None,
    right_columns: list[str] | None = None,
    left_schema: list[dict] | None = None,
    right_schema: list[dict] | None = None,
    left_label: str = "left",
    right_label: str = "right",
    limit: int = MAX_SUGGESTIONS,
    left_total: int | None = None,
    right_total: int | None = None,
) -> list[JoinSuggestion]:
    """Rank the column pairs most likely to be the join key, best first.

    ``left_total`` / ``right_total`` are each side's full row count, when
    known: a side sampled in part has its unique column's matches scaled to
    the whole of it.
    """
    lcols = list(left_columns or (list(left_rows[0].keys()) if left_rows else []))[:MAX_COLUMNS]
    rcols = list(right_columns or (list(right_rows[0].keys()) if right_rows else []))[:MAX_COLUMNS]
    ltypes, rtypes = _types_from(left_schema), _types_from(right_schema)

    lprof = {c: _column_profile(left_rows, c) for c in lcols}
    rprof = {c: _column_profile(right_rows, c) for c in rcols}

    found: list[JoinSuggestion] = []
    for lc in lcols:
        lvals, lcount = lprof[lc]
        for rc in rcols:
            rvals, rcount = rprof[rc]
            name_score, name_reason = _name_evidence(lc, rc, left_label, right_label)
            lk, rk = _kind(ltypes.get(lc, "")), _kind(rtypes.get(rc, ""))
            if "bool" in (lk, rk) and name_score < 1.0:
                continue
            # Two or fewer distinct values is a flag, not a key.
            if (len(lvals) <= 2 or len(rvals) <= 2) and name_score < 0.9:
                continue
            shared = lvals & rvals
            l_pct = (100.0 * len(shared) / len(lvals)) if lvals else 0.0
            r_pct = (100.0 * len(shared) / len(rvals)) if rvals else 0.0
            if not shared and name_score < 0.9:
                continue
            l_unique = bool(lcount) and len(lvals) == lcount
            r_unique = bool(rcount) and len(rvals) == rcount
            # Share of a side's sampled values that are distinct: 1.0 for a
            # key, near 0 for a category such as a region.
            l_ratio = len(lvals) / lcount if lcount else 0.0
            r_ratio = len(rvals) / rcount if rcount else 0.0
            l_key, r_key = l_ratio >= NEAR_UNIQUE, r_ratio >= NEAR_UNIQUE
            # A key column on a side sampled in part: a value from the other
            # side can only be found if its row was in the sample, so the
            # share found is scaled by the part sampled. A few shared values
            # are not enough to scale on.
            l_est, r_est = l_pct, r_pct
            if r_key and right_total and right_total > len(right_rows) and len(shared) >= 5:
                l_est = min(100.0, l_pct * right_total / len(right_rows))
            if l_key and left_total and left_total > len(left_rows) and len(shared) >= 5:
                r_est = min(100.0, r_pct * left_total / len(left_rows))
            estimated = (l_est, r_est) != (l_pct, r_pct)
            overlap = max(l_est, r_est) / 100.0
            uniqueness = max(l_ratio, r_ratio)
            score = 0.55 * overlap + 0.30 * name_score + 0.15 * uniqueness
            if lk != "other" and rk != "other" and lk != rk:
                score -= 0.1  # joinable with a cast, but say so below
            # Each value repeating on both sides is a group (a region, a
            # status), not a key; it would multiply every row it matches.
            if not (l_key or r_key) and lvals and rvals:
                if min(lcount / len(lvals), rcount / len(rvals)) >= 2:
                    score -= 0.3
            # The key side repeats a few values: those rows match twice.
            repeat_note = None
            if not (l_unique or r_unique) and (l_key or r_key):
                col, label, count, distinct = (
                    (rc, right_label, rcount, len(rvals)) if r_key and r_ratio >= l_ratio
                    else (lc, left_label, lcount, len(lvals))
                )
                repeat_note = (
                    f"{col} repeats in {count - distinct:,} of the {count:,} sampled {label} rows: "
                    f"those rows will match more than once"
                )
            if l_unique and r_unique:
                shape = "one-to-one"
            elif r_unique:
                shape = "many-to-one"
            elif l_unique:
                shape = "one-to-many"
            else:
                shape = "many-to-many"

            evidence: list[str] = []
            if name_reason:
                evidence.append(name_reason)
            sample_note = f" (in samples of {len(left_rows):,} and {len(right_rows):,} rows)"
            if lvals:
                evidence.append(f"{l_pct:.0f}% of {_whose(left_label)} {lc} values appear in {_whose(right_label)} {rc}" + sample_note)
            if l_est != l_pct:
                evidence.append(
                    f"the sample holds {len(right_rows):,} of {_whose(right_label)} {right_total:,} rows, so across all of them "
                    f"about {l_est:.0f}% of {_whose(left_label)} {lc} values would match"
                )
            if r_est != r_pct:
                evidence.append(
                    f"the sample holds {len(left_rows):,} of {_whose(left_label)} {left_total:,} rows, so across all of them "
                    f"about {r_est:.0f}% of {_whose(right_label)} {rc} values would match"
                )
            if not shared:
                evidence.append("no values in common in the samples")
            if lk != "other" and rk != "other" and lk != rk:
                evidence.append(f"types differ ({lk} and {rk}); the join compares them after a cast")
            if repeat_note:
                evidence.append(repeat_note)
            elif shape == "many-to-many":
                evidence.append("neither side is unique, so the join can multiply rows")
            elif shape == "many-to-one":
                evidence.append(f"{rc} is unique on {right_label}: each {left_label} row matches at most one")
            elif shape == "one-to-many":
                evidence.append(f"{lc} is unique on {left_label}: each {right_label} row matches at most one")
            else:
                evidence.append("unique on both sides")
            found.append(JoinSuggestion(
                left=lc, right=rc, score=max(0, min(100, round(score * 100))), shape=shape,
                evidence=evidence, left_matched_pct=l_est, right_matched_pct=r_est, estimated=estimated,
                repeat_note=repeat_note,
            ))

    found.sort(key=lambda s: (-s.score, s.left != s.right, s.left, s.right))
    # One suggestion per left column: the best partner for it.
    seen: set[str] = set()
    best: list[JoinSuggestion] = []
    for s in found:
        if s.left in seen:
            continue
        seen.add(s.left)
        best.append(s)
        if len(best) >= limit:
            break
    return best


def join_inputs(workflow: Any, step_id: str) -> tuple[str, str]:
    """(left, right) step ids of a two-input step, as the Join reads them:
    `left_input_id` when it names one of the inputs, else the first."""
    step = next((s for s in workflow.steps if s.id == step_id), None)
    if step is None:
        raise ValueError(f"step '{step_id}' is not in this pipeline")
    inputs = [c.from_step for c in workflow.connections if c.to_step == step_id]
    inputs = list(dict.fromkeys(inputs))
    if len(inputs) < 2:
        raise ValueError(f"'{step.label or step_id}' needs two inputs wired in before keys can be suggested")
    pinned = (step.params or {}).get("left_input_id")
    left = pinned if pinned in inputs else inputs[0]
    right = next(s for s in inputs if s != left)
    return left, right


def suggest_for_step(
    workflow: Any,
    step_id: str,
    *,
    app_state: dict | None,
    data_dir: str,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
) -> dict[str, Any]:
    """Sample both inputs of a join-like step and suggest its keys.

    Each input runs as Test Node runs it: in preview, so nothing is written
    anywhere, with its upstream steps first. Returns step ids, labels, sample
    sizes and the suggestions; never the sampled rows.
    """
    from revoluzen_flow.engine.executor import WorkflowExecutor

    left_id, right_id = join_inputs(workflow, step_id)
    labels = {s.id: (s.label or s.id) for s in workflow.steps}
    executor = WorkflowExecutor(data_dir=data_dir, app_state=app_state or {})
    sides = {}
    for sid in (left_id, right_id):
        trace = executor.execute_step_trace(workflow, sid, preview_limit=sample_rows)
        res = trace.step_results.get(sid)
        if res is None or res.status != "success":
            reason = (res.error if res is not None else None) or "it did not run"
            raise ValueError(f"could not read '{labels.get(sid, sid)}' to sample it: {reason}")
        sides[sid] = res
    left, right = sides[left_id], sides[right_id]
    suggestions = suggest_join_keys(
        left.sample_data, right.sample_data,
        left_columns=left.columns, right_columns=right.columns,
        left_schema=left.schema_info, right_schema=right.schema_info,
        left_label=labels.get(left_id, left_id), right_label=labels.get(right_id, right_id),
        left_total=left.row_count if left.row_count > 0 else None,
        right_total=right.row_count if right.row_count > 0 else None,
    )
    return {
        "step_id": step_id,
        "left_step": left_id,
        "right_step": right_id,
        "left_label": labels.get(left_id, left_id),
        "right_label": labels.get(right_id, right_id),
        "sampled_rows": {"left": len(left.sample_data), "right": len(right.sample_data)},
        "suggestions": [s.as_dict() for s in suggestions],
    }


__all__ = [
    "DEFAULT_SAMPLE_ROWS",
    "JoinSuggestion",
    "join_inputs",
    "suggest_for_step",
    "suggest_join_keys",
]
