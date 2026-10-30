"""Pre-commit gate — refuse the write instead of reporting it afterwards.

``verify.py`` judges a finished run. That is the right primitive for
evidence, and the wrong one for protection: by the time it returns, a
``replace``-mode sink has already truncated the destination. The first
time this was demonstrated end to end, the pipeline reported SUCCESS,
every step reported success, and a managed table lost its three rows —
the receipt faithfully recorded the damage.

    "We told you we destroyed your table."
    "We refused to destroy your table."

Only the second is protection. This module is the second one.

## What can be checked early, and what cannot

A guarantee is pre-commit checkable when it is answerable from the
relation that is *about to be written*:

  checkable      no_empty_output, row_count_min, row_count_max,
                 row_count_variance, schema_contains
  not checkable  all_steps_succeeded   (steps after this one have not run)
                 checkpoint_enabled    (a property of the run, not the data)
                 destination_verified  (needs the commit to have happened)
                 freshness_max_age     (out-of-band fact about the source)
                 no_duplicate_key      (possible, but needs a scan — see below)

``no_duplicate_key`` is deliberately excluded. It is answerable, but only
by scanning the full relation, and a gate that silently doubles the cost
of every write is a gate people switch off. It stays a post-run check.

The post-run verification still runs and still produces the receipt —
this does not replace it. A blocked write produces a receipt too, so the
refusal is itself evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from revoluzen_flow.contracts.schema import ExecutionContract, GuaranteeType

__all__ = [
    "PRECOMMIT_CHECKABLE",
    "PrecommitViolation",
    "ContractBlockedWrite",
    "check_precommit",
]

PRECOMMIT_CHECKABLE: frozenset[GuaranteeType] = frozenset({
    GuaranteeType.NO_EMPTY_OUTPUT,
    GuaranteeType.ROW_COUNT_MIN,
    GuaranteeType.ROW_COUNT_MAX,
    GuaranteeType.ROW_COUNT_VARIANCE,
    GuaranteeType.SCHEMA_CONTAINS,
})


@dataclass(frozen=True)
class PrecommitViolation:
    guarantee: GuaranteeType
    description: str
    detail: str

    def __str__(self) -> str:
        return f"{self.description or self.guarantee.value} ({self.detail})"


class ContractBlockedWrite(RuntimeError):
    """A write was refused because it would have broken the contract.

    Deliberately an error, so the run fails rather than succeeding with a
    destination nobody checked. A silent skip would reproduce the exact
    problem this exists to solve: a green run and bad data.
    """

    def __init__(self, step_id: str, violations: list[PrecommitViolation]):
        self.step_id = step_id
        self.violations = violations
        lines = "; ".join(str(v) for v in violations)
        super().__init__(
            f"Execution Contract blocked the write at '{step_id}': {lines}. "
            f"The destination was left unchanged."
        )


def check_precommit(
    contract: ExecutionContract,
    *,
    row_count: int,
    columns: list[str] | None = None,
    step_id: str = "",
) -> list[PrecommitViolation]:
    """Evaluate the checkable guarantees against the pending write.

    Returns the violations; an empty list means the write may proceed.
    Raising is the caller's decision, because a dry run wants the list
    without the exception.
    """
    cols = {c.lower() for c in (columns or [])}
    out: list[PrecommitViolation] = []

    for g in contract.guarantees:
        if g.type not in PRECOMMIT_CHECKABLE:
            continue
        # A guarantee scoped to a different step says nothing about this
        # write; applying it here would block the wrong sink.
        if g.output_step and step_id and g.output_step != step_id:
            continue
        desc = g.description or ""

        if g.type is GuaranteeType.NO_EMPTY_OUTPUT and row_count <= 0:
            out.append(PrecommitViolation(g.type, desc, "would write 0 rows"))

        elif g.type is GuaranteeType.ROW_COUNT_MIN and g.min_rows is not None:
            if row_count < g.min_rows:
                out.append(PrecommitViolation(
                    g.type, desc, f"would write {row_count}, minimum {g.min_rows}"))

        elif g.type is GuaranteeType.ROW_COUNT_MAX and g.max_rows is not None:
            if row_count > g.max_rows:
                out.append(PrecommitViolation(
                    g.type, desc, f"would write {row_count}, maximum {g.max_rows}"))

        elif g.type is GuaranteeType.ROW_COUNT_VARIANCE:
            if g.expected_rows is not None and g.variance_pct is not None:
                lo = g.expected_rows * (1 - g.variance_pct)
                hi = g.expected_rows * (1 + g.variance_pct)
                if not (lo <= row_count <= hi):
                    out.append(PrecommitViolation(
                        g.type, desc,
                        f"would write {row_count}, expected "
                        f"{g.expected_rows} ±{g.variance_pct:.0%}"))

        elif g.type is GuaranteeType.SCHEMA_CONTAINS and g.columns:
            # Only judge when the columns are actually known. Treating
            # "could not read the schema" as a violation would block
            # writes for a reason that has nothing to do with the data.
            if cols:
                missing = [c for c in g.columns if c.lower() not in cols]
                if missing:
                    out.append(PrecommitViolation(
                        g.type, desc, f"missing column(s): {', '.join(missing)}"))

    return out
